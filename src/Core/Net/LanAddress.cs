using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;

namespace a4agent.Core.Net;

/// <summary>选取本机对局域网真正可用的 IPv4 地址。</summary>
/// <remarks>
/// 简单地取"第一个非回环 IPv4"会拿到 WSL/Hyper-V/VPN 等虚拟网卡的地址
/// （如 172.17.x.x），其他设备根本连不上。这里按可用性排序：
/// 有默认网关的接口 &gt; 无网关；非虚拟适配器 &gt; 虚拟适配器；私网地址 &gt; 其他。
/// </remarks>
public static class LanAddress
{
    public static string? GetBestIPv4()
    {
        try
        {
            return CandidateAddresses().FirstOrDefault()?.ToString();
        }
        catch { return null; }
    }

    /// <summary>全部候选地址，按可用性从高到低。</summary>
    public static IEnumerable<IPAddress> CandidateAddresses()
    {
        var candidates = new List<(IPAddress Ip, int Score)>();
        foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (nic.OperationalStatus != OperationalStatus.Up) continue;
            if (nic.NetworkInterfaceType == NetworkInterfaceType.Loopback) continue;

            var score = 0;
            if (HasGateway(nic)) score += 4;
            if (!LooksVirtual(nic)) score += 2;

            foreach (var ip in nic.GetIPProperties().UnicastAddresses)
            {
                if (ip.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                var a = ip.Address;
                var s = score;
                if (IsPrivate(a)) s += 1;
                if (IsApiPa(a)) s -= 10;             // 169.254.x.x 自动专有地址，无连接价值
                candidates.Add((a, s));
            }
        }
        return candidates.OrderByDescending(x => x.Score).Select(x => x.Ip);
    }

    static bool HasGateway(NetworkInterface nic) =>
        nic.GetIPProperties().GatewayAddresses.Any(g => g.Address != null);

    static bool LooksVirtual(NetworkInterface nic)
    {
        var text = (nic.Description + " " + nic.Name).ToLowerInvariant();
        return text.Contains("vethernet") || text.Contains("hyper-v") || text.Contains("wsl")
            || text.Contains("vmware") || text.Contains("virtualbox") || text.Contains("virtual")
            || text.Contains("miniport") || text.Contains("tap-") || text.Contains("tunnel")
            || text.Contains("loopback");
    }

    static bool IsPrivate(IPAddress a)
    {
        var o = a.GetAddressBytes();
        return o.Length == 4 && (o[0] == 10
            || (o[0] == 172 && o[1] >= 16 && o[1] <= 31)
            || (o[0] == 192 && o[1] == 168));
    }

    static bool IsApiPa(IPAddress a)
    {
        var o = a.GetAddressBytes();
        return o.Length == 4 && o[0] == 169 && o[1] == 254;
    }
}
