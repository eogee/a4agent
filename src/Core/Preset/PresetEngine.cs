using a4agent.Core.Config;
using a4agent.Core.Gguf;

namespace a4agent.Core.Preset;

public sealed record Preset(string Label, int ContextTokens, string KvLevel, string Note);

/// <summary>按显存档位给出预定义推理配置，并提供 OOM 风险评估。</summary>
public static class PresetEngine
{
    // 预设依据：Ornith 系列为混合架构（full_attention_interval=4），
    // 仅约 1/4 层携带 KV 且每层只有 2 个 KV 头 —— q4_0 下每千 token 约 5.6KB，
    // 上下文的显存代价极小，预设可以给得激进；权重才是大头。
    public static Preset Recommend(double vramGb, bool hasDiscreteGpu)
    {
        if (!hasDiscreteGpu || vramGb < 2)
            return new Preset("CPU 兜底", 8192, "q4_0",
                "未检测到可用独显，将走 CPU 推理（速度受限），强烈建议搭配 Ornith-1.5-9B");
        if (vramGb < 6)
            return new Preset("入门档 (<6GB)", 8192, "q4_0",
                "该档位瓶颈是权重本身：仅够 9B 全量 offload，上下文没有加码空间；35B 请勿尝试");
        if (vramGb < 12)
            return new Preset("主流档 (6–12GB)", 32768, "q4_0",
                "9B 全量 offload + 32k 绰绰有余（KV 仅 ~0.2GB）；跑 35B 需换 Q3 量化并把上下文降回 16k");
        if (vramGb < 16)
            return new Preset("进阶档 (12–16GB)", 32768, "q4_0",
                "27B / 35B 小量化可跑 32k；9B 可手动上探 64k");
        if (vramGb < 24)
            return new Preset("高端档 (16–24GB)", 65536, "q4_0",
                "35B-IQ4_XS 全量 offload + 64k 有余量；实测 22GB 卡跑 128k 也只到 ~21.9GB，激进场景可手动拉满");
        return new Preset("旗舰档 (≥24GB)", 131072, "q8_0",
            "35B-IQ4_XS 128k 从容且 KV 可升 q8_0 提升长文召回；理论上限可试原生 256k");
    }

    /// <summary>依据 GGUF 元数据估算 显存需求 vs 实际显存，返回警告文案或 null。</summary>
    public static string? AssessRisk(GgufModelInfo model, InferParams p, double vramGb, bool hasGpu)
    {
        if (!model.Ok || vramGb <= 0 || !hasGpu) return null;
        var need = model.EstimateVramGb(p.ContextTokens, p.KvLevel);
        if (need > vramGb * 0.97)
            return $"⚠ 预计需要 {need:F1} GB / 显存 {vramGb:F1} GB —— 大概率 OOM，请降低上下文长度或 KV 档位";
        if (need > vramGb * 0.88)
            return $"⚡ 预计需要 {need:F1} GB / 显存 {vramGb:F1} GB —— 余量偏小，运行时请关闭占用显存的程序";
        return null;
    }
}
