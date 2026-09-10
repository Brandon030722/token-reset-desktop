using System.Security.Principal;
using System.Windows;

namespace TiboMonitor.Windows;

public partial class App : Application
{
    private Mutex? instance;
    private bool ownsInstance;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        string user = WindowsIdentity.GetCurrent().User?.Value ?? Environment.UserName;
        instance = new Mutex(true, @"Local\TiboReset.Windows." + user, out ownsInstance);
        if (!ownsInstance)
        {
            MessageBox.Show("Token重置已在运行，请切回已打开的窗口。", "Token重置");
            Shutdown();
            return;
        }
        MainWindow = new MainWindow(e.Args.Contains("--dry-run", StringComparer.Ordinal));
        MainWindow.Show();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        if (ownsInstance) instance?.ReleaseMutex();
        instance?.Dispose();
        base.OnExit(e);
    }
}
