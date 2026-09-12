namespace a4agent.App;

using a4agent.Core.Config;

internal static class Program
{
    private const string MutexName = "a4agent_SingleInstance_Mutex";

    [STAThread]
    private static void Main(string[] args)
    {
        using var mutex = new Mutex(true, MutexName, out var isFirst);
        if (!isFirst)
        {
            MessageBox.Show("a4agent 已经在运行中（请查看托盘图标）。",
                "提示", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();

        // 无头自检：--uitest 只构造窗体验证控件树完整性，用于 CI 式冒烟
        if (args.Any(a => a == "--uitest"))
        {
            var baseDir = AppContext.BaseDirectory;
            try
            {
                var c = ConfigStore.Load();
                using var _mf = new MainForm(c, false);
                using var _wz = new WizardForm(c);
                File.WriteAllText(Path.Combine(baseDir, "uitest.ok"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            }
            catch (Exception ex)
            {
                File.WriteAllText(Path.Combine(baseDir, "uitest.fail"), ex.ToString());
            }
            return;
        }

        var cfg = ConfigStore.Load();
        var startImmediately = false;

        if (!cfg.WizardDone)
        {
            using var wizard = new WizardForm(cfg);
            if (wizard.ShowDialog() != DialogResult.OK) return;
            startImmediately = wizard.LaunchAfterFinish;
        }

        Application.Run(new MainForm(cfg, startImmediately));
    }
}
