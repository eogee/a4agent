using System.Net.Http;
using System.Text.Json;
using a4agent.Core.Config;
using a4agent.Core.Engine;
using a4agent.Core.Gguf;
using a4agent.Core.Gpu;

// ── 1. 显卡检测 ──────────────────────────────────────────
var gpus = GpuDetector.Detect();
foreach (var g in gpus)
    Console.WriteLine($"[GPU] [{g.Vendor}] {g.Name}  {g.VramGb:N1} GB");
var best = GpuDetector.Best();
Console.WriteLine(best == null ? "[GPU] 未检测到显卡" : $"[GPU] best -> {best.Name}");

// ── 2. GGUF 解析 ─────────────────────────────────────────
var files = new[]
{
    @"C:\models\Ornith-1.5-35B\Ornith-1.5-35B-A3B-IQ4_XS.gguf",
    @"C:\models\Ornith-1.5-9B",
};
var iq4 = GgufParser.Parse(files[0]);
Console.WriteLine($"\n[GGUF] {Path.GetFileName(iq4.FilePath)}");
Console.WriteLine($"  ok={iq4.Ok} err={iq4.Error}");
Console.WriteLine($"  arch={iq4.Arch} layers={iq4.Layers} heads={iq4.HeadCount} kvHeads={iq4.HeadCountKv}");
Console.WriteLine($"  klen={iq4.KeyLength} vlen={iq4.ValueLength} nativeCtx={iq4.NativeContext}");
Console.WriteLine($"  interval={iq4.FullAttnInterval} nextn={iq4.NextnPredictLayers} hasNextnTensors={iq4.HasNextnTensors}");
Console.WriteLine($"  quant={iq4.QuantLabel} size={iq4.FileSize / (1024d * 1024 * 1024):N2} GB");
Console.WriteLine($"  KV@f16 = {iq4.KvBytesPerTokenF16 / 1024.0:F1} KB/tok ({iq4.EffectiveKvLayers} 个全注意力层)");
Console.WriteLine($"  预估显存 @128k q4_0: {iq4.EstimateVramGb(131072, "q4_0"):F2} GB");

if (Directory.Exists(files[1]))
{
    var m9 = UiScan(files[1]);
    foreach (var m in m9.Take(3))
        Console.WriteLine($"[GGUF] {Path.GetFileName(m.FilePath)}  {m.QuantLabel}  ctx={m.NativeContext}  ok={m.Ok}");
}

static List<GgufModelInfo> UiScan(string dir) =>
    Directory.EnumerateFiles(dir, "*.gguf").Select(GgufParser.Parse).ToList();

// ── 3. 引擎端到端：8099 起服务 → health → 停止 ───────────
const int port = 8099;
var cfg = new AppConfig
{
    Port = port,
    Host = "127.0.0.1",
    EngineDir = @"C:\ProgramMine\llama-cpp",
    DefaultModelPath = iq4.FilePath,
    WizardDone = true,
};
cfg.Infer.ContextTokens = 8192; // 冒烟用小上下文，起得快、占得少

using var engine = new ServerEngine();
engine.Log += l => Console.WriteLine("   | " + l);

var done = new TaskCompletionSource<EngineState>(TaskCreationOptions.RunContinuationsAsynchronously);
engine.StateChanged += s =>
{
    if (s is EngineState.Running or EngineState.Failed) done.TrySetResult(s);
};

Console.WriteLine($"\n[ENGINE] 在端口 {port} 启动 llama-server ...");
engine.Start(cfg);
var sw = System.Diagnostics.Stopwatch.StartNew();

var finalState = await done.Task.WaitAsync(TimeSpan.FromSeconds(180));
Console.WriteLine($"[ENGINE] 结果状态: {finalState}（耗时 {sw.Elapsed.TotalSeconds:N0}s）");

// 再独立验证一次 HTTP
bool httpOk = false;
if (finalState == EngineState.Running)
{
    using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
    try
    {
        var json = await http.GetStringAsync($"http://127.0.0.1:{port}/health");
        httpOk = json.Contains("ok", StringComparison.OrdinalIgnoreCase);
        Console.WriteLine($"[HTTP] /health => {json.Trim()}  httpOk={httpOk}");
    }
    catch (Exception ex) { Console.WriteLine($"[HTTP] 探测失败: {ex.Message}"); }
}

await Task.Delay(1500); // 给裁剪定时器一点时间（90s 才触发，这里只验证进程活着）

Console.WriteLine("\n[ENGINE] 停止...");
engine.Stop();
await Task.Delay(500);
Console.WriteLine($"[ENGINE] 最终状态: {engine.State}");

var pass = finalState == EngineState.Running && httpOk && engine.State == EngineState.Stopped;
Console.WriteLine($"\n===== SMOKE TEST {(pass ? "PASS ✅" : "FAIL ❌")} =====");
Environment.Exit(pass ? 0 : 1);
