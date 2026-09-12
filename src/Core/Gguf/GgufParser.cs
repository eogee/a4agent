using System.Text;

namespace a4agent.Core.Gguf;

/// <summary>Parsed metadata of a GGUF model file.</summary>
public sealed class GgufModelInfo
{
    public string FilePath = "";
    public long FileSize;
    public bool Ok;
    public string Error = "";
    public string Arch = "";
    public int Layers;                 // block_count
    public int HeadCount;              // attention.head_count
    public int HeadCountKv;            // attention.head_count_kv
    public int KeyLength;              // attention.key_length (fallback: embedding)
    public int ValueLength;            // attention.value_length
    public int EmbeddingLength;        // embedding_length
    public long NativeContext;         // context_length
    public int FullAttnInterval;       // 0/absent => all layers are full attention
    public int NextnPredictLayers;     // MTP draft layers embedded in the file
    public bool HasNextnTensors;       // tensors named *.nextn.* present
    public int FileTypeRaw;            // general.file_type
    public string QuantLabel = "";

    /// <summary>Layers that actually carry a KV cache. Hybrid SSM models only
    /// pay KV for every Nth layer (full_attention_interval).</summary>
    public int EffectiveKvLayers =>
        FullAttnInterval > 1 ? (int)Math.Ceiling(Layers / (double)FullAttnInterval) : Layers;

    /// <summary>Bytes per token of the KV cache at f16, derived from metadata.</summary>
    public long KvBytesPerTokenF16
    {
        get
        {
            if (HeadCountKv <= 0 || KeyLength <= 0 || ValueLength <= 0) return 0;
            var elemsPerLayer = (long)HeadCountKv * KeyLength + (long)HeadCountKv * ValueLength;
            return EffectiveKvLayers * elemsPerLayer * 2; // 2 bytes fp16
        }
    }

    public double EstimateVramGb(long ctxTokens, string kvLevel)
    {
        const double weightsOverheadGb = 0.9;   // compute buffers + CUDA context (rough)
        var kvBpt = KvBytesPerTokenF16 * KvFactor(kvLevel);
        var kvGb = ctxTokens * kvBpt / (1024d * 1024 * 1024);
        return FileSize / (1024d * 1024 * 1024) + kvGb + weightsOverheadGb;
    }

    public static double KvFactor(string level) => level switch
    {
        "q8_0" => 1.0625 / 2.0,
        "q4_0" => 0.5625 / 2.0,
        _ => 1.0, // f16
    };
}

/// <summary>Minimal streaming GGUF header reader (metadata + tensor names).
/// Stops early on tokenizer arrays to stay fast; never maps the whole file.</summary>
public static class GgufParser
{
    private static readonly Dictionary<uint, string> FtypeMap = new()
    {
        { 0, "F32" }, { 1, "F16" }, { 2, "Q4_0" }, { 3, "Q4_1" }, { 7, "Q8_0" },
        { 8, "Q5_0" }, { 9, "Q5_1" }, { 10, "Q2_K" }, { 11, "Q3_K_S" }, { 12, "Q3_K_M" },
        { 13, "Q3_K_L" }, { 14, "Q4_K_S" }, { 15, "Q4_K_M" }, { 16, "Q5_K_S" }, { 17, "Q5_K_M" },
        { 18, "Q6_K" }, { 19, "IQ2_XXS" }, { 20, "IQ2_XS" }, { 21, "Q2_K_S" }, { 22, "IQ3_XS" },
        { 23, "IQ3_XXS" }, { 24, "IQ1_S" }, { 25, "IQ4_NL" }, { 26, "IQ3_S" }, { 27, "IQ2_M" },
        { 28, "IQ4_XS" }, { 29, "IQ1_M" }, { 30, "BF16" },
        { 32, "TQ1_0" }, { 33, "TQ2_0" }, { 36, "MXFP4" },
    };

    private const long MaxScanBytes = 64L * 1024 * 1024; // safety cap while skipping

    public static GgufModelInfo Parse(string path)
    {
        var info = new GgufModelInfo { FilePath = path, FileSize = TryLength(path) };
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var br = new BinaryReader(fs);

            var magic = br.ReadBytes(4);
            if (magic[0] != (byte)'G' || magic[1] != (byte)'G' || magic[2] != (byte)'U' || magic[3] != (byte)'F')
            { info.Error = "不是有效的 GGUF 文件"; return info; }
            uint version = br.ReadUInt32();
            if (version is < 2 or > 3) { info.Error = $"不支持的 GGUF 版本 {version}"; return info; }

            ulong tensorCount = br.ReadUInt64();
            ulong kvCount = br.ReadUInt64();

            string arch = "";
            int layers = 0, heads = 0, headsKv = 0, klen = 0, vlen = 0, emb = 0, interval = 0, nextn = 0;
            long nativeCtx = 0; int fileType = -1;

            for (ulong i = 0; i < kvCount; i++)
            {
                var key = ReadString(br);
                uint vt = br.ReadUInt32();

                if (vt == 8) // string
                {
                    var s = ReadString(br);
                    if (key == "general.architecture") arch = s;
                }
                else if (vt == 9) // array: capture small numeric arrays, skip big ones fast
                {
                    uint et = br.ReadUInt32();
                    ulong cnt = br.ReadUInt64();
                    if (et <= 7 && cnt <= 8)
                        SkipSlow(br, et, cnt);           // tiny arrays of scalars/bools: rare
                    else
                        SkipArrayFast(br, et, cnt, fs);  // tokenizer arrays etc.
                }
                else
                {
                    long v = ReadScalar(br, vt);
                    // keys 以架构名为前缀，而 arch 可能晚于这些键才出现；
                    // 因此统一按后缀匹配（与顺序无关）：
                    if (key.EndsWith(".block_count")) layers = (int)v;
                    else if (key.EndsWith(".attention.head_count")) heads = (int)v;
                    else if (key.EndsWith(".attention.head_count_kv")) headsKv = (int)v;
                    else if (key.EndsWith(".attention.key_length")) klen = (int)v;
                    else if (key.EndsWith(".attention.value_length")) vlen = (int)v;
                    else if (key.EndsWith(".embedding_length")) emb = (int)v;
                    else if (key.EndsWith(".context_length")) nativeCtx = v;
                    else if (key.EndsWith(".full_attention_interval")) interval = (int)v;
                    else if (key.EndsWith(".nextn_predict_layers")) nextn = (int)v;
                    else if (key == "general.file_type") fileType = (int)v;
                }

                if (fs.Position > MaxScanBytes && key.StartsWith("tokenizer."))
                    break; // paranoia stop
            }

            // Tensor directory scan: look for nextn (MTP) tensors.
            bool hasNextn = false;
            for (ulong t = 0; t < tensorCount && !hasNextn; t++)
            {
                var name = ReadString(br);
                uint nDims = br.ReadUInt32();
                if (nDims > 8) break; // corrupt; abort tensor scan
                br.BaseStream.Seek(8L * nDims, SeekOrigin.Current); // dims u64[]
                br.ReadUInt32();                                    // ggml type
                br.ReadUInt64();                                    // offset
                if (name.Contains(".nextn.", StringComparison.Ordinal)) hasNextn = true;
            }

            info.Arch = arch;
            info.Layers = layers;
            info.HeadCount = heads;
            info.HeadCountKv = headsKv;
            info.KeyLength = klen > 0 ? klen : emb;
            info.ValueLength = vlen > 0 ? vlen : emb;
            info.EmbeddingLength = emb;
            info.NativeContext = nativeCtx;
            info.FullAttnInterval = interval;
            info.NextnPredictLayers = nextn;
            info.HasNextnTensors = hasNextn;
            info.FileTypeRaw = fileType;
            info.QuantLabel = fileType >= 0 && FtypeMap.TryGetValue((uint)fileType, out var q) ? q : "?";
            info.Ok = true;
            return info;
        }
        catch (Exception ex)
        {
            info.Error = "解析失败: " + ex.Message;
            return info;
        }
    }

    private static long TryLength(string p) { try { return new FileInfo(p).Length; } catch { return 0; } }

    private static string ReadString(BinaryReader br)
    {
        ulong len = br.ReadUInt64();
        if (len > 64 * 1024 * 1024) throw new InvalidDataException("字符串长度异常");
        var buf = br.ReadBytes((int)len);
        return Encoding.UTF8.GetString(buf);
    }

    private static long ReadScalar(BinaryReader br, uint type) => type switch
    {
        0 => br.ReadByte(),                       // u8
        1 => (sbyte)br.ReadByte(),                // i8
        2 => br.ReadUInt16(),                     // u16
        3 => br.ReadInt16(),                      // i16
        4 => br.ReadUInt32(),                     // u32
        5 => br.ReadInt32(),                      // i32
        6 => (long)br.ReadSingle(),               // f32 -> truncate
        7 => br.ReadByte() != 0 ? 1 : 0,          // bool
        10 => checked((long)br.ReadUInt64()),     // u64
        11 => br.ReadInt64(),                     // i64
        12 => (long)br.ReadDouble(),              // f64
        _ => throw new InvalidDataException($"未知标量类型 {type}"),
    };

    private static readonly Dictionary<uint, int> ScalarSizes = new()
    {
        { 0, 1 }, { 1, 1 }, { 2, 2 }, { 3, 2 }, { 4, 4 }, { 5, 4 }, { 6, 4 }, { 7, 1 },
        { 10, 8 }, { 11, 8 }, { 12, 8 },
    };

    private static void SkipArrayFast(BinaryReader br, uint elemType, ulong count, FileStream fs)
    {
        if (elemType == 8) // array of strings
        {
            for (ulong i = 0; i < count; i++) ReadString(br);
            return;
        }
        if (elemType == 9) // nested array: must walk (rare)
        {
            for (ulong i = 0; i < count; i++)
            {
                uint et2 = br.ReadUInt32();
                ulong c2 = br.ReadUInt64();
                SkipArrayFast(br, et2, c2, fs);
            }
            return;
        }
        if (!ScalarSizes.TryGetValue(elemType, out var sz))
            throw new InvalidDataException($"未知数组元素类型 {elemType}");
        // Seek instead of read: huge tokenizer blocks fly by instantly.
        br.BaseStream.Seek((long)count * sz, SeekOrigin.Current);
    }

    private static void SkipSlow(BinaryReader br, uint elemType, ulong count)
    {
        for (ulong i = 0; i < count; i++) ReadScalar(br, elemType);
    }
}
