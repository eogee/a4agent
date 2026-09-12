using a4agent.Core.Config;
using a4agent.Core.Gguf;
using a4agent.Core.Gpu;
using a4agent.Core.Engine;
using a4agent.Core.Preset;

namespace a4agent.App;

/// <summary>首次运行向导：获取引擎（Lite 包才出现）→ 显卡检测 → 模型目录与校验 → 默认模型 → 端口。</summary>
public sealed class WizardForm : Form
{
    readonly AppConfig _cfg;
    readonly List<string> _dirs = new();
    readonly Dictionary<string, GgufModelInfo> _scan = new();   // path -> info
    GpuInfo? _bestGpu;
    Preset _preset = new("", 32768, "q4_0", "");
    int _step;

    // 动态步骤：本地已有引擎（完整版安装包）时不出现引擎获取步骤
    readonly List<(string Panel, string Title)> _steps = new();

    Label _lblTitle = null!;
    Panel _body = null!;
    Button _btnBack = null!, _btnNext = null!;
    Label _lblError = null!;

    // step engine
    readonly List<RadioButton> _rdoPacks = new();
    RadioButton _rdoOffline = null!, _rdoSkip = null!;
    Label _lblOfflineDir = null!, _lblEngStatus = null!;
    ProgressBar _prgEng = null!;
    CancellationTokenSource? _dlCts;
    bool _downloading;
    string _offlineEngineDir = "";

    // step1
    TextBox _txtGpu = null!;
    Label _lblPreset = null!;
    // step2
    ListBox _lstDirs = null!;
    ListView _lvwModels = null!;
    Label _lblScan = null!;
    // step3
    ComboBox _cboModel = null!;
    Label _lblModelInfo = null!;
    // step4
    NumericUpDown _nudPort = null!;
    CheckBox _chkStart = null!;
    Label _lblSummary = null!;

    public bool LaunchAfterFinish { get; private set; }

    public WizardForm(AppConfig cfg)
    {
        _cfg = cfg;
        Text = "a4agent 首次配置向导";
        try { Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(760, 560);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;

        _lblTitle = new Label
        {
            Text = "步骤 1/4 · 检测硬件",
            Font = new Font("微软雅黑", 12, FontStyle.Bold),
            Location = new Point(16, 12), AutoSize = true,
        };

        _body = new Panel { Location = new Point(8, 48), Size = new Size(744, 440), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom };
        BuildStepEngine(); BuildStep1(); BuildStep2(); BuildStep3(); BuildStep4();

        _lblError = new Label
        {
            ForeColor = Color.Firebrick, AutoSize = false,
            Size = new Size(430, 28), Location = new Point(16, 496),
            Anchor = AnchorStyles.Bottom | AnchorStyles.Left,
            TextAlign = ContentAlignment.MiddleLeft,
        };
        _btnBack = new Button { Text = "< 上一步", Location = new Point(462, 500), Size = new Size(88, 30), Enabled = false, Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
        _btnNext = new Button { Text = "下一步 >", Location = new Point(556, 500), Size = new Size(96, 30), Anchor = AnchorStyles.Bottom | AnchorStyles.Right };
        _btnBack.Click += (_, _) => { if (_step > 0 && !_downloading) ShowStep(_step - 1); };
        _btnNext.Click += OnNext;

        Controls.AddRange([_lblTitle, _body, _lblError, _btnBack, _btnNext]);
        AcceptButton = _btnNext;

        Load += (_, _) => { RunDetection(); InitSteps(); ShowStep(0); };
        FormClosing += (_, _) => { if (_downloading) _dlCts?.Cancel(); };
    }

    void InitSteps()
    {
        _steps.Clear();
        if (!EngineDownloader.IsEnginePresent(_cfg.EffectiveEngineDir))
        {
            _steps.Add(("pEng", "获取推理引擎"));
            var rec = EngineCatalog.Recommend(_bestGpu);
            _rdoPacks[EngineCatalog.Packs.IndexOf(rec)].Checked = true;
        }
        _steps.Add(("p0", "检测硬件"));
        _steps.Add(("p1", "选择模型目录"));
        _steps.Add(("p2", "选择默认模型"));
        _steps.Add(("p3", "服务端口"));
    }

    async void OnNext(object? s, EventArgs e)
    {
        _lblError.Text = "";
        switch (_steps[_step].Panel)
        {
            case "pEng": await EngineNext(); break;
            case "p0": ShowStep(_step + 1); break;
            case "p1":
                if (_scan.Values.Count(x => x.Ok) == 0)
                {
                    _lblError.Text = "所选目录中未找到有效的 .gguf 模型文件，请检查目录或添加其他目录。";
                    return;
                }
                FillModelCombo();
                ShowStep(_step + 1);
                break;
            case "p2":
                if (_cboModel.SelectedItem is not GgufModelInfo sel)
                {
                    _lblError.Text = "请选择一个默认模型。";
                    return;
                }
                _cfg.DefaultModelPath = sel.FilePath;
                ShowStep(_step + 1);
                break;
            default: Finish(); break;
        }
    }

    // ───────────────────────── step engine ─────────────────────────
    void BuildStepEngine()
    {
        var p = new Panel { Name = "pEng", Dock = DockStyle.Fill, Visible = false };

        var intro = new Label
        {
            Text = "轻量版安装包不内置推理引擎（llama-server）。下面按你的显卡列出了"
                 + "llama.cpp 官方发布的引擎包，选择后由向导自动下载安装；也可以离线安装或暂时跳过。",
            Location = new Point(8, 4), Size = new Size(720, 36), ForeColor = Color.DimGray,
        };

        var gbPack = new GroupBox
        {
            Text = "在线下载（github.com/ggml-org/llama.cpp 官方发布）",
            Font = new Font("微软雅黑", 9),
            Location = new Point(8, 44), Size = new Size(720, 132),
        };
        int y = 24;
        foreach (var pack in EngineCatalog.Packs)
        {
            var rdo = new RadioButton
            {
                Text = $"{pack.Label} — 约 {pack.SizeLabel}　({pack.Note})",
                AutoSize = true, Location = new Point(10, y),
            };
            _rdoPacks.Add(rdo);
            gbPack.Controls.Add(rdo);
            y += 26;
        }

        var gbOther = new GroupBox
        {
            Text = "其他方式", Font = new Font("微软雅黑", 9),
            Location = new Point(8, 184), Size = new Size(720, 112),
        };
        _rdoOffline = new RadioButton
        {
            Text = "离线安装：指定本机已有的引擎目录（含 llama-server.exe）",
            AutoSize = true, Location = new Point(10, 22),
        };
        var btnBrowse = new Button { Text = "浏览目录...", Location = new Point(30, 48), Size = new Size(110, 26), Enabled = false };
        _lblOfflineDir = new Label
        {
            Text = "", Location = new Point(150, 54), AutoSize = false,
            Size = new Size(556, 20), ForeColor = Color.DimGray,
        };
        _rdoSkip = new RadioButton
        {
            Text = "暂时跳过（稍后在主窗口「设置」页指定引擎目录）",
            AutoSize = true, Location = new Point(10, 82),
        };
        _rdoOffline.CheckedChanged += (_, _) => btnBrowse.Enabled = _rdoOffline.Checked;
        btnBrowse.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = "选择包含 llama-server.exe 的引擎目录" };
            if (dlg.ShowDialog(this) != DialogResult.OK) return;
            _offlineEngineDir = dlg.SelectedPath;
            _lblOfflineDir.Text = _offlineEngineDir;
        };
        gbOther.Controls.AddRange([_rdoOffline, btnBrowse, _lblOfflineDir, _rdoSkip]);

        _lblEngStatus = new Label
        {
            Text = "", Location = new Point(8, 310), AutoSize = false,
            Size = new Size(720, 60), ForeColor = Color.DarkSlateBlue,
        };
        _prgEng = new ProgressBar { Location = new Point(8, 374), Size = new Size(720, 18), Visible = false };

        p.Controls.AddRange([intro, gbPack, gbOther, _lblEngStatus, _prgEng]);
        _body.Controls.Add(p);
    }

    EnginePack? SelectedPack()
    {
        for (int i = 0; i < _rdoPacks.Count; i++)
            if (_rdoPacks[i].Checked) return EngineCatalog.Packs[i];
        return null;
    }

    async Task EngineNext()
    {
        if (_downloading) return;

        if (_rdoOffline.Checked)
        {
            if (!EngineDownloader.IsEnginePresent(_offlineEngineDir))
            {
                _lblError.Text = "所选目录中没有 llama-server.exe，请重新选择。";
                return;
            }
            _cfg.EngineDir = _offlineEngineDir;
            ConfigStore.Save(_cfg);
            ShowStep(_step + 1);
            return;
        }
        if (_rdoSkip.Checked) { ShowStep(_step + 1); return; }

        var pack = SelectedPack();
        if (pack == null) { _lblError.Text = "请选择一个引擎包。"; return; }

        _downloading = true;
        _btnBack.Enabled = _btnNext.Enabled = false;
        _prgEng.Value = 0;
        _prgEng.Style = ProgressBarStyle.Continuous;
        _prgEng.Visible = true;
        _dlCts = new CancellationTokenSource();
        var ct = _dlCts.Token;
        var prog = new Progress<EngineDownloadProgress>(p =>
        {
            _lblEngStatus.Text = $"{p.Stage}   {p.SizeText}";
            if (p.Indeterminate) { _prgEng.Style = ProgressBarStyle.Marquee; }
            else
            {
                _prgEng.Style = ProgressBarStyle.Continuous;
                if (p.TotalBytes > 0) _prgEng.Value = Math.Min(100, (int)(100 * p.ReceivedBytes / p.TotalBytes));
            }
        });
        try
        {
            var dir = _cfg.EffectiveEngineDir;
            await EngineDownloader.DownloadAsync(pack, dir, prog, ct);
            _cfg.EngineDir = "";          // 用默认 engine 目录
            _cfg.EnginePackId = pack.Id;
            ConfigStore.Save(_cfg);
            _lblEngStatus.Text = "✔ 引擎安装完成。";
            await Task.Delay(500);
            ShowStep(_step + 1);
        }
        catch (OperationCanceledException)
        {
            _lblEngStatus.Text = "已取消下载，可重新选择后再试。";
        }
        catch (Exception ex)
        {
            _lblError.Text = "下载失败：" + ex.Message + "（可改用离线安装，或检查网络后重试）";
        }
        finally
        {
            _downloading = false;
            _btnBack.Enabled = _step > 0;
            _btnNext.Enabled = true;
        }
    }

    void Finish()
    {
        _cfg.Port = (int)_nudPort.Value;
        _cfg.ModelDirs = _dirs.ToList();
        _cfg.Models = _scan.Values.Where(x => x.Ok)
            .Select(x => new ModelEntry { Path = x.FilePath })
            .ToList();
        _cfg.Infer.ContextTokens = _preset.ContextTokens;
        _cfg.Infer.KvLevel = _preset.KvLevel;
        _cfg.WizardDone = true;
        ConfigStore.Save(_cfg);
        LaunchAfterFinish = _chkStart.Checked;
        DialogResult = DialogResult.OK;
    }

    void ShowStep(int i)
    {
        _step = i;
        foreach (Control c in _body.Controls) c.Visible = false;
        var panel = _steps[i].Panel;
        _body.Controls[panel]!.Visible = true;
        _lblTitle.Text = $"步骤 {i + 1}/{_steps.Count} · {_steps[i].Title}";
        _btnBack.Enabled = i > 0 && !_downloading;
        _btnNext.Text = i == _steps.Count - 1 ? "完成" : "下一步 >";
        if (panel == "p2") RefreshRiskHint();
        if (panel == "p3") FillSummary();
    }

    // ───────────────────────── step 1 ─────────────────────────
    void BuildStep1()
    {
        var p = new Panel { Name = "p0", Dock = DockStyle.Fill, Visible = false };
        var gbGpu = new GroupBox
        {
            Text = "检测到的显卡", Font = new Font("微软雅黑", 9),
            Location = new Point(8, 8), Size = new Size(720, 120),
        };
        _txtGpu = new TextBox
        {
            Multiline = true, ReadOnly = true, BackColor = SystemColors.Window,
            ScrollBars = ScrollBars.Vertical, Font = new Font(FontFamily.GenericMonospace, 9),
            Location = new Point(10, 22), Size = new Size(700, 86),
        };
        gbGpu.Controls.Add(_txtGpu);

        var gbPreset = new GroupBox
        {
            Text = "为你推荐的推理预设", Font = new Font("微软雅黑", 9),
            Location = new Point(8, 140), Size = new Size(720, 130),
        };
        _lblPreset = new Label
        {
            Location = new Point(10, 22), Size = new Size(700, 100),
            Font = new Font("微软雅黑", 9),
        };
        gbPreset.Controls.Add(_lblPreset);

        var hint = new Label
        {
            Text = "预设只是起点：稍后在 设置 页可以随时调整上下文长度、KV 缓存级别、MTP 等所有参数。",
            Location = new Point(8, 290), Size = new Size(720, 24), ForeColor = Color.DimGray,
        };
        p.Controls.AddRange([gbGpu, gbPreset, hint]);
        _body.Controls.Add(p);
    }

    void RunDetection()
    {
        var gpus = GpuDetector.Detect();
        _bestGpu = GpuDetector.Best(gpus);
        _preset = PresetEngine.Recommend(_bestGpu?.VramGb ?? 0, _bestGpu is { IsNvidia: true });

        _txtGpu.Lines = gpus.Count == 0
            ? ["未检测到独立显卡（将以 CPU 模式运行，速度受限）"]
            : gpus.Select(g => $"[{g.Vendor}] {g.Name}  —  显存 {g.VramGb:N1} GB"
                + (string.IsNullOrEmpty(g.DriverVersion) ? "" : $"  ·  驱动 {g.DriverVersion}")).ToArray();

        _lblPreset.Text = $"预设档位：{_preset.Label}\r\n上下文 {_preset.ContextTokens} tokens · KV 缓存 {_preset.KvLevel}\r\n{_preset.Note}";
    }

    // ───────────────────────── step 2 ─────────────────────────
    void BuildStep2()
    {
        var p = new Panel { Name = "p1", Dock = DockStyle.Fill, Visible = false };

        var lblDirs = new Label { Text = "模型库目录（可添加多个）：", Location = new Point(8, 6), AutoSize = true };
        _lstDirs = new ListBox { Location = new Point(8, 28), Size = new Size(600, 84) };
        var btnAdd = new Button { Text = "添加目录...", Location = new Point(618, 28), Size = new Size(110, 28) };
        var btnRm = new Button { Text = "移除选中", Location = new Point(618, 62), Size = new Size(110, 28), Enabled = false };
        var btnScan = new Button { Text = "重新扫描", Location = new Point(618, 96), Size = new Size(110, 28) };

        _lvwModels = new ListView
        {
            View = View.Details, FullRowSelect = true, HideSelection = false,
            Location = new Point(8, 124), Size = new Size(720, 240),
        };
        _lvwModels.Columns.Add("文件", 300);
        _lvwModels.Columns.Add("大小", 90);
        _lvwModels.Columns.Add("量化", 80);
        _lvwModels.Columns.Add("原生上下文", 100);
        _lvwModels.Columns.Add("MTP", 60);
        _lvwModels.ItemSelectionChanged += (_, _) =>
            btnSetDefaultReady = _lvwModels.SelectedItems.Count > 0;

        btnAdd.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = "选择包含 .gguf 模型文件的目录" };
            if (dlg.ShowDialog(this) != DialogResult.OK) return;
            if (_dirs.Contains(dlg.SelectedPath)) return;
            _dirs.Add(dlg.SelectedPath);
            _lstDirs.Items.Add(dlg.SelectedPath);
            RescanAll();
        };
        btnRm.Click += (_, _) =>
        {
            if (_lstDirs.SelectedIndex < 0) return;
            _dirs.RemoveAt(_lstDirs.SelectedIndex);
            _lstDirs.Items.RemoveAt(_lstDirs.SelectedIndex);
            RescanAll();
        };
        _lstDirs.SelectedIndexChanged += (_, _) => btnRm.Enabled = _lstDirs.SelectedIndex >= 0;
        btnScan.Click += (_, _) => RescanAll();

        _lblScan = new Label { Text = "请先添加模型所在目录。", Location = new Point(8, 372), Size = new Size(720, 40) };

        p.Controls.AddRange([lblDirs, _lstDirs, btnAdd, btnRm, btnScan, _lvwModels, _lblScan]);
        _body.Controls.Add(p);

        foreach (var d in _cfg.ModelDirs) { _dirs.Add(d); _lstDirs.Items.Add(d); }
        if (_dirs.Count > 0) RescanAll();
    }

    bool btnSetDefaultReady; // placeholder to silence unused-warning style concerns

    void RescanAll()
    {
        _scan.Clear();
        _lvwModels.BeginUpdate();
        _lvwModels.Items.Clear();
        UseWaitCursor = true;
        try
        {
            foreach (var dir in _dirs)
            {
                if (!Directory.Exists(dir)) continue;
                foreach (var f in Directory.EnumerateFiles(dir, "*.gguf", SearchOption.TopDirectoryOnly))
                {
                    var info = GgufParser.Parse(f);
                    _scan[f] = info;
                    var it = new ListViewItem(Path.GetFileName(f)) { Tag = info };
                    if (!info.Ok)
                    {
                        it.SubItems.Add("-"); it.SubItems.Add("-");
                        it.SubItems.Add("-");
                        it.SubItems.Add("-");
                        it.ForeColor = Color.Gray;
                        it.ToolTipText = info.Error;
                    }
                    else
                    {
                        it.SubItems.Add($"{info.FileSize / (1024d * 1024 * 1024):N1} GB");
                        it.SubItems.Add(info.QuantLabel);
                        it.SubItems.Add(info.NativeContext > 0 ? $"{info.NativeContext / 1024}k" : "-");
                        it.SubItems.Add(info.HasNextnTensors ? "✓" : "-");
                    }
                    _lvwModels.Items.Add(it);
                }
            }
        }
        finally { UseWaitCursor = false; _lvwModels.EndUpdate(); }

        var okCount = _scan.Values.Count(x => x.Ok);
        _lblScan.Text = okCount == 0
            ? "⚠ 所选目录下没有找到可用的 .gguf 模型文件。"
            : $"已找到 {okCount} 个可用模型（灰色行解析失败，将被忽略）。";
    }

    // ───────────────────────── step 3 ─────────────────────────
    void BuildStep3()
    {
        var p = new Panel { Name = "p2", Dock = DockStyle.Fill, Visible = false };
        var lbl = new Label { Text = "默认启动的模型：", Location = new Point(8, 20), AutoSize = true };
        _cboModel = new ComboBox
        {
            DropDownStyle = ComboBoxStyle.DropDownList,
            Location = new Point(8, 44), Size = new Size(720, 26),
        };
        _cboModel.SelectedIndexChanged += (_, _) => RefreshRiskHint();
        _lblModelInfo = new Label
        {
            Location = new Point(8, 84), Size = new Size(720, 220),
            Font = new Font("微软雅黑", 9),
        };
        p.Controls.AddRange([lbl, _cboModel, _lblModelInfo]);
        _body.Controls.Add(p);
    }

    void FillModelCombo()
    {
        _cboModel.Items.Clear();
        foreach (var m in _scan.Values.Where(x => x.Ok).OrderBy(x => x.FilePath))
            _cboModel.Items.Add(m);

        // 默认优先 Ornith-1.5-9B，其次第一个
        var idx = _scan.Values.Where(x => x.Ok).ToList().FindIndex(m =>
            Path.GetFileName(m.FilePath).Contains("ornith", StringComparison.OrdinalIgnoreCase) &&
            Path.GetFileName(m.FilePath).Contains("9b", StringComparison.OrdinalIgnoreCase));
        _cboModel.SelectedIndex = idx >= 0 ? idx :
            _cboModel.Items.Count > 0 ? 0 : -1;
    }

    void RefreshRiskHint()
    {
        if (_cboModel.SelectedItem is not GgufModelInfo m) { _lblModelInfo.Text = ""; return; }

        var risk = PresetEngine.AssessRisk(m,
            new InferParams { ContextTokens = _preset.ContextTokens, KvLevel = _preset.KvLevel },
            _bestGpu?.VramGb ?? 0, _bestGpu is { IsNvidia: true });

        var nativeCtx = m.NativeContext > 0 ? $"{m.NativeContext / 1024}k" : "未知";
        _lblModelInfo.Text =
            $"文件：{m.FilePath}\r\n" +
            $"架构 {m.Arch} · 层数 {m.Layers} · 量化 {_preset.Label}/{m.QuantLabel} · 大小 {m.FileSize / (1024d * 1024 * 1024):N1} GB\r\n" +
            $"原生上下文 {nativeCtx}" +
            (m.HasNextnTensors ? " · 含 MTP 层（可在设置里开启投机解码）" : "") + "\r\n\r\n" +
            (risk ?? "✔ 以当前预设运行没有显存风险。");
    }

    // ───────────────────────── step 4 ─────────────────────────
    void BuildStep4()
    {
        var p = new Panel { Name = "p3", Dock = DockStyle.Fill, Visible = false };
        var lblPort = new Label { Text = "服务端口：", Location = new Point(8, 20), AutoSize = true };
        _nudPort = new NumericUpDown
        {
            Minimum = 1024, Maximum = 65535, Value = Math.Clamp(_cfg.Port, 1024, 65535),
            Location = new Point(90, 16), Size = new Size(120, 26),
        };
        _lblSummary = new Label { Location = new Point(8, 70), Size = new Size(720, 200), Font = new Font("微软雅黑", 9) };
        _chkStart = new CheckBox
        {
            Text = "完成后立即启动服务",
            Checked = true, Location = new Point(8, 280), AutoSize = true,
        };
        p.Controls.AddRange([lblPort, _nudPort, _lblSummary, _chkStart]);
        _body.Controls.Add(p);
    }

    void FillSummary()
    {
        var model = _cboModel.SelectedItem as GgufModelInfo;
        _lblSummary.Text =
            $"配置摘要\r\n──────────────────────\r\n" +
            $"默认模型：{(model != null ? Path.GetFileName(model.FilePath) : "-")}\r\n" +
            $"服务端口：{(int)_nudPort.Value}（API: http://127.0.0.1:{(int)_nudPort.Value}/ ）\r\n" +
            $"推理预设：{_preset.Label} —— 上下文 {_preset.ContextTokens}, KV {_preset.KvLevel}\r\n" +
            $"模型库目录：{_dirs.Count} 个\r\n\r\n" +
            "以上全部设置之后都可以在主窗口的「设置」页随时修改。";
    }
}
