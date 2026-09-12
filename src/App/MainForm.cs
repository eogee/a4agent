using a4agent.Core.Config;
using a4agent.Core.Engine;
using a4agent.Core.Gguf;
using a4agent.Core.Update;

namespace a4agent.App;

public sealed class MainForm : Form
{
    readonly AppConfig _cfg;
    readonly ServerEngine _engine = new();
    BackendCapabilities _caps = new();

    TabControl _tabs = null!;
    Label _lblState = null!;
    Button _btnStart = null!, _btnStop = null!;
    TextBox _txtLog = null!;
    ListView _lvwModels = null!;
    Label _lblDefaultModel = null!;

    NumericUpDown _nudPort = null!, _nudCtx = null!, _nudNgl = null!,
                  _nudThreads = null!, _nudUb = null!, _nudPar = null!,
                  _nudMtp = null!, _nudTrimSec = null!;
    ComboBox _cboKv = null!;
    CheckBox _chkLan = null!, _chkFa = null!, _chkTrim = null!;
    TextBox _txtExtra = null!, _txtEngineDir = null!;
    Button _btnSaveApply = null!;
    Button _btnCheckUpdate = null!;
    Label _lblUpdate = null!;
    bool _checkingUpdate;

    NotifyIcon _tray = null!;
    bool _balloonShown;

    TabPage _tabConnect = null!;
    TextBox _txtBaseUrl = null!, _txtChatUrl = null!, _txtModelId = null!, _txtCurl = null!, _txtPyClient = null!;
    Label _lblLan = null!;

    public MainForm(AppConfig cfg, bool startImmediately)
    {
        _cfg = cfg;

        Text = "a4agent 控制台";
        try { Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(980, 640);
        MinimumSize = new Size(860, 540);

        BuildTabs();
        BuildTray();

        _engine.Log += line => SafeUi(() => AppendLog(line));
        _engine.StateChanged += s => SafeUi(() =>
        {
            UpdateStateUi(s);
            if (s == EngineState.Failed && !_balloonShown)
            {
                _tray.ShowBalloonTip(4000, "a4agent", "服务启动失败，请打开主窗口查看日志。", ToolTipIcon.Error);
                _balloonShown = true;
            }
        });

        Load += (_, _) =>
        {
            AdoptOrPromptEngine();
            LoadSettingsToControls();
            RescanModels();
            ProbeCapabilities();
            if (startImmediately) StartServer();
            _ = SilentCheckAfterDelayAsync();
        };
        FormClosing += OnFormClosing;
    }

    // ───────────────────────── 引擎接管与初始引导 ─────────────────────────

    /// <summary>启动时：默认目录没引擎则探测旧版本安装目录并自动接管；全机无引擎时提供重新向导。</summary>
    void AdoptOrPromptEngine()
    {
        var found = EngineLocator.FindExisting(_cfg.EngineDir, AppContext.BaseDirectory);
        if (found == null)
        {
            var choice = MessageBox.Show(this,
                "未检测到推理引擎（llama-server.exe）。\r\n\r\n" +
                "「是」立即重新运行配置向导，自动下载引擎；\r\n" +
                "「否」稍后在「设置 → 配置向导」处理。",
                "a4agent 初始引导", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
            if (choice == DialogResult.Yes) RunWizardAgain();
            return;
        }

        var effective = Path.TrimEndingDirectorySeparator(Path.GetFullPath(_cfg.EffectiveEngineDir));
        if (!string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(found)), effective, StringComparison.OrdinalIgnoreCase))
        {
            _cfg.EngineDir = found;
            ConfigStore.Save(_cfg);
            AppendLog($"[引擎] 已自动接管已有引擎目录: {found}");
        }
    }

    /// <summary>重新运行首次配置向导（可重新下载引擎 / 调整模型与端口）。服务运行中会先停止。</summary>
    void RunWizardAgain()
    {
        if (_engine.IsBusy)
        {
            AppendLog("[向导] 先停止服务再重新配置…");
            _engine.Stop();
        }
        using var wizard = new WizardForm(_cfg);
        if (wizard.ShowDialog(this) != DialogResult.OK) return;
        LoadSettingsToControls();
        RefreshConnectTab();
        AppendLog("[配置] 已重新完成配置向导");
        if (wizard.LaunchAfterFinish && !_engine.IsBusy) StartServer();
    }

    // ───────────────────────── 软件更新 ─────────────────────────

    async Task SilentCheckAfterDelayAsync()
    {
        try { await Task.Delay(3000); await CheckUpdateAsync(silent: true); } catch { /* 后台检查，失败不打扰 */ }
    }

    async Task CheckUpdateAsync(bool silent)
    {
        if (_checkingUpdate) return;
        _checkingUpdate = true;
        _btnCheckUpdate.Enabled = false;
        _lblUpdate.ForeColor = Color.DimGray;
        _lblUpdate.Text = "正在检查更新…";

        var r = await Updater.CheckAsync();
        _checkingUpdate = false;
        _btnCheckUpdate.Enabled = true;

        switch (r.Status)
        {
            case UpdateCheckStatus.UpdateAvailable:
                _lblUpdate.ForeColor = Color.DarkSlateBlue;
                _lblUpdate.Text = $"发现新版本 v{r.LatestVersion}（当前 v{r.CurrentVersion}）";
                if (silent)
                    _tray.ShowBalloonTip(5000, "a4agent",
                        $"发现新版本 v{r.LatestVersion}，到「设置」页可一键更新。", ToolTipIcon.Info);
                else if (new UpdateDialog(r).ShowDialog(this) == DialogResult.OK)
                    ReallyExit();   // 安装器已拉起：停止引擎并退出，释放文件锁让安装器覆盖
                break;
            case UpdateCheckStatus.UpToDate:
                _lblUpdate.ForeColor = Color.DimGray;
                _lblUpdate.Text = $"已是最新版本 v{r.CurrentVersion}";
                if (!silent) MessageBox.Show($"已是最新版本（v{r.CurrentVersion}）。", "检查更新",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                break;
            case UpdateCheckStatus.Ignored:
                _lblUpdate.ForeColor = Color.DimGray;
                _lblUpdate.Text = $"已跳过版本 v{r.LatestVersion}（当前 v{r.CurrentVersion}）";
                break;
            case UpdateCheckStatus.TooOld:
                _lblUpdate.ForeColor = Color.Firebrick;
                _lblUpdate.Text = r.Error;
                if (!silent) MessageBox.Show(r.Error, "检查更新", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                break;
            default:
                _lblUpdate.ForeColor = Color.Firebrick;
                _lblUpdate.Text = silent ? $"当前版本 v{r.CurrentVersion}（检查更新失败）" : "检查更新失败";
                if (!silent) MessageBox.Show("检查更新失败：" + r.Error, "检查更新",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                break;
        }
    }

    // ═══════════════════════ UI 构建 ═══════════════════════

    void BuildTabs()
    {
        _tabs = new TabControl { Dock = DockStyle.Fill };
        var tabStatus = new TabPage("状态");
        var tabModels = new TabPage("模型");
        var tabSettings = new TabPage("设置");
        _tabConnect = new TabPage("接入");
        BuildStatusTab(tabStatus);
        BuildModelsTab(tabModels);
        BuildSettingsTab(tabSettings);
        BuildConnectTab(_tabConnect);
        _tabs.TabPages.AddRange([tabStatus, tabModels, tabSettings, _tabConnect]);
        _tabs.SelectedIndexChanged += (_, _) =>
        {
            if (ReferenceEquals(_tabs.SelectedTab, _tabConnect)) RefreshConnectTab();
        };
        Controls.Add(_tabs);
    }

    void BuildStatusTab(TabPage page)
    {
        var top = new Panel { Dock = DockStyle.Top, Height = 56 };

        _lblState = new Label
        {
            Text = "● 已停止", Font = new Font("微软雅黑", 14, FontStyle.Bold),
            ForeColor = Color.Gray, AutoSize = true,
            Location = new Point(12, 12),
        };
        _btnStart = new Button
        {
            Text = "启动", Size = new Size(96, 32), Location = new Point(700, 10),
            BackColor = Color.Honeydew,
        };
        _btnStop = new Button
        {
            Text = "停止", Size = new Size(96, 32), Location = new Point(804, 10),
            Enabled = false,
        };
        _btnStart.Click += (_, _) => StartServer();
        _btnStop.Click += (_, _) => _engine.Stop();

        top.Controls.AddRange([_lblState, _btnStart, _btnStop]);

        _txtLog = new TextBox
        {
            Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
            Dock = DockStyle.Fill, BackColor = SystemColors.Window,
            Font = new Font(FontFamily.GenericMonospace, 9),
        };

        page.Controls.Add(_txtLog);
        page.Controls.Add(top);
    }

    void BuildModelsTab(TabPage page)
    {
        var top = new Panel { Dock = DockStyle.Top, Height = 46 };

        var btnAddDir = new Button { Text = "添加目录...", Size = new Size(110, 30), Location = new Point(8, 8) };
        var btnRescan = new Button { Text = "重新扫描", Size = new Size(100, 30), Location = new Point(124, 8) };
        var btnSetDefault = new Button { Text = "设为默认模型", Size = new Size(130, 30), Location = new Point(230, 8) };
        btnAddDir.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = "选择包含 .gguf 模型文件的目录" };
            if (dlg.ShowDialog(this) != DialogResult.OK) return;
            if (!_cfg.ModelDirs.Contains(dlg.SelectedPath))
                _cfg.ModelDirs.Add(dlg.SelectedPath);
            ConfigStore.Save(_cfg);
            RescanModels();
        };
        btnRescan.Click += (_, _) => RescanModels();
        btnSetDefault.Click += (_, _) =>
        {
            if (_lvwModels.SelectedItems.Count == 0 ||
                _lvwModels.SelectedItems[0].Tag is not GgufModelInfo m || !m.Ok)
            {
                MessageBox.Show("请先选中一个有效模型。", "提示"); return;
            }
            _cfg.DefaultModelPath = m.FilePath;
            ConfigStore.Save(_cfg);
            RefreshDefaultLabel();
            AppendLog($"[配置] 默认模型 -> {m.FilePath}");
        };

        top.Controls.AddRange([btnAddDir, btnRescan, btnSetDefault]);

        _lvwModels = new ListView
        {
            View = View.Details, FullRowSelect = true, HideSelection = false, Dock = DockStyle.Fill,
        };
        _lvwModels.Columns.Add("文件", 340);
        _lvwModels.Columns.Add("大小", 90);
        _lvwModels.Columns.Add("量化", 80);
        _lvwModels.Columns.Add("原生上下文", 100);
        _lvwModels.Columns.Add("MTP", 60);

        _lblDefaultModel = new Label
        {
            Dock = DockStyle.Bottom, Height = 30, TextAlign = ContentAlignment.MiddleLeft,
            Padding = new Padding(8, 4, 0, 0),
        };

        page.Controls.AddRange([_lvwModels, _lblDefaultModel, top]);
    }

    void BuildSettingsTab(TabPage page)
    {
        var scroll = new Panel
        {
            Dock = DockStyle.Fill, AutoScroll = true,
            Padding = new Padding(12),
        };
        var table = new TableLayoutPanel
        {
            Dock = DockStyle.Top, AutoSize = true, ColumnCount = 4,
            ColumnStyles = { new ColumnStyle(SizeType.Absolute, 150), new ColumnStyle(SizeType.Absolute, 220),
                             new ColumnStyle(SizeType.Absolute, 160), new ColumnStyle(SizeType.Percent, 100) },
        };

        int AddRow(string label, Control ctl, Control? extra = null)
        {
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(new Label
            { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 3, 3) },
                0, table.RowCount);
            ctl.Margin = new Padding(3, 4, 3, 4);
            table.Controls.Add(ctl, 1, table.RowCount);
            if (extra != null) { extra.Margin = new Padding(3, 4, 3, 4); table.Controls.Add(extra, 2, table.RowCount); }
            table.RowCount++;
            return table.RowCount - 1;
        }

        _nudPort = new NumericUpDown { Minimum = 1024, Maximum = 65535 };
        AddRow("服务端口 (默认 8080)", _nudPort, new Label { Text = "", AutoSize = true });

        _chkLan = new CheckBox { Text = "允许局域网设备访问", AutoSize = true };
        AddRow("局域网访问", _chkLan, new Label { Text = "勾选 = 0.0.0.0；首次使用请放行防火墙", AutoSize = true, ForeColor = Color.DimGray });

        _txtEngineDir = new TextBox { Width = 210, ReadOnly = true, BackColor = SystemColors.Window };
        var btnEng = new Button { Text = "浏览...", Size = new Size(70, 26) };
        btnEng.Click += (_, _) =>
        {
            using var dlg = new FolderBrowserDialog { Description = "选择包含 llama-server.exe 的引擎目录" };
            if (dlg.ShowDialog(this) != DialogResult.OK) return;
            _txtEngineDir.Text = dlg.SelectedPath;
            ApplyEngineDirAndProbe();
        };
        AddRow("引擎目录", _txtEngineDir, btnEng);
        var engHint = new Label
        {
            Text = "「引擎」指 llama-server 推理后端本体：即 llama-server.exe 及其依赖 DLL 所在的目录。" +
                   "默认使用安装目录下的 engine\\ 子目录；把它改指向其它后端包的 engine 目录" +
                   "（例如 Vulkan 版安装目录），即可不重装直接切换显卡后端。修改并保存后需重启服务生效。",
            ForeColor = Color.DimGray, AutoSize = false, Size = new Size(620, 46),
            Margin = new Padding(3, 2, 3, 2),
        };
        table.Controls.Add(engHint, 1, table.RowCount);
        table.SetColumnSpan(engHint, 3);
        table.RowCount++;

        _nudCtx = new NumericUpDown { Minimum = 512, Maximum = 1048576, Increment = 512 };
        AddRow("上下文长度 (tokens)", _nudCtx, new Label { Text = "预设档会自动推荐", AutoSize = true, ForeColor = Color.DimGray });

        _cboKv = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
        _cboKv.Items.AddRange(["f16", "q8_0", "q4_0"]);
        _chkFa = new CheckBox { Text = "Flash Attention", Checked = true, AutoSize = true };
        AddRow("KV 缓存级别", _cboKv, _chkFa);

        _nudNgl = new NumericUpDown { Minimum = 0, Maximum = 999 };
        AddRow("GPU 层数 (-ngl)", _nudNgl, new Label { Text = "99 = 全量 offload", AutoSize = true, ForeColor = Color.DimGray });

        _nudMtp = new NumericUpDown { Minimum = 0, Maximum = 10 };
        AddRow("MTP 投机步数", _nudMtp, new Label { Text = "0=关闭；需引擎与模型支持", AutoSize = true, ForeColor = Color.DimGray });

        _nudThreads = new NumericUpDown { Minimum = 0, Maximum = 256 };
        AddRow("CPU 线程数", _nudThreads, new Label { Text = "0 = 自动", AutoSize = true, ForeColor = Color.DimGray });

        _nudUb = new NumericUpDown { Minimum = 16, Maximum = 16384, Increment = 16 };
        AddRow("ubatch", _nudUb);

        _nudPar = new NumericUpDown { Minimum = 1, Maximum = 16 };
        AddRow("并行槽位 (--parallel)", _nudPar);

        _txtExtra = new TextBox { Width = 380, PlaceholderText = "额外参数，空格分隔，原样追加" };
        AddRow("额外参数", _txtExtra);

        _chkTrim = new CheckBox { Text = "启动后自动裁剪内存（释放文件缓存）", AutoSize = true };
        _nudTrimSec = new NumericUpDown { Minimum = 5, Maximum = 3600 };
        AddRow("", _chkTrim, _nudTrimSec);

        var btnWizard = new Button { Text = "重新运行向导", Size = new Size(110, 28) };
        btnWizard.Click += (_, _) => RunWizardAgain();
        AddRow("配置向导", btnWizard, new Label
        {
            Text = "重新检测硬件、下载引擎、选择模型与端口", AutoSize = true, ForeColor = Color.DimGray,
        });

        _btnCheckUpdate = new Button { Text = "检查更新", Size = new Size(110, 28) };
        _btnCheckUpdate.Click += async (_, _) => await CheckUpdateAsync(false);
        _lblUpdate = new Label
        {
            Text = $"当前版本 v{Updater.CurrentVersion}", AutoSize = true,
            ForeColor = Color.DimGray, Anchor = AnchorStyles.Left,
        };
        AddRow("软件更新", _btnCheckUpdate, _lblUpdate);

        _btnSaveApply = new Button { Text = "保存设置", Size = new Size(120, 32) };
        _btnSaveApply.Click += (_, _) => SaveSettings();
        AddRow("", _btnSaveApply);

        scroll.Controls.Add(table);
        page.Controls.Add(scroll);
    }

    void BuildTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("显示主窗口", null, (_, _) => RestoreFromTray());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("启动服务", null, (_, _) => StartServer());
        menu.Items.Add("停止服务", null, (_, _) => _engine.Stop());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("打开 WebUI", null, (_, _) => OpenWebUi());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出（会先停止服务）", null, (_, _) => ReallyExit());

        _tray = new NotifyIcon
        {
            Icon = Ui.IconForState(EngineState.Stopped),
            Visible = true,
            Text = "a4agent",
            ContextMenuStrip = menu,
        };
        _tray.DoubleClick += (_, _) => RestoreFromTray();
    }

    // ═══════════════════════ 行为 ═══════════════════════

    // ───────────────────────── 接入页 ─────────────────────────
    void BuildConnectTab(TabPage page)
    {
        var scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(12) };

        Label Head(string t, int y) => new()
        { Text = t, Location = new Point(4, y), AutoSize = true, Font = new Font("微软雅黑", 9, FontStyle.Bold) };
        TextBox Box(int y) => new()
        {
            ReadOnly = true, BackColor = SystemColors.Window,
            Location = new Point(4, y), Width = 640,
        };
        Button CopyBtn(int y, Func<string> get)
        {
            var b = new Button { Text = "复制", Location = new Point(652, y - 1), Size = new Size(70, 26) };
            b.Click += (_, _) =>
            {
                try { Clipboard.SetText(get()); AppendLog("[复制] 已复制到剪贴板"); }
                catch (Exception ex) { MessageBox.Show("复制失败: " + ex.Message); }
            };
            return b;
        }

        int y = 8;
        scroll.Controls.Add(Head("Base URL（OpenAI 兼容接口根地址）", y));
        _txtBaseUrl = Box(y + 22); scroll.Controls.Add(_txtBaseUrl);
        scroll.Controls.Add(CopyBtn(y + 23, () => _txtBaseUrl.Text));
        y += 62;

        scroll.Controls.Add(Head("Chat Completions 完整地址", y));
        _txtChatUrl = Box(y + 22); scroll.Controls.Add(_txtChatUrl);
        scroll.Controls.Add(CopyBtn(y + 23, () => _txtChatUrl.Text));
        y += 62;

        scroll.Controls.Add(Head("模型名称（model 字段填这个）", y));
        _txtModelId = Box(y + 22); scroll.Controls.Add(_txtModelId);
        scroll.Controls.Add(CopyBtn(y + 23, () => _txtModelId.Text));
        y += 62;

        scroll.Controls.Add(new Label
        {
            Text = "API Key：服务未启用鉴权，客户端里随便填或留空即可。",
            Location = new Point(4, y), AutoSize = true, ForeColor = Color.DimGray,
        });
        y += 30;
        _lblLan = new Label
        {
            Text = "", Location = new Point(4, y), AutoSize = false,
            Size = new Size(720, 40), ForeColor = Color.DarkSlateBlue,
        };
        scroll.Controls.Add(_lblLan);
        y += 50;

        scroll.Controls.Add(Head("curl 调用示例", y));
        _txtCurl = new TextBox
        {
            ReadOnly = true, BackColor = SystemColors.Window, Multiline = true,
            Location = new Point(4, y + 22), Width = 640, Height = 96,
            Font = new Font(FontFamily.GenericMonospace, 9), ScrollBars = ScrollBars.Vertical,
        };
        scroll.Controls.Add(_txtCurl);
        scroll.Controls.Add(CopyBtn(y + 23, () => _txtCurl.Text));
        y += 130;

        scroll.Controls.Add(Head("Python (openai SDK) 示例", y));
        _txtPyClient = new TextBox
        {
            ReadOnly = true, BackColor = SystemColors.Window, Multiline = true,
            Location = new Point(4, y + 22), Width = 640, Height = 120,
            Font = new Font(FontFamily.GenericMonospace, 9), ScrollBars = ScrollBars.Vertical,
        };
        scroll.Controls.Add(_txtPyClient);
        scroll.Controls.Add(CopyBtn(y + 23, () => _txtPyClient.Text));

        page.Controls.Add(scroll);
    }

    void RefreshConnectTab()
    {
        const string probeHost = "127.0.0.1";
        var baseUrl = $"http://{probeHost}:{_cfg.Port}/v1";
        var modelId = string.IsNullOrEmpty(_cfg.DefaultModelPath)
            ? "(尚未设置默认模型)"
            : Path.GetFileNameWithoutExtension(_cfg.DefaultModelPath);

        _txtBaseUrl.Text = baseUrl;
        _txtChatUrl.Text = baseUrl + "/chat/completions";
        _txtModelId.Text = modelId;

        _lblLan.Text = _cfg.Host == "0.0.0.0"
            ? $"已允许局域网访问：其他设备可改用 http://{Ui.GetLanIPv4() ?? "<本机IP>"}:{_cfg.Port}/v1"
            : "当前仅本机可访问。如需局域网调用：设置页勾选「允许局域网设备访问」并重启服务。";

        _txtCurl.Text =
            "curl " + baseUrl + "/chat/completions \\\r\n" +
            "  -H \"Content-Type: application/json\" \\\r\n" +
            "  -d \"{\\\"model\\\": \\\"" + modelId + "\\\", \\\"messages\\\": [{\\\"role\\\": \\\"user\\\", \\\"content\\\": \\\"你好\\\"}]}\"";

        _txtPyClient.Text =
            "from openai import OpenAI\r\n" +
            "\r\n" +
            "client = OpenAI(base_url=\"" + baseUrl + "\", api_key=\"none\")\r\n" +
            "\r\n" +
            "resp = client.chat.completions.create(\r\n" +
            "    model=\"" + modelId + "\",\r\n" +
            "    messages=[{\"role\": \"user\", \"content\": \"你好\"}],\r\n" +
            ")\r\n" +
            "print(resp.choices[0].message.content)";
    }

    void ProbeCapabilities()
    {
        _caps = BackendCapabilities.Detect(_cfg.EffectiveEngineDir);
        _nudMtp.Enabled = _caps.SupportsMtp;
        if (!_caps.SupportsMtp)
        {
            _nudMtp.Value = 0;
            AppendLog("[探测] 当前引擎不支持 --mtp 参数（MTP 步数已锁定为关闭）");
        }
    }

    void ApplyEngineDirAndProbe()
    {
        _cfg.EngineDir = _txtEngineDir.Text.Trim();
        ConfigStore.Save(_cfg);
        ProbeCapabilities();
    }

    void StartServer()
    {
        ApplyControlsToConfig();
        ConfigStore.Save(_cfg);
        if (_engine.IsBusy) { AppendLog("[跳过] 服务已在运行"); return; }
        _engine.Start(_cfg);
        _tabs.SelectedIndex = 0; // 切到状态页看日志
    }

    void ApplyControlsToConfig()
    {
        _cfg.Port = (int)_nudPort.Value;
        _cfg.Host = _chkLan.Checked ? "0.0.0.0" : "127.0.0.1";
        _cfg.EngineDir = _txtEngineDir.Text.Trim();
        _cfg.Infer.ContextTokens = (int)_nudCtx.Value;
        _cfg.Infer.KvLevel = _cboKv.SelectedItem?.ToString() ?? "q4_0";
        _cfg.Infer.FlashAttention = _chkFa.Checked;
        _cfg.Infer.Ngl = (int)_nudNgl.Value;
        _cfg.Infer.Threads = (int)_nudThreads.Value;
        _cfg.Infer.Ubatch = (int)_nudUb.Value;
        _cfg.Infer.Parallel = (int)_nudPar.Value;
        _cfg.Infer.MtpSteps = (int)_nudMtp.Value;
        _cfg.Infer.ExtraArgs = _txtExtra.Text.Trim();
        _cfg.Run.AutoTrimRam = _chkTrim.Checked;
        _cfg.Run.TrimDelaySeconds = (int)_nudTrimSec.Value;
    }

    void LoadSettingsToControls()
    {
        _nudPort.Value = Math.Clamp(_cfg.Port, 1024, 65535);
        _chkLan.Checked = _cfg.Host == "0.0.0.0";
        _txtEngineDir.Text = _cfg.EngineDir;
        _nudCtx.Value = Math.Clamp(_cfg.Infer.ContextTokens, 512, 1048576);
        _cboKv.SelectedItem = _cfg.Infer.KvLevel switch
        {
            "f16" => "f16", "q8_0" => "q8_0", _ => "q4_0",
        };
        _chkFa.Checked = _cfg.Infer.FlashAttention;
        _nudNgl.Value = Math.Clamp(_cfg.Infer.Ngl, 0, 999);
        _nudThreads.Value = Math.Clamp(_cfg.Infer.Threads, 0, 256);
        _nudUb.Value = Math.Clamp(_cfg.Infer.Ubatch, 16, 16384);
        _nudPar.Value = Math.Clamp(_cfg.Infer.Parallel, 1, 16);
        _nudMtp.Value = Math.Clamp(_cfg.Infer.MtpSteps, 0, 10);
        _txtExtra.Text = _cfg.Infer.ExtraArgs;
        _chkTrim.Checked = _cfg.Run.AutoTrimRam;
        _nudTrimSec.Value = Math.Clamp(_cfg.Run.TrimDelaySeconds, 5, 3600);
        RefreshDefaultLabel();
    }

    void SaveSettings()
    {
        ApplyControlsToConfig();
        ConfigStore.Save(_cfg);
        RefreshConnectTab();
        AppendLog("[配置] 设置已保存（运行中的服务重启后生效）");
        MessageBox.Show("设置已保存。\r\n若服务正在运行，需要「停止 → 启动」才会应用新参数。",
            "保存成功", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    void RescanModels()
    {
        _lvwModels.BeginUpdate();
        _lvwModels.Items.Clear();
        try
        {
            foreach (var m in Ui.ScanDirs(_cfg.ModelDirs))
            {
                var it = new ListViewItem(Path.GetFileName(m.FilePath)) { Tag = m };
                Ui.FillModelColumns(it, m);
                if (_cfg.DefaultModelPath.Equals(m.FilePath, StringComparison.OrdinalIgnoreCase))
                    it.Selected = true;
                _lvwModels.Items.Add(it);
            }
        }
        finally { _lvwModels.EndUpdate(); }
        RefreshDefaultLabel();
    }

    void RefreshDefaultLabel()
    {
        _lblDefaultModel.Text = string.IsNullOrEmpty(_cfg.DefaultModelPath)
            ? "默认模型：（未设置——请到本页选择后点「设为默认模型」）"
            : $"默认模型：{_cfg.DefaultModelPath}";
    }

    void AppendLog(string line)
    {
        if (!IsHandleCreated) return;
        _txtLog.AppendText($"{DateTime.Now:HH:mm:ss}  {line}\r\n");
    }

    void SafeUi(Action a)
    {
        if (!IsHandleCreated) return;
        try { BeginInvoke(a); } catch (ObjectDisposedException) { }
    }

    void UpdateStateUi(EngineState s)
    {
        _lblState.Text = s switch
        {
            EngineState.Running => "● 运行中",
            EngineState.Starting => "◐ 启动中...",
            EngineState.Failed => "✖ 失败",
            _ => "● 已停止",
        };
        _lblState.ForeColor = s switch
        {
            EngineState.Running => Color.Green,
            EngineState.Starting => Color.DarkOrange,
            EngineState.Failed => Color.Firebrick,
            _ => Color.Gray,
        };
        _btnStart.Enabled = !s.IsRunningOrStarting();
        _btnStop.Enabled = s.IsRunningOrStarting();
        _tray.Icon = Ui.IconForState(s);
        _tray.Text = $"a4agent — {_lblState.Text}";
    }

    void OpenWebUi()
    {
        var port = _cfg.Port;
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo($"http://127.0.0.1:{port}/") { UseShellExecute = true });
        }
        catch (Exception ex) { MessageBox.Show("打开浏览器失败: " + ex.Message); }
    }

    void RestoreFromTray()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    void ReallyExit()
    {
        if (_engine.IsBusy) _engine.Stop();
        _tray.Visible = false;
        Application.Exit();
    }

    void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (e.CloseReason != CloseReason.UserClosing) return;
        if (!_engine.IsBusy) { _tray.Visible = false; return; }

        // 最小化到托盘而不是退出，保持服务存活
        e.Cancel = true;
        Hide();
        if (!_balloonShown)
        {
            _tray.ShowBalloonTip(3000, "a4agent",
                "程序已缩到托盘，服务继续运行。右键托盘图标可退出。", ToolTipIcon.Info);
            _balloonShown = true;
        }
    }
}

internal static class EngineStateExtensions
{
    public static bool IsRunningOrStarting(this EngineState s)
        => s is EngineState.Running or EngineState.Starting;
}
