using a4agent.Core.Gpu;

namespace a4agent.Core.Engine;

/// <summary>
/// 一个可自动下载的引擎包：对应 llama.cpp 官方 Release 的一个或多个 zip
/// （CUDA 主包 + cudart 运行库包）。安装包（Lite 版）不再内置引擎，由向导按需获取。
/// </summary>
public sealed record EnginePack(
    string Id,                // cuda133 / cuda124 / vulkan / cpu
    string Label,             // 显示名（不含体积）
    string Note,              // 一行适配说明
    string[] Zips,            // 需依次下载的官方 zip 资产名
    long TotalBytes,          // 所有 zip 体积合计，用于界面展示
    string MinNvidiaDriver,   // NVIDIA 最低驱动大版本号（空 = 不要求）
    bool NeedsNvidia)         // true = 仅 NVIDIA 显卡可用
{
    public string SizeLabel => TotalBytes >= 512 * 1024 * 1024
        ? $"{TotalBytes / (1024d * 1024 * 1024):N1} GB"
        : $"{TotalBytes / (1024d * 1024 * 1024):N0} MB";
}

/// <summary>钉定版本的引擎包目录。升级 llama.cpp：改 RepoTag、
/// 重新核对资产名与体积（release 页面）后同步更新即可。</summary>
public static class EngineCatalog
{
    public const string RepoOwner = "ggml-org";
    public const string RepoName = "llama.cpp";
    public const string RepoTag = "b10919";   // 2026-09-12 发布

    public static readonly List<EnginePack> Packs = new()
    {
        new("cuda133",
            "CUDA 13.3",
            "NVIDIA 显卡，驱动 ≥ 580（较新驱动，性能优先）",
            new[] { "llama-b10919-bin-win-cuda-13.3-x64.zip", "cudart-llama-bin-win-cuda-13.3-x64.zip" },
            (long)(142.8 + 372.9) * 1024 * 1024, "580", NeedsNvidia: true),
        new("cuda124",
            "CUDA 12.4",
            "NVIDIA 显卡，驱动 ≥ 550（兼容旧驱动）",
            new[] { "llama-b10919-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip" },
            (long)(242.3 + 373.3) * 1024 * 1024, "550", NeedsNvidia: true),
        new("vulkan",
            "Vulkan",
            "通用后端：NVIDIA / AMD / Intel 独显与核显均可",
            new[] { "llama-b10919-bin-win-vulkan-x64.zip" },
            (long)30.2 * 1024 * 1024, "", NeedsNvidia: false),
        new("cpu",
            "CPU",
            "无可用显卡时的兜底方案，速度受限",
            new[] { "llama-b10919-bin-win-cpu-x64.zip" },
            (long)17.6 * 1024 * 1024, "", NeedsNvidia: false),
    };

    static string ZipUrl(string asset) =>
        $"https://github.com/{RepoOwner}/{RepoName}/releases/download/{RepoTag}/{asset}";

    public static string PackUrl(EnginePack pack, int index) => ZipUrl(pack.Zips[index]);

    public static EnginePack? Find(string? id) =>
        string.IsNullOrEmpty(id) ? null : Packs.FirstOrDefault(p => p.Id == id);

    /// <summary>按显卡自动推荐：NVIDIA 看驱动版本选 CUDA，其余选 Vulkan，无卡选 CPU。</summary>
    public static EnginePack Recommend(GpuInfo? gpu)
    {
        if (gpu is { IsNvidia: true })
        {
            var major = ParseDriverMajor(gpu.DriverVersion);
            foreach (var p in Packs.Where(p => p.NeedsNvidia).OrderByDescending(p => p.MinNvidiaDriver))
                if (major >= ParseDriverMajor(p.MinNvidiaDriver))
                    return p;
        }
        // 非 NVIDIA（或驱动过旧）：有显卡给 Vulkan，裸机给 CPU
        return gpu != null ? Find("vulkan")! : Find("cpu")!;
    }

    static int ParseDriverMajor(string version) =>
        int.TryParse(version.Split('.')[0], out var v) ? v : 0;
}
