using a4agent.Core.Update;

namespace a4agent.App;

/// <summary>
/// 发现新版本弹窗：展示更新说明 → 下载（进度/取消）→ 拉起安装器。
/// 「安装并关闭程序」返回 DialogResult.OK，由调用方停止引擎并退出应用。
/// 「跳过此版本」返回 DialogResult.Ignore，写入忽略清单。
/// </summary>
public sealed class UpdateDialog : Form
{
    readonly UpdateManifest _manifest;
    readonly UpdateAsset _asset;
    CancellationTokenSource? _cts;
    bool _downloading;
    string? _installerPath;

    readonly Label _lblVersions = new()
    { AutoSize = true, Location = new Point(14, 14), Font = new Font("微软雅黑", 10, FontStyle.Bold) };
    readonly TextBox _txtNotes = new()
    {
        ReadOnly = true, BackColor = SystemColors.Window, Multiline = true,
        ScrollBars = ScrollBars.Vertical, Font = new Font(FontFamily.GenericMonospace, 9),
        Location = new Point(14, 44), Size = new Size(532, 220),
    };
    readonly ProgressBar _prg = new()
    { Location = new Point(14, 276), Size = new Size(532, 18), Visible = false };
    readonly Label _lblStatus = new()
    {
        AutoSize = false, Size = new Size(532, 40), Location = new Point(14, 300),
        ForeColor = Color.DimGray,
    };
    readonly Button _btnNow = new()
    { Text = "立即更新", Size = new Size(120, 32), Location = new Point(306, 348), Enabled = false };
    readonly Button _btnSkip = new()
    { Text = "跳过此版本", Size = new Size(104, 32), Location = new Point(190, 348) };
    readonly Button _btnClose = new()
    { Text = "关闭", Size = new Size(88, 32), Location = new Point(80, 348) };

    public UpdateDialog(UpdateCheckResult check)
    {
        _manifest = check.Manifest!;
        _asset = check.Asset!;

        Text = "软件更新";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false; MinimizeBox = false;
        ClientSize = new Size(560, 396);

        _lblVersions.Text = $"新版本 v{check.LatestVersion}  （当前 v{check.CurrentVersion}）";
        _txtNotes.Text = string.IsNullOrWhiteSpace(check.Notes) ? "（无更新说明）" : check.Notes;
        _lblStatus.Text = "下载完成后将校验 SHA256，再由安装器完成升级；期间可正常使用，安装时会自动关闭本程序。";

        _btnNow.Click += async (_, _) => await OnUpdateNowAsync();
        _btnSkip.Click += (_, _) => { Updater.IgnoreVersion(_manifest.Version); DialogResult = DialogResult.Ignore; };
        _btnClose.Click += (_, _) => Close();

        Controls.AddRange([_lblVersions, _txtNotes, _prg, _lblStatus, _btnNow, _btnSkip, _btnClose]);
        FormClosing += (_, e) => { if (_downloading) { _cts?.Cancel(); e.Cancel = true; } };
    }

    async Task OnUpdateNowAsync()
    {
        if (_downloading) return;
        _cts = new CancellationTokenSource();

        if (_installerPath == null)
        {
            // 阶段 1：下载 + 校验
            _downloading = true;
            _btnNow.Enabled = _btnSkip.Enabled = false;
            _btnClose.Text = "取消下载";
            _prg.Visible = true;
            _lblStatus.ForeColor = Color.DimGray;
            var progress = new Progress<(long Received, long Total)>(p =>
            {
                _prg.Style = ProgressBarStyle.Continuous;
                if (p.Total > 0) _prg.Value = Math.Min(100, (int)(100 * p.Received / p.Total));
                _lblStatus.Text = $"下载中 {p.Received / 1048576.0:N1} / {p.Total / 1048576.0:N1} MB"
                                + $"（GitHub → Gitee 自动回退，边下边校验 SHA256）";
            });
            try
            {
                _installerPath = await Updater.DownloadAssetAsync(_manifest, _asset, progress, _cts.Token);
            }
            catch (OperationCanceledException)
            {
                ResetToOffer("已取消下载。");
                return;
            }
            catch (Exception ex)
            {
                ResetToOffer("下载失败：" + ex.Message + "（可稍后重试，或到 Release 页手动下载）");
                return;
            }
            finally { _downloading = false; }

            _prg.Value = 100;
            _lblStatus.ForeColor = Color.DarkGreen;
            _lblStatus.Text = "✔ 下载完成，SHA256 校验通过。安装器会自动完成升级，配置与已下载的引擎全部保留。";
            _btnNow.Text = "安装并关闭程序";
            _btnNow.Enabled = true;
            _btnSkip.Visible = false;
            return;
        }

        // 阶段 2：拉起安装器；返回 OK 让主窗体停止引擎并退出（AppMutex 释放后安装器才可覆盖）
        try
        {
            Updater.Apply(_installerPath);
            DialogResult = DialogResult.OK;
        }
        catch (Exception ex)
        {
            _lblStatus.ForeColor = Color.Firebrick;
            _lblStatus.Text = "启动安装器失败：" + ex.Message;
        }
    }

    void ResetToOffer(string message)
    {
        _lblStatus.ForeColor = Color.Firebrick;
        _lblStatus.Text = message;
        _prg.Visible = false;
        _btnNow.Enabled = true;
        _btnSkip.Enabled = true;
        _btnClose.Text = "关闭";
    }
}
