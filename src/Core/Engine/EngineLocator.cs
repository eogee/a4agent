namespace a4agent.Core.Engine;

/// <summary>
/// 引擎目录自动接管：默认 engine\ 缺失时，探测本机旧版本安装目录下的引擎
/// （baseDir 同级与 %LOCALAPPDATA%\Programs 下的 a4agent*\engine），
/// 让 Lite 等新装版本直接复用完整版/旧版已下载的引擎，免去重复下载。
/// </summary>
public static class EngineLocator
{
    public static bool IsEnginePresent(string? dir) =>
        !string.IsNullOrWhiteSpace(dir) && File.Exists(Path.Combine(dir, "llama-server.exe"));

    /// <summary>按优先级返回首个可用引擎目录（配置目录 → 默认 engine\ → 旧版本安装目录）；找不到返回 null。</summary>
    public static string? FindExisting(string? configuredDir, string baseDir)
    {
        if (IsEnginePresent(configuredDir)) return configuredDir;
        var fallback = DefaultDir(baseDir);
        if (IsEnginePresent(fallback)) return fallback;
        foreach (var cand in CandidateDirs(baseDir))
            if (IsEnginePresent(cand)) return cand;
        return null;
    }

    public static string DefaultDir(string baseDir) => Path.Combine(baseDir, "engine");

    /// <summary>候选引擎目录：baseDir 同级与 %LOCALAPPDATA%\Programs 下所有 a4agent*\engine，
    /// 按其 llama-server.exe 修改时间从新到旧排序（多个旧版安装时优先最新引擎）。</summary>
    public static IEnumerable<string> CandidateDirs(string baseDir)
    {
        var ownRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(baseDir));
        var ownEngine = Path.TrimEndingDirectorySeparator(Path.GetFullPath(DefaultDir(baseDir)));
        var roots = new List<string>();
        var parent = Path.GetDirectoryName(ownRoot);
        if (!string.IsNullOrEmpty(parent)) roots.Add(parent);
        roots.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs"));

        var found = new List<(string Dir, DateTime Time)>();
        foreach (var root in roots.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (!Directory.Exists(root)) continue;
            string[] dirs;
            try { dirs = Directory.GetDirectories(root, "a4agent*", SearchOption.TopDirectoryOnly); }
            catch { continue; }
            foreach (var d in dirs)
            {
                try
                {
                    var engineDir = Path.TrimEndingDirectorySeparator(
                        Path.GetFullPath(Path.Combine(d, "engine")));
                    if (string.Equals(engineDir, ownEngine, StringComparison.OrdinalIgnoreCase)) continue;
                    var exe = Path.Combine(engineDir, "llama-server.exe");
                    if (!File.Exists(exe)) continue;
                    found.Add((engineDir, File.GetLastWriteTimeUtc(exe)));
                }
                catch { /* 单个候选损坏不影响其余 */ }
            }
        }
        return found.OrderByDescending(x => x.Time).Select(x => x.Dir);
    }
}
