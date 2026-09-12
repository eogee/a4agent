using System.Diagnostics;
using Microsoft.Win32;

namespace a4agent.Core.Gpu;

public sealed record GpuInfo(string Vendor, string Name, long VramBytes, string DriverVersion = "")
{
    public bool IsNvidia => Vendor == "NVIDIA";
    public double VramGb => Math.Round(VramBytes / (1024d * 1024 * 1024), 1);
}

public static class GpuDetector
{
    /// <summary>Detect GPUs: NVIDIA via nvidia-smi first (accurate VRAM),
    /// then registry enumeration so AMD/Intel cards are still listed.</summary>
    public static List<GpuInfo> Detect()
    {
        var result = new List<GpuInfo>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var g in TryNvidiaSmi())
            if (seen.Add(g.Name)) result.Add(g);
        foreach (var g in TryRegistry())
            // 虚拟显示适配器（GameViewer/OrayIddDriver 之类）没有显存也没有推理价值
            if (seen.Add(g.Name) && !(g.Vendor == "Unknown" && g.VramBytes <= 0)) result.Add(g);

        return result;
    }

    public static GpuInfo? Best(List<GpuInfo>? list = null)
    {
        list ??= Detect();
        return list.OrderByDescending(g => g.IsNvidia).ThenByDescending(g => g.VramBytes).FirstOrDefault();
    }

    private static List<GpuInfo> TryNvidiaSmi()
    {
        var list = new List<GpuInfo>();
        string? exe = ResolveNvidiaSmi();
        if (exe == null) return list;
        try
        {
                var psi = new ProcessStartInfo(exe, "--query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits")
                {
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                };
                using var p = Process.Start(psi)!;
                var output = p.StandardOutput.ReadToEnd();
                p.WaitForExit(8000);
                foreach (var line in output.Split('\n', StringSplitOptions.RemoveEmptyEntries))
                {
                    var parts = line.Split(',');
                    if (parts.Length < 2) continue;
                    var name = parts[0].Trim();
                    if (!long.TryParse(parts[1].Trim(), out var mib)) continue;
                    var driver = parts.Length > 2 ? parts[2].Trim() : "";
                    list.Add(new GpuInfo("NVIDIA", name, mib * 1024L * 1024L, driver));
                }
        }
        catch { /* nvidia-smi broken/absent -> registry path covers it */ }
        return list;
    }

    private static string? ResolveNvidiaSmi()
    {
        try
        {
            var psi = new ProcessStartInfo("cmd.exe", "/c where nvidia-smi.exe")
            { UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true };
            using var p = Process.Start(psi)!;
            var line = p.StandardOutput.ReadLine();
            p.WaitForExit(3000);
            if (!string.IsNullOrWhiteSpace(line) && File.Exists(line.Trim())) return line.Trim();
        }
        catch { }
        var sys32 = @"C:\Windows\System32\nvidia-smi.exe";
        return File.Exists(sys32) ? sys32 : null;
    }

    private static List<GpuInfo> TryRegistry()
    {
        var list = new List<GpuInfo>();
        const string keyRoot = @"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}";
        try
        {
            using var root = Registry.LocalMachine.OpenSubKey(keyRoot);
            if (root == null) return list;
            foreach (var sub in root.GetSubKeyNames())
            {
                if (!sub.StartsWith("00")) continue;
                using var k = root.OpenSubKey(sub);
                var desc = k?.GetValue("DriverDesc") as string;
                if (string.IsNullOrEmpty(desc)) continue;

                long vram = 0;
                var qword = k?.GetValue("HardwareInformation.qwMemorySize");
                if (qword is long l) vram = l;
                else if (qword is int i) vram = i;
                if (vram <= 0 && k?.GetValue("HardwareInformation.MemorySize") is long l2) vram = l2;

                var vendor =
                    desc.Contains("NVIDIA", StringComparison.OrdinalIgnoreCase) ||
                    desc.Contains("GeForce", StringComparison.OrdinalIgnoreCase) ||
                    desc.Contains("RTX", StringComparison.OrdinalIgnoreCase) ? "NVIDIA" :
                    desc.Contains("AMD", StringComparison.OrdinalIgnoreCase) ||
                    desc.Contains("Radeon", StringComparison.OrdinalIgnoreCase) ? "AMD" :
                    desc.Contains("Intel", StringComparison.OrdinalIgnoreCase) ||
                    desc.Contains("ARC", StringComparison.OrdinalIgnoreCase) ? "Intel" : "Unknown";

                list.Add(new GpuInfo(vendor, desc.Trim(), vram));
            }
        }
        catch { /* best effort */ }
        return list;
    }
}
