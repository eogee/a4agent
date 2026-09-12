using a4agent.Core.Gguf;

namespace a4agent.App;

internal static class Ui
{
    /// <summary>程序运行期缓存的状态图标（绿=运行 橙=启动 红=失败 灰=停止）。</summary>
    public static Icon IconForState(Core.Engine.EngineState state)
    {
        var color = state switch
        {
            Core.Engine.EngineState.Running => Color.LimeGreen,
            Core.Engine.EngineState.Starting => Color.Orange,
            Core.Engine.EngineState.Failed => Color.Crimson,
            _ => Color.Gray,
        };
        return MakeCircleIcon(color);
    }

    public static Icon MakeCircleIcon(Color color)
    {
        using var bmp = new Bitmap(32, 32);
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        g.Clear(Color.Transparent);
        using var brush = new SolidBrush(color);
        g.FillEllipse(brush, 3, 3, 26, 26);
        return Icon.FromHandle(bmp.GetHicon());
    }

    /// <summary>扫描多个目录下的 gguf 并解析（跳过解析失败项不抛出）。</summary>
    public static List<GgufModelInfo> ScanDirs(IEnumerable<string> dirs)
    {
        var list = new List<GgufModelInfo>();
        foreach (var dir in dirs)
        {
            if (!Directory.Exists(dir)) continue;
            foreach (var f in Directory.EnumerateFiles(dir, "*.gguf", SearchOption.TopDirectoryOnly))
                list.Add(GgufParser.Parse(f));
        }
        return list.Where(x => x.Ok).OrderBy(x => x.FilePath).ToList();
    }

    public static void FillModelColumns(ListViewItem it, GgufModelInfo m)
    {
        if (!m.Ok)
        {
            it.SubItems.Add("-"); it.SubItems.Add("-");
            it.SubItems.Add("-"); it.SubItems.Add("-");
            it.ForeColor = Color.Gray;
            it.ToolTipText = m.Error;
            return;
        }
        it.SubItems.Add($"{m.FileSize / (1024d * 1024 * 1024):N1} GB");
        it.SubItems.Add(m.QuantLabel);
        it.SubItems.Add(m.NativeContext > 0 ? $"{m.NativeContext / 1024}k" : "-");
        it.SubItems.Add(m.HasNextnTensors ? "✓" : "-");
    }

    /// <summary>本机第一个非回环 IPv4，用于局域网接入提示。</summary>
    public static string? GetLanIPv4()
    {
        try
        {
            return System.Net.NetworkInformation.NetworkInterface.GetAllNetworkInterfaces()
                .Where(n => n.OperationalStatus == System.Net.NetworkInformation.OperationalStatus.Up
                         && n.NetworkInterfaceType != System.Net.NetworkInformation.NetworkInterfaceType.Loopback)
                .SelectMany(n => n.GetIPProperties().UnicastAddresses)
                .Where(a => a.Address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
                .Select(a => a.Address.ToString())
                .FirstOrDefault();
        }
        catch { return null; }
    }
}
