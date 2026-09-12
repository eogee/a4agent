using a4agent.Core.Config;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace a4agent.Core.Update;

public sealed record UpdateAsset(string Name, long Size, string Sha256, string Url);

public sealed record UpdateManifest(
    [property: JsonPropertyName("schema_version")] string SchemaVersion,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("min_version")] string MinVersion,
    [property: JsonPropertyName("prerelease")] bool Prerelease,
    [property: JsonPropertyName("published_at")] string PublishedAt,
    [property: JsonPropertyName("notes")] string Notes,
    [property: JsonPropertyName("notes_url")] string NotesUrl,
    [property: JsonPropertyName("assets")] IReadOnlyList<UpdateAsset> Assets,
    [property: JsonPropertyName("signature")] string Signature);

public enum UpdateCheckStatus { UpToDate, UpdateAvailable, Ignored, TooOld, Error }

public sealed record UpdateCheckResult(
    UpdateCheckStatus Status, string CurrentVersion,
    string LatestVersion = "", string Notes = "", string? Error = null,
    UpdateManifest? Manifest = null, UpdateAsset? Asset = null);

/// <summary>
/// 应用自更新：清单拉取/验签、安装包下载/校验、安装器应用。
/// 清单协议与发布工具 tools/update-manifest.js 严格一致（签名载荷 = 固定字段顺序 +
/// 4 字节大端长度前缀；URL 不入签名，GitHub/Gitee 两份清单共用同一份签名）。
/// 安全设计（对齐 a4api）：
/// - 仅 HTTPS，重定向逐跳校验主机白名单，防跳转钓鱼；
/// - latest.json 由发布侧 Ed25519 私钥签名，内置公钥验签，任何校验不过即作废；
/// - 安装包边下边算 SHA256，与签名清单比对通过才落盘，apply 前再次复核；
/// - 版本比较防降级；预发布仅当当前版本也是预发布时才提示；
/// - 下载写入配置目录的 updates\ 子目录，不信任系统临时目录。
/// </summary>
public static class Updater
{
    // 发布侧签名密钥的公钥（私钥在 .claude/keys/，绝不入库；公钥是公开信息，随应用分发）
    public const string PublicKeyPem = """
        -----BEGIN PUBLIC KEY-----
        MCowBQYDK2VwAyEAVv9XhmESQX/lxw65Lvc0kq+YBkGFG0XusXJXIzsEpfU=
        -----END PUBLIC KEY-----
        """;

    public const string Repo = "eogee/a4agent";
    public const string GithubManifestUrl = $"https://github.com/{Repo}/releases/latest/download/latest.json";
    public const string GiteeApiLatestUrl = $"https://gitee.com/api/v5/repos/{Repo}/releases/latest";
    public const string InstallerPrefix = "a4agent-Lite-setup-v";

    // 签名载荷协议常量（与 tools/update-manifest.js 严格一致，两端必须输出相同字节）
    public const string NamespaceId = "a4agent-update";
    public const string PayloadVersion = "1";
    public const string SchemaVersion = "1";

    public const int ManifestMaxBytes = 512 * 1024;
    public const long MaxDownloadSize = 300L * 1024 * 1024;

    static readonly HashSet<string> ExactHosts = new(StringComparer.OrdinalIgnoreCase)
    { "github.com", "gitee.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com" };

    static readonly System.Text.RegularExpressions.Regex VersionRe = new(@"^\d+\.\d+\.\d+$",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    static readonly System.Text.RegularExpressions.Regex Sha256Re = new(@"^[a-fA-F0-9]{64}$",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    static readonly JsonSerializerOptions JsonOpts = new() { PropertyNameCaseInsensitive = true };

    /// <summary>入口程序集版本（x.y.z）；用于更新比较与展示。</summary>
    public static string CurrentVersion
    {
        get
        {
            var v = Assembly.GetEntryAssembly()?.GetName().Version;
            return v == null ? "0.0.0" : $"{v.Major}.{v.Minor}.{v.Build}";
        }
    }

    // ───────────────────────── 版本比较 ─────────────────────────

    public static (int Major, int Minor, int Patch)? ParseVersion(string? v) =>
        v != null && VersionRe.IsMatch(v)
            ? (int.Parse(v.Split('.')[0]), int.Parse(v.Split('.')[1]), int.Parse(v.Split('.')[2]))
            : null;

    /// <summary>a&gt;b 返回 1，相等 0，a&lt;b 返回 -1；任一非法格式返回 0。</summary>
    public static int Compare(string? a, string? b)
    {
        var pa = ParseVersion(a); var pb = ParseVersion(b);
        if (pa == null || pb == null) return 0;
        return pa.Value.CompareTo(pb.Value);
    }

    /// <summary>取版本号数字核心：'0.2.0-beta' → '0.2.0'，预发布当前版本也能参与比较。</summary>
    public static string NumericCore(string v)
    {
        var m = System.Text.RegularExpressions.Regex.Match(v ?? "", @"^(\d+\.\d+\.\d+)");
        return m.Success ? m.Groups[1].Value : v ?? "";
    }

    // ───────────────────────── 签名载荷与清单校验 ─────────────────────────

    static void Push(List<byte> buf, string s)
    {
        var data = Encoding.UTF8.GetBytes(s);
        buf.Add((byte)(data.Length >> 24)); buf.Add((byte)(data.Length >> 16));
        buf.Add((byte)(data.Length >> 8)); buf.Add((byte)data.Length);
        buf.AddRange(data);
    }

    /// <summary>构造签名载荷：与发布工具逐字节一致；URL 不在签名范围。</summary>
    public static byte[] BuildPayload(UpdateManifest m)
    {
        var buf = new List<byte>(512);
        Push(buf, NamespaceId);
        Push(buf, PayloadVersion);
        Push(buf, m.SchemaVersion);
        Push(buf, m.Version);
        Push(buf, string.IsNullOrEmpty(m.MinVersion) ? "0.0.0" : m.MinVersion);
        Push(buf, m.PublishedAt ?? "");
        Push(buf, m.Prerelease ? "1" : "0");
        Push(buf, m.Notes ?? "");
        Push(buf, m.NotesUrl ?? "");
        foreach (var a in m.Assets.OrderBy(x => x.Name, StringComparer.Ordinal))
        {
            Push(buf, a.Name);
            Push(buf, a.Sha256.ToLowerInvariant());
            Push(buf, a.Size.ToString(System.Globalization.CultureInfo.InvariantCulture));
        }
        return buf.ToArray();
    }

    /// <summary>从内置 PEM 提取 Ed25519 裸公钥（SPKI 固定 12 字节头 + 32 字节公钥）。</summary>
    static byte[]? LoadPublicKeyRaw()
    {
        try
        {
            const string begin = "-----BEGIN PUBLIC KEY-----";
            const string end = "-----END PUBLIC KEY-----";
            var pem = PublicKeyPem.ReplaceLineEndings("");
            int i = pem.IndexOf(begin, StringComparison.Ordinal);
            int j = pem.IndexOf(end, StringComparison.Ordinal);
            if (i < 0 || j < 0) return null;
            var der = Convert.FromBase64String(pem[(i + begin.Length)..j].Trim());
            return der.Length == 44 ? der[12..] : null;
        }
        catch { return null; }
    }

    static bool AllowedHost(string? host)
    {
        host = (host ?? "").ToLowerInvariant();
        if (host.Length == 0) return false;
        if (ExactHosts.Contains(host)) return true;
        return host.EndsWith(".githubusercontent.com", StringComparison.OrdinalIgnoreCase)
            || host.EndsWith(".gitee.com", StringComparison.OrdinalIgnoreCase);
    }

    static string HostOf(string? url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var u) ? u.Host.Split(':')[0] : "";

    /// <summary>严格校验清单字段类型与取值；不合法一律视为签名无效。</summary>
    public static bool ValidateManifest(UpdateManifest? m)
    {
        try
        {
            if (m == null || m.SchemaVersion != SchemaVersion) return false;
            if (ParseVersion(m.Version) == null) return false;
            if (!VersionRe.IsMatch(string.IsNullOrEmpty(m.MinVersion) ? "0.0.0" : m.MinVersion)) return false;
            if (m.Assets == null || m.Assets.Count == 0 || m.Assets.Count > 4) return false;
            var seen = new HashSet<(string, string)>();
            foreach (var a in m.Assets)
            {
                if (a == null || string.IsNullOrEmpty(a.Name)) return false;
                if (!AllowedHost(HostOf(a.Url))) return false;
                if (!seen.Add((a.Name, a.Url))) return false;   // 同名同 URL 拒绝；同名不同镜像允许
                if (a.Sha256 == null || !Sha256Re.IsMatch(a.Sha256.Trim())) return false;
                if (a.Size <= 0 || a.Size > MaxDownloadSize) return false;
            }
            return true;
        }
        catch { return false; }
    }

    /// <summary>Ed25519 验签：先字段严格校验，再重建签名载荷比对；失败即拒。</summary>
    public static bool VerifySignature(UpdateManifest? m)
    {
        if (!ValidateManifest(m)) return false;
        if (string.IsNullOrEmpty(m!.Signature)) return false;
        try
        {
            var pk = LoadPublicKeyRaw();
            if (pk == null) return false;
            var sig = Convert.FromBase64String(m.Signature);
            var payload = BuildPayload(m);
            return Org.BouncyCastle.Math.EC.Rfc8032.Ed25519.Verify(sig, 0, pk, 0, payload, 0, payload.Length);
        }
        catch { return false; }
    }

    public static UpdateManifest? ParseManifest(string json)
    {
        try { return JsonSerializer.Deserialize<UpdateManifest>(json, JsonOpts); }
        catch { return null; }
    }

    // ───────────────────────── 状态持久化（忽略版本 / 已下载待安装） ─────────────────────────

    static string StatePath() =>
        Path.Combine(Path.GetDirectoryName(ConfigStore.ResolvePath())!, "update_state.json");

    public sealed class UpdateState
    {
        [JsonPropertyName("ignored")]
        public List<string> Ignored { get; set; } = new();
        [JsonPropertyName("downloaded")]
        public DownloadedEntry? Downloaded { get; set; }
    }

    public sealed class DownloadedEntry
    {
        [JsonPropertyName("version")] public string Version { get; set; } = "";
        [JsonPropertyName("path")] public string Path { get; set; } = "";
        [JsonPropertyName("sha256")] public string Sha256 { get; set; } = "";
        [JsonPropertyName("size")] public long Size { get; set; }
    }

    static readonly object StateLock = new();

    public static UpdateState ReadState()
    {
        lock (StateLock)
        {
            try
            {
                return JsonSerializer.Deserialize<UpdateState>(File.ReadAllText(StatePath()), JsonOpts)
                       ?? new UpdateState();
            }
            catch { return new UpdateState(); }
        }
    }

    public static void WriteState(UpdateState state)
    {
        lock (StateLock)
        {
            try
            {
                var path = StatePath();
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
                var tmp = path + ".tmp";
                File.WriteAllText(tmp, JsonSerializer.Serialize(state,
                    new JsonSerializerOptions { WriteIndented = true }));
                File.Move(tmp, path, overwrite: true);
            }
            catch { /* 状态写失败不阻断主流程 */ }
        }
    }

    public static bool IsIgnored(string version) => ReadState().Ignored.Contains(version);

    public static void IgnoreVersion(string version)
    {
        var st = ReadState();
        if (!st.Ignored.Contains(version)) { st.Ignored.Add(version); WriteState(st); }
    }

    // ───────────────────────── 网络拉取（逐跳 host 白名单） ─────────────────────────

    static HttpClient NewClient(long timeoutMs) => new(new HttpClientHandler
    {
        AllowAutoRedirect = false,   // 重定向手动处理，逐跳校验
        UseCookies = false,
    })
    { Timeout = TimeSpan.FromMilliseconds(timeoutMs) };

    static async Task<HttpResponseMessage> SendWithHostCheckAsync(HttpClient http, string url, CancellationToken ct)
    {
        for (int hop = 0; hop < 6; hop++)
        {
            if (!AllowedHost(HostOf(url)))
                throw new IOException($"blocked host: {HostOf(url)}");
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.UserAgent.ParseAdd("a4agent-updater/1.0");
            var resp = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
            if ((int)resp.StatusCode is 301 or 302 or 303 or 307 or 308)
            {
                var loc = resp.Headers.Location?.ToString();
                resp.Dispose();
                if (string.IsNullOrEmpty(loc)) throw new IOException("redirect without location");
                url = new Uri(new Uri(url), loc).ToString();
                continue;
            }
            if (!resp.IsSuccessStatusCode)
            {
                resp.Dispose();
                throw new IOException($"HTTP {(int)resp.StatusCode}");
            }
            if (!AllowedHost(HostOf(resp.RequestMessage?.RequestUri?.ToString() ?? url)))
            {
                resp.Dispose();
                throw new IOException("final host not allowed");
            }
            return resp;
        }
        throw new IOException("too many redirects");
    }

    static async Task<string> HttpGetStringBoundedAsync(string url, int maxBytes, int timeoutMs, CancellationToken ct)
    {
        using var http = NewClient(timeoutMs);
        using var resp = await SendWithHostCheckAsync(http, url, ct);
        await using var s = await resp.Content.ReadAsStreamAsync(ct);
        var buf = new MemoryStream();
        var chunk = new byte[64 * 1024];
        int n, total = 0;
        while ((n = await s.ReadAsync(chunk, ct)) > 0)
        {
            total += n;
            if (total > maxBytes) throw new IOException("response too large");
            buf.Write(chunk, 0, n);
        }
        return Encoding.UTF8.GetString(buf.ToArray());
    }

    static async Task<(string? Json, Exception? Err)> TryFetch(Func<CancellationToken, Task<string>> fetch, CancellationToken ct)
    {
        try { return (await fetch(ct), null); }
        catch (Exception e) { return (null, e); }
    }

    /// <summary>并行拉取 GitHub / Gitee 清单，首个「解析且验签通过」的胜出（国内 Gitee 通常先回）。</summary>
    public static async Task<UpdateManifest> FetchManifestAsync(CancellationToken ct)
    {
        var tasks = new List<Task<(string? Json, Exception? Err)>>
        {
            TryFetch(FetchGithubManifestAsync, ct),
            TryFetch(FetchGiteeManifestAsync, ct),
        };
        Exception? last = null;
        while (tasks.Count > 0)
        {
            var done = await Task.WhenAny(tasks);
            tasks.Remove(done);
            var (json, err) = await done;
            if (err != null) { last = err; continue; }
            var m = ParseManifest(json!);
            if (m != null && VerifySignature(m)) return m;
            last = new IOException("manifest signature invalid");
        }
        throw last ?? new IOException("update manifest unreachable");
    }

    static Task<string> FetchGithubManifestAsync(CancellationToken ct) =>
        HttpGetStringBoundedAsync(GithubManifestUrl, ManifestMaxBytes, 8000, ct);

    static async Task<string> FetchGiteeManifestAsync(CancellationToken ct)
    {
        // Gitee 无 latest 别名：公开 API 取最新 release 的 tag，再拼清单地址
        var apiJson = await HttpGetStringBoundedAsync(GiteeApiLatestUrl, ManifestMaxBytes, 8000, ct);
        var tag = JsonSerializer.Deserialize<JsonElement>(apiJson).TryGetProperty("tag_name", out var t)
            ? t.GetString() : null;
        if (string.IsNullOrEmpty(tag)) throw new IOException("gitee latest release has no tag");
        return await HttpGetStringBoundedAsync(
            $"https://gitee.com/{Repo}/releases/download/{tag}/latest.json", ManifestMaxBytes, 8000, ct);
    }

    // ───────────────────────── 更新检查 ─────────────────────────

    public static string InstallerName(string version) => $"{InstallerPrefix}{version}.exe";

    public static UpdateAsset? AssetForVersion(UpdateManifest m, string version) =>
        m.Assets.FirstOrDefault(a => a.Name == InstallerName(version));

    public static async Task<UpdateCheckResult> CheckAsync()
    {
        var current = CurrentVersion;
        var cmpCurrent = NumericCore(current);
        UpdateManifest m;
        try { m = await FetchManifestAsync(CancellationToken.None); }
        catch (Exception e) { return new UpdateCheckResult(UpdateCheckStatus.Error, current, Error: e.Message); }

        var candidate = m.Version;
        if (Compare(candidate, cmpCurrent) <= 0)
            return new UpdateCheckResult(UpdateCheckStatus.UpToDate, current, candidate, m.Notes);
        if (Compare(cmpCurrent, m.MinVersion) < 0)
            return new UpdateCheckResult(UpdateCheckStatus.TooOld, current, candidate,
                Error: $"当前版本过旧，请直接下载完整安装包更新到 {candidate}");
        if (m.Prerelease && VersionRe.IsMatch(current))
            return new UpdateCheckResult(UpdateCheckStatus.UpToDate, current, candidate, m.Notes);
        if (IsIgnored(candidate))
            return new UpdateCheckResult(UpdateCheckStatus.Ignored, current, candidate, m.Notes);

        var asset = AssetForVersion(m, candidate);
        if (asset == null)
            return new UpdateCheckResult(UpdateCheckStatus.Error, current, candidate,
                Error: $"更新清单中找不到版本 {candidate} 的安装包");
        return new UpdateCheckResult(UpdateCheckStatus.UpdateAvailable, current, candidate, m.Notes, Manifest: m, Asset: asset);
    }

    // ───────────────────────── 下载 ─────────────────────────

    static string Sha256Of(string path)
    {
        using var h = SHA256.Create();
        using var f = File.OpenRead(path);
        return Convert.ToHexString(h.ComputeHash(f)).ToLowerInvariant();
    }

    /// <summary>下载安装包（GitHub → Gitee 顺序回退），边下边算 SHA256；校验通过才落盘。</summary>
    public static async Task<string> DownloadAssetAsync(UpdateManifest m, UpdateAsset asset,
        IProgress<(long Received, long Total)>? progress, CancellationToken ct)
    {
        var baseDir = Path.Combine(Path.GetDirectoryName(ConfigStore.ResolvePath())!, "updates", m.Version);
        Directory.CreateDirectory(baseDir);
        var finalPath = Path.Combine(baseDir, asset.Name);
        var partPath = finalPath + ".part";

        // 已存在且校验通过 → 直接复用（覆盖"下载完成后重启再装"的场景）
        if (File.Exists(finalPath))
        {
            if (Sha256Of(finalPath) == asset.Sha256.ToLowerInvariant() && new FileInfo(finalPath).Length == asset.Size)
                return finalPath;
            File.Delete(finalPath);
        }

        Exception? last = null;
        foreach (var url in new[] { asset.Url, GiteeUrlFor(asset) }.Distinct())
        {
            try
            {
                await DownloadFileAsync(url, partPath, asset, progress, ct);
                File.Move(partPath, finalPath, overwrite: true);
                var state = ReadState();
                state.Downloaded = new DownloadedEntry
                { Version = m.Version, Path = finalPath, Sha256 = asset.Sha256.ToLowerInvariant(), Size = asset.Size };
                WriteState(state);
                return finalPath;
            }
            catch (OperationCanceledException) { TryDelete(partPath); throw; }
            catch (Exception e) { last = e; TryDelete(partPath); }
        }
        throw last ?? new IOException("下载失败");

        static void TryDelete(string p) { try { if (File.Exists(p)) File.Delete(p); } catch { } }
    }

    /// <summary>同名镜像：GitHub 资产 URL 替换主机与仓库段 → Gitee 下载地址。</summary>
    static string GiteeUrlFor(UpdateAsset asset)
    {
        if (Uri.TryCreate(asset.Url, UriKind.Absolute, out var u) && u.Host == "github.com")
        {
            var parts = u.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
            // eogee / a4agent / releases / download / vX.Y.Z / name
            if (parts.Length >= 6)
                return $"https://gitee.com/{parts[0]}/{parts[1]}/releases/download/{parts[4]}/{parts[5]}";
        }
        return $"https://gitee.com/{Repo}/releases/download/v{asset.Name.Replace(InstallerPrefix, "")}/{asset.Name}";
    }

    static async Task DownloadFileAsync(string url, string dest, UpdateAsset asset,
        IProgress<(long Received, long Total)>? progress, CancellationToken ct)
    {
        using var http = NewClient(Timeout.Infinite);
        using var resp = await SendWithHostCheckAsync(http, url, ct);
        await using var src = await resp.Content.ReadAsStreamAsync(ct);
        using var hasher = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        long received = 0;
        var buf = new byte[64 * 1024];
        await using var fs = new FileStream(dest, FileMode.Create, FileAccess.Write, FileShare.None);
        int n;
        while ((n = await src.ReadAsync(buf, ct)) > 0)
        {
            ct.ThrowIfCancellationRequested();
            hasher.AppendData(buf, 0, n);
            received += n;
            if (received > MaxDownloadSize) throw new IOException("download too large");
            await fs.WriteAsync(buf.AsMemory(0, n), ct);
            progress?.Report((received, asset.Size));
        }
        if (received != asset.Size) throw new IOException($"size mismatch: expected {asset.Size}, got {received}");
        var hex = Convert.ToHexString(hasher.GetHashAndReset()).ToLowerInvariant();
        if (hex != asset.Sha256.ToLowerInvariant()) throw new IOException("sha256 mismatch");
    }

    // ───────────────────────── 应用更新 ─────────────────────────

    /// <summary>应用更新：重校验磁盘安装包 → 拉起安装器（调用方随后停止引擎并退出应用）。</summary>
    public static string Apply(string installerPath)
    {
        if (string.IsNullOrEmpty(installerPath) || !File.Exists(installerPath))
            throw new IOException("安装包文件不存在，请重新下载");
        var marker = ReadState().Downloaded;
        if (marker != null && marker.Path == installerPath)
        {
            if (!string.IsNullOrEmpty(marker.Sha256) && Sha256Of(installerPath) != marker.Sha256.ToLowerInvariant())
                throw new IOException("安装包校验失败，请重新下载");
            if (marker.Size > 0 && new FileInfo(installerPath).Length != marker.Size)
                throw new IOException("安装包大小不符，请重新下载");
        }
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(installerPath) { UseShellExecute = true });
        return installerPath;
    }
}
