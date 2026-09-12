using System.IO.Compression;
using System.Net.Http;
using System.Security.Cryptography;

namespace a4agent.Core.Engine;

public sealed record EngineDownloadProgress(string Stage, long ReceivedBytes, long TotalBytes, bool Indeterminate)
{
    public string SizeText
    {
        get
        {
            if (TotalBytes <= 0) return "";
            return TotalBytes >= 512 * 1024 * 1024
                ? $"{ReceivedBytes / (1024d * 1024 * 1024):N1} / {TotalBytes / (1024d * 1024 * 1024):N1} GB"
                : $"{ReceivedBytes / (1024d * 1024):N0} / {TotalBytes / (1024d * 1024):N0} MB";
        }
    }
}

/// <summary>
/// 把引擎包（llama.cpp 官方 Release zip，CUDA 含 cudart 运行库包）下载、
/// 解压合并到目标 engine 目录。全程先写临时目录，成功后原子换入，
/// 失败/取消不会留下半个 engine 目录。
/// </summary>
public static class EngineDownloader
{
    public static bool IsEnginePresent(string engineDir) =>
        File.Exists(Path.Combine(engineDir, "llama-server.exe"));

    public static async Task DownloadAsync(EnginePack pack, string engineDir,
        IProgress<EngineDownloadProgress> progress, CancellationToken ct)
    {
        var root = Path.GetDirectoryName(engineDir.TrimEnd(Path.DirectorySeparatorChar));
        if (string.IsNullOrEmpty(root)) throw new IOException($"引擎目录路径无效: {engineDir}");
        Directory.CreateDirectory(root);

        var tmpRoot = Path.Combine(root, $".engine-dl-{Guid.NewGuid():N}");
        var staging = Path.Combine(root, $".engine-new-{Guid.NewGuid():N}");
        Directory.CreateDirectory(tmpRoot);
        Directory.CreateDirectory(staging);

        try
        {
            var grandTotal = pack.TotalBytes;
            long doneBytes = 0;
            var zips = new List<string>();

            using var http = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
            http.DefaultRequestHeaders.UserAgent.ParseAdd("a4agent");

            for (int i = 0; i < pack.Zips.Length; i++)
            {
                ct.ThrowIfCancellationRequested();
                var dest = Path.Combine(tmpRoot, $"{i}.zip");
                progress.Report(new EngineDownloadProgress(
                    $"下载 {i + 1}/{pack.Zips.Length}: {pack.Zips[i]}", doneBytes, grandTotal, false));
                await DownloadFileAsync(http, EngineCatalog.PackUrl(pack, i), dest,
                    p => progress.Report(new EngineDownloadProgress(
                        $"下载 {i + 1}/{pack.Zips.Length}: {pack.Zips[i]}", doneBytes + p, grandTotal, false)),
                    ct);
                doneBytes += new FileInfo(dest).Length;
                zips.Add(dest);
            }

            progress.Report(new EngineDownloadProgress("解压安装中…", doneBytes, grandTotal, true));
            foreach (var zip in zips) ExtractFlat(zip, staging, ct);

            // 原子换入：先确认关键文件在，再替换旧目录
            if (!IsEnginePresent(staging))
                throw new IOException("解压后未找到 llama-server.exe，引擎包内容异常");

            var old = engineDir.TrimEnd(Path.DirectorySeparatorChar);
            if (Directory.Exists(old)) Directory.Delete(old, recursive: true);
            Directory.Move(staging, old);

            progress.Report(new EngineDownloadProgress("✔ 引擎安装完成", grandTotal, grandTotal, false));
        }
        finally
        {
            TryDelete(tmpRoot);
            TryDelete(staging);
        }
    }

    static async Task DownloadFileAsync(HttpClient http, string url, string dest,
        Action<long> onBytes, CancellationToken ct)
    {
        using var resp = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();
        await using var src = await resp.Content.ReadAsStreamAsync(ct);
        await using var fs = new FileStream(dest, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16);
        var buf = new byte[1 << 16];
        int n;
        while ((n = await src.ReadAsync(buf, ct)) > 0)
        {
            await fs.WriteAsync(buf.AsMemory(0, n), ct);
            onBytes(fs.Length);
        }
    }

    /// <summary>zip 内容全部平铺进 dest（llama.cpp 官方包内部结构是平的，
    /// CUDA 主包与 cudart 包文件名不重叠）。跳过试图逃出 dest 的恶意条目。</summary>
    static void ExtractFlat(string zipPath, string dest, CancellationToken ct)
    {
        using var archive = ZipFile.OpenRead(zipPath);
        var destRoot = Path.GetFullPath(dest);
        foreach (var entry in archive.Entries)
        {
            if (string.IsNullOrEmpty(entry.Name)) continue;   // 目录条目
            ct.ThrowIfCancellationRequested();
            var target = Path.GetFullPath(Path.Combine(destRoot, entry.FullName));
            if (!target.StartsWith(destRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                continue;                                     // 路径穿越，丢弃
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (File.Exists(target)) continue;                // 先到先得，主包优先
            ZipFileExtensions.ExtractToFile(entry, target, overwrite: false);
        }
    }

    static void TryDelete(string path)
    {
        try { if (Directory.Exists(path)) Directory.Delete(path, recursive: true); }
        catch { /* 清理失败不影响主流程 */ }
    }
}
