using System.Diagnostics;
using a4agent.Core.Config;

namespace a4agent.Core.Engine;

/// <summary>启发式 MTP（投机解码）应使用的命令行写法。</summary>
public enum MtpFlagStyle
{
    /// <summary>引擎不支持投机解码。</summary>
    None,
    /// <summary>旧版写法：--mtp N（早期 llama.cpp）。</summary>
    Legacy,
    /// <summary>新版 spec 体系：--spec-type draft-mtp --spec-draft-n-max N。</summary>
    SpecType,
}

public sealed class BackendCapabilities
{
    public bool SupportsMtp => Mtp != MtpFlagStyle.None;
    public MtpFlagStyle Mtp = MtpFlagStyle.None;
    public bool HelpProbeDone;

    /// <summary>跑一次 llama-server --help，探测当前后端支持哪些特性（结果缓存）。</summary>
    public static BackendCapabilities Detect(string engineDir)
    {
        var caps = new BackendCapabilities();
        try
        {
            var exe = Path.Combine(engineDir, "llama-server.exe");
            if (!File.Exists(exe)) return caps;
            var psi = new ProcessStartInfo(exe, "--help")
            {
                UseShellExecute = false, RedirectStandardOutput = true,
                RedirectStandardError = true, CreateNoWindow = true,
            };
            using var p = Process.Start(psi)!;
            // --help 会以非零码退出，但输出内容有效；等它写完
            var text = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
            p.WaitForExit(10000);
            // 旧版 llama.cpp 用 --mtp N；新版（b105xx 起）移除了 --mtp，
            // 投机解码改为 --spec-type 体系，MTP 是其中的 draft-mtp 类型。
            caps.Mtp = text.Contains("--mtp", StringComparison.Ordinal)
                ? MtpFlagStyle.Legacy
                : text.Contains("draft-mtp", StringComparison.Ordinal)
                    ? MtpFlagStyle.SpecType
                    : MtpFlagStyle.None;
            caps.HelpProbeDone = true;
        }
        catch { /* 探测失败按最保守处理 */ }
        return caps;
    }
}

/// <summary>把配置拼装成 llama-server 命令行参数（与现有 bat 行为对齐）。</summary>
public static class CommandLineBuilder
{
    public static List<string> Build(AppConfig cfg, string modelPath, bool includeMtp, MtpFlagStyle mtpStyle = MtpFlagStyle.Legacy)
    {
        var i = cfg.Infer;
        var args = new List<string>
        {
            "-m", modelPath,
            "--host", cfg.Host,
            "--port", cfg.Port.ToString(),
            "-ngl", i.Ngl.ToString(),
            "-c", i.ContextTokens.ToString(),
            "--alias", Path.GetFileNameWithoutExtension(modelPath),
            "--parallel", Math.Max(1, i.Parallel).ToString(),
            "-fa", i.FlashAttention ? "on" : "off",
            "--reasoning", "off",
            "--reasoning-budget", "0",
            "-ctk", i.KvLevel,
            "-ctv", i.KvLevel,
        };
        if (i.Ubatch > 0 && i.Ubatch != 512) { args.Add("-ub"); args.Add(i.Ubatch.ToString()); }
        if (i.Threads > 0) { args.Add("-t"); args.Add(i.Threads.ToString()); }
        if (includeMtp && i.MtpSteps > 0)
        {
            switch (mtpStyle)
            {
                case MtpFlagStyle.Legacy:
                    args.Add("--mtp"); args.Add(i.MtpSteps.ToString());
                    break;
                case MtpFlagStyle.SpecType:
                    // 新版 llama.cpp：--mtp 已移除，MTP 是 --spec-type 的 draft-mtp 类型；
                    // --spec-draft-n-max 控制每步草稿 token 数（即 MTP 步数）。
                    args.Add("--spec-type"); args.Add("draft-mtp");
                    args.Add("--spec-draft-n-max"); args.Add(i.MtpSteps.ToString());
                    break;
            }
        }

        foreach (var a in i.ExtraArgs.Split(' ', StringSplitOptions.RemoveEmptyEntries))
            args.Add(a);
        return args;
    }
}
