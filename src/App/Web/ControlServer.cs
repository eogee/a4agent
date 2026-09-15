using System.Diagnostics;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using a4agent.Core.Config;
using a4agent.Core.Engine;
using a4agent.Core.Update;

namespace a4agent.App.Web;

/// <summary>需要回到 WinForms UI 线程（或应用生命周期）才能执行的操作桥。</summary>
public interface IUiBridge
{
    void RunWizard();     // UI 线程打开原生配置向导
    void RequestExit();   // UI 线程执行退出（停引擎 + 收托盘 + Exit）
}

readonly record struct LogLine(long Id, string Text);

/// <summary>
/// 内嵌控制台服务：HttpListener 只绑 127.0.0.1，托管 layui 前端（程序集内嵌资源）
/// 与 /api/v1 REST + SSE。引擎本身另占 8080（或配置端口），两者互不相干。
/// </summary>
public sealed class ControlServer : IDisposable
{
    const int DefaultPort = 18080;
    const int MaxPortsToTry = 20;
    const int RecentLogCapacity = 800;

    static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    static readonly Dictionary<string, string> Mime = new(StringComparer.OrdinalIgnoreCase)
    {
        [".html"] = "text/html; charset=utf-8",
        [".css"] = "text/css; charset=utf-8",
        [".js"] = "application/javascript; charset=utf-8",
        [".json"] = "application/json; charset=utf-8",
        [".map"] = "application/json; charset=utf-8",
        [".svg"] = "image/svg+xml",
        [".png"] = "image/png",
        [".ico"] = "image/x-icon",
        [".woff"] = "font/woff",
        [".woff2"] = "font/woff2",
        [".ttf"] = "font/ttf",
        [".eot"] = "application/vnd.ms-fontobject",
    };

    readonly AppConfig _cfg;
    readonly ServerEngine _engine;
    readonly IUiBridge _bridge;

    HttpListener? _listener;
    CancellationTokenSource? _cts;
    readonly object _sseGate = new();
    readonly List<SseClient> _sseClients = new();
    readonly Queue<LogLine> _recent = new();
    long _logId;
    System.Threading.Timer? _heartbeat;

    BackendCapabilities _caps = new();
    string _capsProbedDir = "";

    // 更新下载编排（同一时刻仅一个下载任务）
    readonly object _updateGate = new();
    volatile bool _downloading;
    UpdateManifest? _lastManifest;
    UpdateAsset? _lastAsset;
    string? _downloadedInstaller;

    public string BaseUrl { get; private set; } = "";

    public ControlServer(AppConfig cfg, ServerEngine engine, IUiBridge bridge)
    {
        _cfg = cfg;
        _engine = engine;
        _bridge = bridge;
    }

    // ───────────────────────── 生命周期 ─────────────────────────

    /// <summary>从 18080 起找一个空闲回环端口并开始监听；失败抛出。</summary>
    public void Start()
    {
        Exception? last = null;
        for (int port = DefaultPort; port < DefaultPort + MaxPortsToTry; port++)
        {
            var listener = new HttpListener();
            listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            try
            {
                listener.Start();
                _listener = listener;
                BaseUrl = $"http://127.0.0.1:{port}";
                break;
            }
            catch (HttpListenerException ex)
            {
                last = ex;
            }
        }
        if (_listener == null) throw last ?? new IOException("无法监听控制台端口");

        _cts = new CancellationTokenSource();
        var thread = new Thread(AcceptLoop) { IsBackground = true, Name = "a4agent-console" };
        thread.Start();
        _heartbeat = new System.Threading.Timer(
            _ => BroadcastRaw(": ping\n\n"), null, TimeSpan.FromSeconds(15), TimeSpan.FromSeconds(15));
    }

    void AcceptLoop()
    {
        while (_cts is { IsCancellationRequested: false })
        {
            HttpListenerContext ctx;
            try { ctx = _listener!.GetContext(); }
            catch { break; }
            _ = Task.Run(() => HandleSafe(ctx));
        }
    }

    async Task HandleSafe(HttpListenerContext ctx)
    {
        try
        {
            var path = ctx.Request.Url?.AbsolutePath ?? "/";
            if (path.StartsWith("/api/", StringComparison.Ordinal)) await HandleApiAsync(ctx);
            else ServeStatic(ctx);
        }
        catch (Exception ex)
        {
            try { WriteError(ctx.Response, "服务器内部错误: " + ex.Message, 500); } catch { /* 连接已断 */ }
        }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _heartbeat?.Dispose();
        try { _listener?.Stop(); _listener?.Close(); } catch { }
        lock (_sseGate)
        {
            foreach (var c in _sseClients) c.Close();
            _sseClients.Clear();
        }
    }

    // ───────────────────────── 静态资源（程序集内嵌） ─────────────────────────

    /// <summary>前端资源以 EmbeddedResource 打包，资源名 = 本命名空间 + ".frontend." + 相对路径（'/'→'.'）。</summary>
    static string ResourcePrefix => typeof(ControlServer).Namespace + ".frontend.";

    Dictionary<string, string>? _resourceMap;

    string? FindResource(string relPath)
    {
        if (_resourceMap == null)
        {
            var map = new Dictionary<string, string>(StringComparer.Ordinal);
            var prefix = ResourcePrefix;
            foreach (var name in typeof(ControlServer).Assembly.GetManifestResourceNames())
            {
                if (!name.StartsWith(prefix, StringComparison.Ordinal)) continue;
                map[name[prefix.Length..]] = name;
            }
            _resourceMap = map;
        }
        return _resourceMap.GetValueOrDefault(relPath.Replace('/', '.'));
    }

    void ServeStatic(HttpListenerContext ctx)
    {
        var rel = ctx.Request.Url?.AbsolutePath.TrimStart('/') ?? "";
        if (rel.Length == 0) rel = "index.html";
        if (rel.Contains("..")) { WriteError(ctx.Response, "not found", 404); return; }

        var name = FindResource(rel);
        using var stream = name == null ? null : typeof(ControlServer).Assembly.GetManifestResourceStream(name);
        if (stream == null) { WriteError(ctx.Response, "not found", 404); return; }

        var ext = Path.GetExtension(rel);
        ctx.Response.ContentType = Mime.GetValueOrDefault(ext, "application/octet-stream");
        ctx.Response.Headers.Add("Cache-Control", "no-cache");
        ctx.Response.ContentLength64 = stream.Length;
        stream.CopyTo(ctx.Response.OutputStream);
        ctx.Response.OutputStream.Close();
    }

    // ───────────────────────── API 路由 ─────────────────────────

    async Task HandleApiAsync(HttpListenerContext ctx)
    {
        var req = ctx.Request;
        var path = req.Url!.AbsolutePath;
        var method = req.HttpMethod;
        var isGet = method == "GET";

        switch (path, method)
        {
            case ("/api/v1/status", "GET"): WriteJson(ctx.Response, ComputeStatus()); return;
            case ("/api/v1/start", "POST"): HandleStart(ctx.Response); return;
            case ("/api/v1/stop", "POST"): _engine.Stop(); WriteOk(ctx.Response); return;
            case ("/api/v1/wizard", "POST"): _bridge.RunWizard(); WriteOk(ctx.Response); return;

            case ("/api/v1/models", "GET"): WriteJson(ctx.Response, ModelsPayload()); return;
            case ("/api/v1/models/dirs", "POST"): await HandleAddModelDirAsync(ctx); return;
            case ("/api/v1/models/default", "POST"): await HandleSetDefaultModelAsync(ctx); return;

            case ("/api/v1/dirs", "GET"): HandleBrowseDirs(ctx); return;

            case ("/api/v1/settings", "GET"): WriteJson(ctx.Response, SettingsPayload()); return;
            case ("/api/v1/settings", "POST"): await HandleSaveSettingsAsync(ctx); return;

            case ("/api/v1/connect", "GET"): WriteJson(ctx.Response, ComputeConnect()); return;

            case ("/api/v1/logs", "GET"): HandleSse(ctx); return;
            case ("/api/v1/logs/recent", "GET"): HandleRecentLogsQuery(ctx.Request, ctx.Response); return;

            case ("/api/v1/update/check", "POST"): await HandleUpdateCheckAsync(ctx.Response); return;
            case ("/api/v1/update/install", "POST"): HandleUpdateInstall(ctx.Response); return;
            case ("/api/v1/update/apply", "POST"): HandleUpdateApply(ctx.Response); return;

            default:
                WriteError(ctx.Response, isGet ? "接口不存在" : $"不支持的请求: {method} {path}", 404);
                return;
        }
    }

    // ───────────────────────── 状态 / 启停 ─────────────────────────

    sealed record StatusDto(
        [property: JsonPropertyName("state")] string State,
        [property: JsonPropertyName("engine_missing")] bool EngineMissing,
        [property: JsonPropertyName("port")] int Port,
        [property: JsonPropertyName("host")] string Host,
        [property: JsonPropertyName("base_url")] string ApiBaseUrl,
        [property: JsonPropertyName("default_model")] string DefaultModel,
        [property: JsonPropertyName("default_model_name")] string DefaultModelName,
        [property: JsonPropertyName("caps_mtp")] bool CapsMtp,
        [property: JsonPropertyName("version")] string Version);

    void EnsureCaps()
    {
        var dir = _cfg.EffectiveEngineDir;
        if (string.Equals(dir, _capsProbedDir, StringComparison.OrdinalIgnoreCase)) return;
        _caps = BackendCapabilities.Detect(dir);
        _capsProbedDir = dir;
    }

    StatusDto ComputeStatus()
    {
        EnsureCaps();
        var state = _engine.State switch
        {
            EngineState.Running => "running",
            EngineState.Starting => "starting",
            EngineState.Failed => "failed",
            _ => "stopped",
        };
        var model = _cfg.DefaultModelPath ?? "";
        return new StatusDto(
            state,
            !EngineLocator.IsEnginePresent(_cfg.EffectiveEngineDir),
            _cfg.Port,
            _cfg.Host,
            $"http://127.0.0.1:{_cfg.Port}/v1",
            model,
            string.IsNullOrEmpty(model) ? "" : Path.GetFileNameWithoutExtension(model),
            _caps.SupportsMtp,
            Updater.CurrentVersion);
    }

    void HandleStart(HttpListenerResponse resp)
    {
        if (_engine.IsBusy) { WriteOk(resp); return; }
        if (!EngineLocator.IsEnginePresent(_cfg.EffectiveEngineDir))
        {
            WriteError(resp, "未检测到推理引擎（llama-server.exe），请先在「设置 → 配置向导」完成配置");
            return;
        }
        if (string.IsNullOrEmpty(_cfg.DefaultModelPath) || !File.Exists(_cfg.DefaultModelPath))
        {
            WriteError(resp, "未设置有效的默认模型，请先到「模型」页选择");
            return;
        }
        ConfigStore.Save(_cfg);
        _engine.Start(_cfg);
        WriteOk(resp);
    }

    // ───────────────────────── 模型 ─────────────────────────

    object ModelsPayload()
    {
        var models = Ui.ScanDirs(_cfg.ModelDirs).Select(m => new
        {
            file_path = m.FilePath,
            file_name = Path.GetFileName(m.FilePath),
            size_text = $"{m.FileSize / (1024d * 1024 * 1024):N1} GB",
            quant = m.QuantLabel,
            ctx_text = m.NativeContext > 0 ? $"{m.NativeContext / 1024}k" : "-",
            mtp = m.Ok && m.HasNextnTensors,
        });
        return new { models, default_model = _cfg.DefaultModelPath, model_dirs = _cfg.ModelDirs };
    }

    static async Task<JsonElement> ReadJsonBodyAsync(HttpListenerRequest req)
    {
        using var ms = new MemoryStream();
        await req.InputStream.CopyToAsync(ms);
        if (ms.Length == 0) return default;
        try { return JsonSerializer.Deserialize<JsonElement>(Encoding.UTF8.GetString(ms.ToArray())); }
        catch { return default; }
    }

    async Task HandleAddModelDirAsync(HttpListenerContext ctx)
    {
        var body = await ReadJsonBodyAsync(ctx.Request);
        var dir = body.ValueKind == JsonValueKind.Object && body.TryGetProperty("path", out var p)
            ? p.GetString()?.Trim() ?? "" : "";
        if (dir.Length == 0 || !Directory.Exists(dir))
        {
            WriteError(ctx.Response, "目录不存在");
            return;
        }
        if (!_cfg.ModelDirs.Contains(dir, StringComparer.OrdinalIgnoreCase))
            _cfg.ModelDirs.Add(dir);
        ConfigStore.Save(_cfg);
        Log($"[模型] 已添加目录: {dir}");
        WriteOk(ctx.Response);
    }

    async Task HandleSetDefaultModelAsync(HttpListenerContext ctx)
    {
        var body = await ReadJsonBodyAsync(ctx.Request);
        var path = body.ValueKind == JsonValueKind.Object && body.TryGetProperty("path", out var p)
            ? p.GetString() ?? "" : "";
        if (path.Length == 0 || !File.Exists(path))
        {
            WriteError(ctx.Response, "请先选中一个有效模型");
            return;
        }
        _cfg.DefaultModelPath = path;
        ConfigStore.Save(_cfg);
        Log($"[配置] 默认模型 -> {path}");
        BroadcastState();
        WriteOk(ctx.Response);
    }

    // ───────────────────────── 目录浏览（引擎目录 / 模型目录选择器） ─────────────────────────

    void HandleBrowseDirs(HttpListenerContext ctx)
    {
        var raw = ctx.Request.QueryString["path"] ?? "";
        object payload;
        if (string.IsNullOrWhiteSpace(raw))
        {
            var drives = DriveInfo.GetDrives()
                .Where(d => d.IsReady)
                .Select(d => new { name = d.Name, path = d.RootDirectory.FullName })
                .ToList();
            payload = new { current = "", parent = "", entries = (IEnumerable<object>)drives };
        }
        else
        {
            string full;
            try { full = Path.GetFullPath(raw); }
            catch { WriteError(ctx.Response, "路径无效"); return; }
            if (!Directory.Exists(full)) { WriteError(ctx.Response, "目录不存在"); return; }

            var root = Path.GetPathRoot(full) ?? "";
            var isRoot = string.Equals(full.TrimEnd(Path.DirectorySeparatorChar),
                root.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase);
            var trimmed = full.TrimEnd(Path.DirectorySeparatorChar);
            var parent = isRoot ? "" : Path.GetDirectoryName(trimmed)?.TrimEnd(Path.DirectorySeparatorChar) ?? "";
            if (parent.EndsWith(":")) parent = root;   // "C:" 归一为 "C:\"，避免驱动器相对路径歧义

            string[] dirs = Array.Empty<string>();
            try { dirs = Directory.EnumerateDirectories(full).OrderBy(x => x, StringComparer.OrdinalIgnoreCase).ToArray(); }
            catch (UnauthorizedAccessException) { /* 无权限目录按空处理 */ }
            catch (IOException) { }

            var entries = dirs
                .Select(d => new { name = Path.GetFileName(d), path = d })
                .ToList();
            payload = new
            {
                current = isRoot ? root : trimmed,
                parent,
                entries = (IEnumerable<object>)entries,
            };
        }
        WriteJson(ctx.Response, payload);
    }

    // ───────────────────────── 设置 ─────────────────────────

    object SettingsPayload()
    {
        EnsureCaps();
        return new
        {
            port = _cfg.Port,
            lan = _cfg.Host == "0.0.0.0",
            engine_dir = _cfg.EffectiveEngineDir,
            context_tokens = _cfg.Infer.ContextTokens,
            kv_level = _cfg.Infer.KvLevel,
            flash_attention = _cfg.Infer.FlashAttention,
            ngl = _cfg.Infer.Ngl,
            threads = _cfg.Infer.Threads,
            ubatch = _cfg.Infer.Ubatch,
            parallel = _cfg.Infer.Parallel,
            mtp_steps = _cfg.Infer.MtpSteps,
            extra_args = _cfg.Infer.ExtraArgs,
            auto_trim_ram = _cfg.Run.AutoTrimRam,
            trim_delay_seconds = _cfg.Run.TrimDelaySeconds,
            caps_mtp = _caps.SupportsMtp,
            version = Updater.CurrentVersion,
        };
    }

    async Task HandleSaveSettingsAsync(HttpListenerContext ctx)
    {
        var b = await ReadJsonBodyAsync(ctx.Request);
        if (b.ValueKind != JsonValueKind.Object) { WriteError(ctx.Response, "请求体格式错误"); return; }

        int Num(string name, int min, int max, int fallback)
        {
            if (!b.TryGetProperty(name, out var v) || v.ValueKind != JsonValueKind.Number)
                return fallback;
            var n = v.GetInt32();
            return Math.Clamp(n, min, max);
        }
        bool Bool(string name, bool fb) =>
            b.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False ? v.GetBoolean() : fb;
        string Str(string name, string fb) =>
            b.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? fb : fb;

        _cfg.Port = Num("port", 1024, 65535, _cfg.Port);
        _cfg.Host = Bool("lan", _cfg.Host == "0.0.0.0") ? "0.0.0.0" : "127.0.0.1";

        // 引擎目录：空或等于默认值 → 存空串（跟随安装目录 engine\）
        var submitted = Str("engine_dir", "").Trim();
        var defEngine = Path.TrimEndingDirectorySeparator(
            Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "engine")));
        var submittedFull = submitted.Length == 0 ? "" : Path.TrimEndingDirectorySeparator(Path.GetFullPath(submitted));
        _cfg.EngineDir = submitted.Length == 0 || string.Equals(submittedFull, defEngine, StringComparison.OrdinalIgnoreCase)
            ? "" : submitted;

        _cfg.Infer.ContextTokens = Num("context_tokens", 512, 1048576, _cfg.Infer.ContextTokens);
        var kv = Str("kv_level", _cfg.Infer.KvLevel);
        _cfg.Infer.KvLevel = kv is "f16" or "q8_0" or "q4_0" ? kv : "q4_0";
        _cfg.Infer.FlashAttention = Bool("flash_attention", _cfg.Infer.FlashAttention);
        _cfg.Infer.Ngl = Num("ngl", 0, 999, _cfg.Infer.Ngl);
        _cfg.Infer.Threads = Num("threads", 0, 256, _cfg.Infer.Threads);
        _cfg.Infer.Ubatch = Num("ubatch", 16, 16384, _cfg.Infer.Ubatch);
        _cfg.Infer.Parallel = Num("parallel", 1, 16, _cfg.Infer.Parallel);

        EnsureCaps();
        _cfg.Infer.MtpSteps = _caps.SupportsMtp ? Num("mtp_steps", 0, 10, _cfg.Infer.MtpSteps) : 0;

        _cfg.Infer.ExtraArgs = Str("extra_args", _cfg.Infer.ExtraArgs);
        _cfg.Run.AutoTrimRam = Bool("auto_trim_ram", _cfg.Run.AutoTrimRam);
        _cfg.Run.TrimDelaySeconds = Num("trim_delay_seconds", 5, 3600, _cfg.Run.TrimDelaySeconds);

        ConfigStore.Save(_cfg);
        Log("[配置] 设置已通过控制台保存（运行中的服务重启后生效）");
        BroadcastState();
        WriteOk(ctx.Response);
    }

    // ───────────────────────── 接入信息 ─────────────────────────

    object ComputeConnect()
    {
        const string probeHost = "127.0.0.1";
        var baseUrl = $"http://{probeHost}:{_cfg.Port}/v1";
        var modelId = string.IsNullOrEmpty(_cfg.DefaultModelPath)
            ? "(尚未设置默认模型)"
            : Path.GetFileNameWithoutExtension(_cfg.DefaultModelPath);

        var lan = _cfg.Host == "0.0.0.0";
        var lanIp = lan ? Ui.GetLanIPv4() : null;

        var curl =
            "curl " + baseUrl + "/chat/completions \\\n" +
            "  -H \"Content-Type: application/json\" \\\n" +
            "  -d \"{\\\"model\\\": \\\"" + modelId + "\\\", \\\"messages\\\": [{\\\"role\\\": \\\"user\\\", \\\"content\\\": \\\"你好\\\"}]}\"";

        var python =
            "from openai import OpenAI\n\n" +
            "client = OpenAI(base_url=\"" + baseUrl + "\", api_key=\"none\")\n\n" +
            "resp = client.chat.completions.create(\n" +
            "    model=\"" + modelId + "\",\n" +
            "    messages=[{\"role\": \"user\", \"content\": \"你好\"}],\n" +
            ")\n" +
            "print(resp.choices[0].message.content)";

        return new
        {
            base_url = baseUrl,
            chat_url = baseUrl + "/chat/completions",
            model_id = modelId,
            lan,
            lan_ip = lanIp ?? "",
            lan_url = lan ? $"http://{lanIp ?? "<本机IP>"}:{_cfg.Port}/v1" : "",
            curl,
            python,
        };
    }

    // ───────────────────────── 日志（环形缓冲 + SSE） ─────────────────────────

    /// <summary>应用级日志入口：引擎输出与本程序事件都走这里广播给控制台。</summary>
    public void Log(string line)
    {
        var id = Interlocked.Increment(ref _logId);
        var text = $"{DateTime.Now:HH:mm:ss}  {line}";
        lock (_sseGate)
        {
            _recent.Enqueue(new LogLine(id, text));
            while (_recent.Count > RecentLogCapacity) _recent.Dequeue();
        }
        BroadcastEvent("log", new { id, text });
    }

    void HandleSse(HttpListenerContext ctx)
    {
        var resp = ctx.Response;
        resp.ContentType = "text/event-stream; charset=utf-8";
        resp.Headers.Add("Cache-Control", "no-cache");
        resp.SendChunked = true;
        resp.KeepAlive = true;

        var client = new SseClient(resp);
        lock (_sseGate) _sseClients.Add(client);
        try
        {
            client.Write("retry: 3000\n\n");
            // 挂起等待广播；客户端断开由广播写入失败感知，或服务退出时统一关闭
            Task.Delay(Timeout.Infinite, _cts?.Token ?? CancellationToken.None).Wait();
        }
        catch { /* 正常断开或取消 */ }
        finally
        {
            lock (_sseGate) _sseClients.Remove(client);
            client.Close();
        }
    }

    void HandleRecentLogsQuery(HttpListenerRequest req, HttpListenerResponse resp)
    {
        long after = 0;
        var raw = req.QueryString["after"];
        if (long.TryParse(raw, out var v)) after = v;
        List<LogLine> lines;
        lock (_sseGate)
            lines = _recent.Where(l => l.Id > after).ToList();
        WriteJson(resp, new { lines = lines.Select(l => new { id = l.Id, text = l.Text }) });
    }

    void BroadcastRaw(string sseText)
    {
        SseClient[] snapshot;
        lock (_sseGate) snapshot = _sseClients.ToArray();
        foreach (var c in snapshot) c.TryWrite(sseText);
    }

    void BroadcastEvent(string eventName, object data)
    {
        var json = JsonSerializer.Serialize(data, JsonOpts);
        BroadcastRaw($"event: {eventName}\ndata: {json}\n\n");
    }

    /// <summary>向所有控制台页面广播最新状态（引擎启停、配置变化后调用）。</summary>
    public void BroadcastState() => BroadcastEvent("state", ComputeStatus());

    // ───────────────────────── 软件更新 ─────────────────────────

    sealed record UpdateCheckDto(
        [property: JsonPropertyName("status")] string Status,
        [property: JsonPropertyName("current_version")] string CurrentVersion,
        [property: JsonPropertyName("latest_version")] string LatestVersion,
        [property: JsonPropertyName("notes")] string Notes,
        [property: JsonPropertyName("error")] string? Error);

    async Task HandleUpdateCheckAsync(HttpListenerResponse resp)
    {
        var r = await Updater.CheckAsync();
        if (r.Manifest != null && r.Asset != null)
        {
            _lastManifest = r.Manifest;
            _lastAsset = r.Asset;
        }
        var status = r.Status switch
        {
            UpdateCheckStatus.UpToDate => "up_to_date",
            UpdateCheckStatus.UpdateAvailable => "update_available",
            UpdateCheckStatus.Ignored => "ignored",
            UpdateCheckStatus.TooOld => "too_old",
            _ => "error",
        };
        WriteJson(resp, new UpdateCheckDto(status, r.CurrentVersion, r.LatestVersion, r.Notes, r.Error));
    }

    void HandleUpdateInstall(HttpListenerResponse resp)
    {
        if (_downloading) { WriteError(resp, "已有下载任务进行中", 409); return; }
        if (_lastManifest == null || _lastAsset == null)
        {
            WriteError(resp, "请先检查更新");
            return;
        }
        _downloading = true;
        WriteOk(resp);
        _ = Task.Run(DownloadUpdateAsync);
    }

    async Task DownloadUpdateAsync()
    {
        long lastSentMs = 0;
        int lastPct = -1;
        try
        {
            var manifest = _lastManifest!;
            var asset = _lastAsset!;
            Log($"[更新] 开始下载 v{manifest.Version} 安装包…");
            var progress = new Progress<(long Received, long Total)>(p =>
            {
                var pct = p.Total > 0 ? (int)(p.Received * 100 / p.Total) : 0;
                var now = Environment.TickCount64;
                if (pct == lastPct && now - lastSentMs < 300) return;
                lastPct = pct;
                lastSentMs = now;
                BroadcastEvent("update", new
                {
                    phase = "progress",
                    percent = pct,
                    received_mb = $"{p.Received / 1024d / 1024:N1}",
                    total_mb = $"{p.Total / 1024d / 1024:N1}",
                });
            });
            var path = await Updater.DownloadAssetAsync(manifest, asset, progress, CancellationToken.None);
            _downloadedInstaller = path;
            Log($"[更新] 安装包下载完成: {path}");
            BroadcastEvent("update", new { phase = "done", version = manifest.Version });
        }
        catch (Exception ex)
        {
            Log($"[更新] 下载失败: {ex.Message}");
            BroadcastEvent("update", new { phase = "error", error = ex.Message });
        }
        finally { _downloading = false; }
    }

    void HandleUpdateApply(HttpListenerResponse resp)
    {
        var path = _downloadedInstaller;
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
        {
            WriteError(resp, "尚未下载更新安装包，请先检查更新并下载");
            return;
        }
        try
        {
            Updater.Apply(path);   // 拉起安装器（内部已重校验）
            Log("[更新] 安装器已启动，应用即将退出以释放文件锁…");
            WriteOk(resp);
            _bridge.RequestExit();
        }
        catch (Exception ex)
        {
            WriteError(resp, ex.Message);
        }
    }

    // ───────────────────────── 响应写入 ─────────────────────────

    static void WriteJson(HttpListenerResponse resp, object o, int code = 200)
    {
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(o, JsonOpts));
        resp.StatusCode = code;
        resp.ContentType = "application/json; charset=utf-8";
        resp.ContentLength64 = bytes.Length;
        resp.OutputStream.Write(bytes, 0, bytes.Length);
        resp.OutputStream.Close();
    }

    static void WriteOk(HttpListenerResponse resp) => WriteJson(resp, new { ok = true });

    static void WriteError(HttpListenerResponse resp, string message, int code = 400) =>
        WriteJson(resp, new { error = message }, code);

    /// <summary>一条 SSE 连接的写出端；写入失败即视为客户端断开。</summary>
    sealed class SseClient
    {
        readonly HttpListenerResponse _resp;
        readonly object _gate = new();
        bool _dead;

        public SseClient(HttpListenerResponse resp) { _resp = resp; }

        public void Write(string sseText)
        {
            lock (_gate)
            {
                if (_dead) return;
                var bytes = Encoding.ASCII.GetBytes(sseText);
                try
                {
                    _resp.OutputStream.Write(bytes, 0, bytes.Length);
                    _resp.OutputStream.Flush();
                }
                catch
                {
                    _dead = true;
                    Close();
                }
            }
        }

        public void TryWrite(string sseText)
        {
            if (_dead) return;
            Write(sseText);
        }

        public void Close()
        {
            try { _resp.OutputStream.Close(); } catch { }
        }
    }
}
