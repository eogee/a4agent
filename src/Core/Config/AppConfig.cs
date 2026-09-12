using System.Text.Json;
using System.Text.Json.Serialization;

namespace a4agent.Core.Config;

public sealed class ModelEntry
{
    public string Path = "";
    public string Alias = "";            // 空 = 使用文件名
    public DateTime AddedAt = DateTime.Now;
}

public sealed class InferParams
{
    public int ContextTokens = 32768;
    public string KvLevel = "q4_0";      // f16 | q8_0 | q4_0
    public bool FlashAttention = true;
    public int Ngl = 99;                 // GPU 层数，99 = 全部
    public int Threads = 0;              // 0 = 自动
    public int Ubatch = 512;
    public int Parallel = 1;
    public int MtpSteps = 0;             // 投机解码步数，0 = 关闭（需后端与模型支持）
    public string ExtraArgs = "";        // 高级逃生舱：原样追加
}

public sealed class RuntimeSettings
{
    public bool AutoTrimRam = true;
    public int TrimDelaySeconds = 90;
    public bool AutoRestartOnCrash = false;
}

public sealed class AppConfig
{
    public bool WizardDone = false;
    public int Port = 8080;
    public string Host = "127.0.0.1";    // 127.0.0.1 | 0.0.0.0
    public string EngineDir = "";        // llama-server.exe 所在目录；空 = 程序目录\engine
    public string EnginePackId = "";     // 向导自动下载的引擎包标识（vulkan/cuda124/cuda133/cpu）
    public List<string> ModelDirs = new();
    public List<ModelEntry> Models = new();
    public string DefaultModelPath = "";
    public InferParams Infer = new();
    public RuntimeSettings Run = new();

    [JsonIgnore]
    public string EffectiveEngineDir =>
        string.IsNullOrWhiteSpace(EngineDir)
            ? Path.Combine(AppContext.BaseDirectory, "engine")
            : EngineDir;
}

public static class ConfigStore
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        // 配置模型全部使用 public 字段（field）承载；System.Text.Json 默认只
        // 序列化属性，不开启 IncludeFields 会把每次保存写成空对象 {}，导致
        // WizardDone 等所有配置"看似保存、实则丢失"，每次启动都重新进入向导。
        IncludeFields = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>便携模式优先：exe 旁存在 config.json 则用它，否则用 %APPDATA%。</summary>
    public static string ResolvePath()
    {
        var portable = Path.Combine(AppContext.BaseDirectory, "config.json");
        if (File.Exists(portable)) return portable;
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "a4agent");
        return Path.Combine(dir, "config.json");
    }

    public static AppConfig Load()
    {
        try
        {
            var path = ResolvePath();
            if (File.Exists(path))
                return JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(path), JsonOpts) ?? new AppConfig();
        }
        catch { /* 损坏的配置按全新处理 */ }
        return new AppConfig();
    }

    public static void Save(AppConfig cfg)
    {
        var path = ResolvePath();
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        File.WriteAllText(path, JsonSerializer.Serialize(cfg, JsonOpts));
    }
}
