using a4agent.App.Web;
using a4agent.Core.Config;
using a4agent.Core.Engine;
using a4agent.Core.Update;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System.Diagnostics;

namespace a4agent.App;

/// <summary>
/// 主窗体：WebView2 承载 layui 控制台（http://127.0.0.1:{port}/），
/// 保留托盘常驻与关闭最小化行为；WebView2 Runtime 缺失时降级为系统浏览器打开。
/// 原生配置向导（WizardForm）保留，由控制台经 IUiBridge 拉起。
/// </summary>
public sealed class MainForm : Form
{
    readonly AppConfig _cfg;
    readonly ServerEngine _engine = new();
    ControlServer _server = null!;
    WebView2 _web = null!;
    Label _fallbackLabel = null!;
    NotifyIcon _tray = null!;
    bool _balloonShown;
    string _consoleUrl = "";

    public MainForm(AppConfig cfg, bool startImmediately)
    {
        _cfg = cfg;

        Text = "a4agent 控制台";
        try { Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(1080, 720);
        MinimumSize = new Size(900, 600);

        BuildWebView();
        BuildTray();

        _server = new ControlServer(_cfg, _engine, new UiBridge(this));
        _engine.Log += line => _server.Log(line);
        _engine.StateChanged += s => SafeUi(() =>
        {
            _tray.Icon = Ui.IconForState(s);
            _tray.Text = $"a4agent — {StateText(s)}";
            if (s == EngineState.Failed && !_balloonShown)
            {
                _tray.ShowBalloonTip(4000, "a4agent", "服务启动失败，请打开主窗口查看日志。", ToolTipIcon.Error);
                _balloonShown = true;
            }
        });

        Load += async (_, _) =>
        {
            try
            {
                _server.Start();
                _consoleUrl = _server.BaseUrl + "/";
            }
            catch (Exception ex)
            {
                MessageBox.Show("控制台服务启动失败: " + ex.Message, "a4agent",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            await InitWebViewAsync();

            AdoptOrPromptEngine();
            _server.BroadcastState();
            if (startImmediately) StartServer();
            _ = SilentCheckAfterDelayAsync();
        };
        FormClosing += OnFormClosing;
    }

    // ───────────────────────── WebView2 宿主 ─────────────────────────

    void BuildWebView()
    {
        _web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(238, 250, 250) };
        _fallbackLabel = new Label
        {
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("微软雅黑", 10),
            Visible = false,
        };
        Controls.Add(_fallbackLabel);
        Controls.Add(_web);
    }

    async Task InitWebViewAsync()
    {
        try
        {
            var userData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "a4agent", "WebView2");
            var env = await CoreWebView2Environment.CreateAsync(null, userData);
            await _web.EnsureCoreWebView2Async(env);

            var s = _web.CoreWebView2.Settings;
            s.AreDefaultContextMenusEnabled = false;
            s.IsStatusBarEnabled = false;
            s.AreDevToolsEnabled = false;
            _web.CoreWebView2.NewWindowRequested += (_, e) =>
            {
                e.Handled = true;                      // window.open 一律交给系统浏览器
                OpenInBrowser(e.Uri);
            };

            _web.Source = new Uri(_consoleUrl);
        }
        catch (Exception ex)
        {
            // WebView2 Runtime 缺失等 → 降级为系统浏览器打开同一地址
            Log($"[控制台] WebView2 不可用（{ex.Message}），已改用系统浏览器打开控制台");
            _web.Visible = false;
            _fallbackLabel.Visible = true;
            _fallbackLabel.Text = $"控制台已在系统浏览器打开：{_consoleUrl}";
            OpenInBrowser(_consoleUrl);
        }
    }

    void OpenInBrowser(string url)
    {
        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex) { Log("[浏览器] 打开失败: " + ex.Message); }
    }

    // ───────────────────────── UI 桥（控制台 → 原生窗口） ─────────────────────────

    sealed class UiBridge : IUiBridge
    {
        readonly MainForm _f;
        public UiBridge(MainForm f) { _f = f; }
        public void RunWizard() => _f.SafeUi(_f.RunWizardInternal);
        public void RequestExit() => _f.SafeUi(_f.ReallyExit);
    }

    void RunWizardInternal()
    {
        if (_engine.IsBusy)
        {
            Log("[向导] 先停止服务再重新配置…");
            _engine.Stop();
        }
        using var wizard = new WizardForm(_cfg);
        if (wizard.ShowDialog(this) != DialogResult.OK) return;
        Log("[配置] 已重新完成配置向导");
        if (wizard.LaunchAfterFinish && !_engine.IsBusy) StartServer();
        _server.BroadcastState();
    }

    // ───────────────────────── 引擎接管与初始引导 ─────────────────────────

    /// <summary>启动时：默认目录没引擎则探测旧版本安装目录并自动接管；全机无引擎时在控制台提示（不弹原生框）。</summary>
    void AdoptOrPromptEngine()
    {
        var found = EngineLocator.FindExisting(_cfg.EngineDir, AppContext.BaseDirectory);
        if (found == null)
        {
            Log("[引擎] 未检测到推理引擎（llama-server.exe）——请到「设置 → 配置向导」重新运行向导自动下载引擎");
            return;
        }

        var effective = Path.TrimEndingDirectorySeparator(Path.GetFullPath(_cfg.EffectiveEngineDir));
        if (!string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(found)), effective, StringComparison.OrdinalIgnoreCase))
        {
            _cfg.EngineDir = found;
            ConfigStore.Save(_cfg);
            Log($"[引擎] 已自动接管已有引擎目录: {found}");
        }
    }

    // ───────────────────────── 软件更新（静默检查仅托盘气泡提醒） ─────────────────────────

    async Task SilentCheckAfterDelayAsync()
    {
        try { await Task.Delay(3000); } catch { return; }
        try
        {
            var r = await Updater.CheckAsync();
            if (r.Status == UpdateCheckStatus.UpdateAvailable)
            {
                SafeUi(() => _tray.ShowBalloonTip(5000, "a4agent",
                    $"发现新版本 v{r.LatestVersion}，到控制台「检查更新」可一键升级。", ToolTipIcon.Info));
                Log($"[更新] 发现新版本 v{r.LatestVersion}（当前 v{r.CurrentVersion}）");
            }
        }
        catch { /* 后台检查，失败不打扰 */ }
    }

    // ───────────────────────── 托盘 ─────────────────────────

    void BuildTray()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("显示主窗口", null, (_, _) => RestoreFromTray());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("启动服务", null, (_, _) => StartServer());
        menu.Items.Add("停止服务", null, (_, _) => _engine.Stop());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("打开控制台（浏览器）", null, (_, _) => OpenConsole());
        menu.Items.Add("打开 llama-server WebUI", null, (_, _) => OpenWebUi());
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

    void OpenConsole()
    {
        if (_consoleUrl.Length == 0) { RestoreFromTray(); return; }
        OpenInBrowser(_consoleUrl);
    }

    void OpenWebUi()
    {
        try
        {
            Process.Start(new ProcessStartInfo($"http://127.0.0.1:{_cfg.Port}/") { UseShellExecute = true });
        }
        catch (Exception ex) { Log("[浏览器] 打开失败: " + ex.Message); }
    }

    // ───────────────────────── 行为 ─────────────────────────

    void StartServer()
    {
        ConfigStore.Save(_cfg);
        if (_engine.IsBusy) { Log("[跳过] 服务已在运行"); return; }
        _engine.Start(_cfg);
    }

    void Log(string line) => _server.Log(line);

    void SafeUi(Action a)
    {
        try { BeginInvoke(a); }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
    }

    static string StateText(EngineState s) => s switch
    {
        EngineState.Running => "运行中",
        EngineState.Starting => "启动中...",
        EngineState.Failed => "失败",
        _ => "已停止",
    };

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
