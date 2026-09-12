using System.Net.Http;
using System.Text.Json;
using a4agent.Core.Config;
using a4agent.Core.Engine;
using a4agent.Core.Gguf;
using a4agent.Core.Gpu;
using a4agent.Core.Update;

// ── 0. 更新模块自检：--updatetest 只跑更新协议测试，跳过显卡/GGUF/引擎冒烟 ──
if (args.Contains("--updatetest"))
{
    return RunUpdateSelfTest();
}

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

// ── 更新模块自检：版本比较 + 与 node 发布端（tools/update-manifest.js）跨语言签名互验 ──
// 固定测试清单的签名由私钥对生成；改动清单任何字段或载荷算法都会使互验失败——
// 那正是该测试要拦住的事故（两端载荷协议不再一致）。
static int RunUpdateSelfTest()
{
    int fails = 0;
    void Assert(bool ok, string name)
    {
        Console.WriteLine($"  {(ok ? "[OK]  " : "[FAIL]")} {name}");
        if (!ok) fails++;
    }

    Console.WriteLine("[UPDATE] 版本比较");
    Assert(Updater.Compare("0.2.1", "0.2.0") == 1, "0.2.1 > 0.2.0");
    Assert(Updater.Compare("0.2.0", "0.2.0") == 0, "0.2.0 == 0.2.0");
    Assert(Updater.Compare("0.1.9", "0.2.0") == -1, "0.1.9 < 0.2.0");
    Assert(Updater.Compare("abc", "0.2.0") == 0, "非法版本比较返回 0");
    Assert(Updater.NumericCore("0.2.0-beta") == "0.2.0", "预发布版本剥离数字核心");

    Console.WriteLine("[UPDATE] 清单验签（node 签名 → C# 验签）");
    const string nodeSig = "7s6zDIImx5yPcw3kRK4DXqPJGORWjVIPxfiWI4wOjAvG+E6fla0V+oikWGRMOx2xHTmSToWl3KJuSLLmWeH3AA==";
    const string payloadSha256 = "62247B9FAAC54ACB26B8DE6EDECA04DE0F2FCF538C4CE87CB4CE8FD07957E4D6";
    var manifest = new UpdateManifest(
        "1", "0.0.2", "0.0.0", false, "2026-09-12T00:00:00Z", "smoke fixture",
        "https://github.com/eogee/a4agent/releases/tag/v0.0.2",
        new[] { new UpdateAsset(
            "a4agent-Lite-setup-v0.0.2.exe", 1048576, new string('a', 64),
            "https://github.com/eogee/a4agent/releases/download/v0.0.2/a4agent-Lite-setup-v0.0.2.exe") },
        nodeSig);

    var payload = Updater.BuildPayload(manifest);
    var payloadHash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(payload));
    Assert(payloadHash == payloadSha256, $"载荷字节与 node 端一致（sha256={payloadHash[..12]}…）");
    Assert(Updater.VerifySignature(manifest), "node 签名验签通过");
    Assert(!Updater.VerifySignature(manifest with { Version = "0.0.3" }), "篡改 version 拒签");
    Assert(!Updater.VerifySignature(manifest with { Notes = "tampered" }), "篡改 notes 拒签");
    Assert(!Updater.VerifySignature(manifest with
    {
        Assets = new[] { manifest.Assets[0] with { Sha256 = new string('b', 64) } }
    }), "篡改资产 sha256 拒签");
    Assert(!Updater.VerifySignature(manifest with { Signature = Convert.ToBase64String(new byte[64]) }), "伪造签名拒收");
    Assert(!Updater.VerifySignature(manifest with { SchemaVersion = "9" }), "未知 schema 拒收");
    Assert(!Updater.ValidateManifest(manifest with
    {
        Assets = new[] { manifest.Assets[0] with { Url = "https://evil.example.com/x.exe" } },
        Signature = nodeSig,
    }), "非白名单下载域拒收");

    Console.WriteLine($"\n===== UPDATE SELFTEST {(fails == 0 ? "PASS" : "FAIL")} =====");
    return fails == 0 ? 0 : 1;
}

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
Console.WriteLine($"\n===== SMOKE TEST {(pass ? "PASS" : "FAIL")} =====");
Environment.Exit(pass ? 0 : 1);
return pass ? 0 : 1;
