using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Serialization;
using a4agent.Core.Config;
using a4agent.Core.Gguf;

namespace a4agent.Core.Engine;

public enum EngineState { Stopped, Starting, Running, Failed }

/// <summary>llama-server 进程管理：启动/停止/健康轮询/定时内存裁剪。</summary>
public sealed class ServerEngine : IDisposable
{
    [DllImport("psapi.dll", SetLastError = true)]
    private static extern bool EmptyWorkingSet(IntPtr hProcess);

    Process? _proc;
    CancellationTokenSource? _pollCts;
    System.Threading.Timer? _trimTimer;
    readonly object _gate = new();

    public EngineState State { get; private set; } = EngineState.Stopped;
    public int LastExitCode;

    public event Action<string>? Log;                 // 后台线程触发，UI 自行调度
    public event Action<EngineState>? StateChanged;

    void SetState(EngineState s)
    {
        State = s;
        StateChanged?.Invoke(s);
    }

    public bool IsBusy => State is EngineState.Starting or EngineState.Running;

    public void Start(AppConfig cfg)
    {
        lock (_gate)
        {
            if (IsBusy) return;

            var engineDir = cfg.EffectiveEngineDir;
            var exe = Path.Combine(engineDir, "llama-server.exe");
            if (!File.Exists(exe))
            {
                Log?.Invoke($"[错误] 未找到引擎: {exe}");
                Log?.Invoke("[提示] 请在 设置 页指定引擎目录（含 llama-server.exe）；轻量版可在首次配置向导里自动下载引擎");
                SetState(EngineState.Failed);
                return;
            }
            if (!IsPortFree(cfg.Port))
            {
                Log?.Invoke($"[错误] 端口 {cfg.Port} 已被占用（可能已有实例在运行）");
                SetState(EngineState.Failed);
                return;
            }

            var model = cfg.DefaultModelPath;
            if (string.IsNullOrEmpty(model) || !File.Exists(model))
            {
                Log?.Invoke("[错误] 未设置有效的默认模型，请先在 模型 页选择");
                SetState(EngineState.Failed);
                return;
            }

            var caps = BackendCapabilities.Detect(engineDir);
            var includeMtp = caps.SupportsMtp;
            if (!caps.SupportsMtp && cfg.Infer.MtpSteps > 0)
                Log?.Invoke("[提示] 当前引擎不支持 MTP（投机解码），MTP 步数将被忽略");
            else if (includeMtp && cfg.Infer.MtpSteps > 0 && !ModelHasMtpHead(model))
            {
                Log?.Invoke("[提示] 当前模型不包含 MTP 层（nextn 张量），MTP 步数将被忽略");
                includeMtp = false;
            }

            var args = CommandLineBuilder.Build(cfg, model, includeMtp, caps.Mtp);

            var psi = new ProcessStartInfo(exe)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                WorkingDirectory = engineDir,
            };
            foreach (var a in args) psi.ArgumentList.Add(a);

            Log?.Invoke($"[启动] {exe}");
            Log?.Invoke("[参数] " + string.Join(' ', args));

            _proc = Process.Start(psi);
            _proc!.OutputDataReceived += (_, e) => { if (e.Data != null) Log?.Invoke(e.Data); };
            _proc.ErrorDataReceived += (_, e) => { if (e.Data != null) Log?.Invoke(e.Data); };
            _proc.BeginOutputReadLine();
            _proc.BeginErrorReadLine();
            SetState(EngineState.Starting);

            _pollCts = new CancellationTokenSource();
            _ = PollHealthAsync(cfg, _proc, _pollCts.Token);
        }
    }

    async Task PollHealthAsync(AppConfig cfg, Process proc, CancellationToken ct)
    {
        var probeHost = cfg.Host is "0.0.0.0" or "::" ? "127.0.0.1" : cfg.Host;
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        var url = $"http://{probeHost}:{cfg.Port}/health";
        var deadline = DateTime.UtcNow.AddMinutes(4);

        while (DateTime.UtcNow < deadline && !ct.IsCancellationRequested)
        {
            if (proc.HasExited)
            {
                LastExitCode = proc.ExitCode;
                Log?.Invoke($"[失败] 服务进程退出，exit code {proc.ExitCode}");
                SetState(EngineState.Failed);
                ScheduleAutoRestart(cfg.Run);
                return;
            }
            try
            {
                using var resp = await http.GetAsync(url, ct);
                if (resp.IsSuccessStatusCode)
                {
                    Log?.Invoke($"[就绪] health ok —— http://{probeHost}:{cfg.Port}/");
                    SetState(EngineState.Running);
                    ScheduleRamTrim(proc, cfg.Run);
                    return;
                }
            }
            catch { /* 还没起来，继续等 */ }
            try { await Task.Delay(1000, ct); } catch (OperationCanceledException) { return; }
        }

        if (!ct.IsCancellationRequested)
        {
            Log?.Invoke("[失败] 等待健康检查超时（4 分钟），请查看日志排查");
            SetState(EngineState.Failed);
        }
    }

    void ScheduleRamTrim(Process proc, RuntimeSettings run)
    {
        if (!run.AutoTrimRam) return;
        _trimTimer?.Dispose();
        var delay = Math.Max(5, run.TrimDelaySeconds) * 1000;
        _trimTimer = new Timer(_ =>
        {
            try
            {
                if (proc.HasExited) return;
                var before = proc.WorkingSet64 / 1024.0 / 1024 / 1024;
                if (EmptyWorkingSet(proc.Handle))
                {
                    proc.Refresh();
                    var after = proc.WorkingSet64 / 1024.0 / 1024 / 1024;
                    Log?.Invoke($"[{DateTime.Now:HH:mm:ss}] 内存裁剪完成: {before:N2}GB -> {after:N2}GB");
                }
            }
            catch (Exception ex) { Log?.Invoke("[裁剪] 失败: " + ex.Message); }
        }, null, delay, Timeout.Infinite);
    }

    void ScheduleAutoRestart(RuntimeSettings run)
    {
        // 预留：崩溃自动重启。v1 默认关闭，避免错误配置导致无限循环。
    }

    public void Stop()
    {
        lock (_gate)
        {
            _pollCts?.Cancel();
            _trimTimer?.Dispose();
            _trimTimer = null;
            var p = _proc;
            if (p != null && !p.HasExited)
            {
                try
                {
                    Log?.Invoke("[停止] 正在关闭 llama-server ...");
                    p.Kill(entireProcessTree: true);
                    p.WaitForExit(8000);
                    Log?.Invoke("[停止] 已关闭");
                }
                catch (Exception ex) { Log?.Invoke("[停止] 异常: " + ex.Message); }
            }
            _proc = null;
            SetState(EngineState.Stopped);
        }
    }

    public void Dispose()
    {
        Stop();
        _pollCts?.Dispose();
    }

    static bool IsPortFree(int port)
    {
        try
        {
            var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, port);
            l.Start(); l.Stop();
            return true;
        }
        catch { return false; }
    }

    /// <summary>MTP（投机解码）需要模型自带 nextn 头；解析失败或无法判定时保守放行，交给引擎自行处理。</summary>
    static bool ModelHasMtpHead(string modelPath)
    {
        try
        {
            var info = GgufParser.Parse(modelPath);
            return !info.Ok || info.HasNextnTensors;
        }
        catch { return true; }
    }
}
