using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace TiboMonitor.Windows;

public sealed class MainWindow : Window
{
    private const string Origin = "https://tibo.invalid";
    private readonly string home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".tibo-reset");
    private readonly string site = Path.Combine(AppContext.BaseDirectory, "site");
    private readonly bool dryRun;
    private readonly WebView2 web = new();
    private readonly TextBlock status = new() { Text = "正在打开本机观察站…", Margin = new Thickness(12, 6, 12, 6) };
    private readonly DispatcherTimer timer = new(DispatcherPriority.Background) { Interval = TimeSpan.FromMinutes(15) };
    private readonly SemaphoreSlim pollGate = new(1, 1);
    private readonly CancellationTokenSource lifetime = new();
    private Process? helper;
    private bool closing;
    private bool didStart;
    private JsonElement? latestResult;
    private CoreWebView2Environment? environment;

    public MainWindow(bool dryRun)
    {
        this.dryRun = dryRun;
        Title = "Token重置";
        Width = 760;
        Height = 650;
        MinWidth = 380;
        MinHeight = 500;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = new SolidColorBrush(Color.FromRgb(255, 253, 244));
        var layout = new DockPanel();
        var menu = new Menu();
        var monitor = new MenuItem { Header = "监控" };
        var check = new MenuItem { Header = "立即检查", InputGestureText = "Ctrl+R" };
        check.Click += async (_, _) => await CheckAsync();
        monitor.Items.Add(check);
        menu.Items.Add(monitor);
        DockPanel.SetDock(menu, Dock.Top);
        layout.Children.Add(menu);
        DockPanel.SetDock(status, Dock.Bottom);
        layout.Children.Add(status);
        layout.Children.Add(web);
        Content = layout;
        CommandBindings.Add(new CommandBinding(NavigationCommands.Refresh, async (_, _) => await CheckAsync()));
        InputBindings.Add(new KeyBinding(NavigationCommands.Refresh, new KeyGesture(Key.R, ModifierKeys.Control)));
        Loaded += async (_, _) => await InitializeAsync();
        StateChanged += async (_, _) => await UpdateVisibilityAsync();
        timer.Tick += async (_, _) => await CheckAsync();
    }

    private static bool IsLocal(string? url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var uri) &&
        uri.Scheme == Uri.UriSchemeHttps && uri.Host == "tibo.invalid" &&
        uri.IsDefaultPort && string.IsNullOrEmpty(uri.UserInfo);

    private static bool IsPage(string? url) =>
        IsLocal(url) && new Uri(url!).AbsolutePath is "/" or "/index.html";

    private static bool IsExternal(string? url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var uri) &&
        uri.Scheme == Uri.UriSchemeHttps && !string.IsNullOrWhiteSpace(uri.Host) &&
        string.IsNullOrEmpty(uri.UserInfo) && !uri.IsLoopback &&
        !uri.Host.Equals("tibo.invalid", StringComparison.OrdinalIgnoreCase);

    private async Task InitializeAsync()
    {
        try
        {
            Directory.CreateDirectory(home);
            if (!File.Exists(Path.Combine(site, "index.html")))
                throw new FileNotFoundException("Bundled site missing");
            environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(home, "webview2"));
            if (closing) return;
            await web.EnsureCoreWebView2Async(environment);
            if (closing) return;
            var core = web.CoreWebView2;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsWebMessageEnabled = true;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.NavigationStarting += (_, e) =>
            {
                if (IsPage(e.Uri)) return;
                e.Cancel = true;
                if (e.IsUserInitiated && !e.IsRedirected) OpenExternal(e.Uri);
            };
            core.FrameNavigationStarting += (_, e) => e.Cancel = true;
            core.NewWindowRequested += (_, e) =>
            {
                e.Handled = true;
                if (e.IsUserInitiated && IsPage(core.Source)) OpenExternal(e.Uri);
            };
            core.WebMessageReceived += async (_, e) =>
            {
                if (!IsPage(e.Source) || !IsPage(core.Source) || closing) return;
                try
                {
                    if (e.WebMessageAsJson.Length > 1024) return;
                    using var message = JsonDocument.Parse(e.WebMessageAsJson);
                    if (message.RootElement.ValueKind == JsonValueKind.Object &&
                        message.RootElement.TryGetProperty("type", out var type) &&
                        type.ValueKind == JsonValueKind.String && type.GetString() == "monitor.check")
                        await CheckAsync();
                }
                catch (JsonException) { }
            };
            // Exact resource responses keep both credentials and the SQLite database outside the WebView.
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += HandleResource;
            await core.AddScriptToExecuteOnDocumentCreatedAsync(
                "if(location.origin === 'https://tibo.invalid' && window === window.top) {" +
                "Object.defineProperty(window,'__TIBO_DESKTOP__'," +
                "{value:Object.freeze({platform:'windows'}),writable:false,configurable:false});}");
            core.NavigationCompleted += async (_, e) =>
            {
                if (closing || !e.IsSuccess || !IsPage(core.Source)) return;
                if (!didStart)
                {
                    didStart = true;
                    timer.Start();
                    await CheckAsync();
                }
                else if (latestResult.HasValue) await EmitAsync(latestResult.Value);
            };
            core.Navigate(Origin + "/index.html");
        }
        catch (WebView2RuntimeNotFoundException)
        {
            ShowStartupFailure("此电脑缺少 Microsoft WebView2 Runtime。安装官方 Evergreen Runtime 后重新打开应用。");
        }
        catch (Exception ex)
        {
            ShowStartupFailure("本机界面未能启动（" + ex.GetType().Name + "）。请检查安装包完整性。");
        }
    }

    private void ShowStartupFailure(string message)
    {
        if (closing) return;
        status.Text = message;
        var panel = new StackPanel { Margin = new Thickness(32), VerticalAlignment = VerticalAlignment.Center };
        panel.Children.Add(new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap, FontSize = 17 });
        var button = new Button { Content = "打开微软运行时下载页", Padding = new Thickness(16, 8, 16, 8), Margin = new Thickness(0, 16, 0, 0), HorizontalAlignment = HorizontalAlignment.Left };
        button.Click += (_, _) => OpenExternal("https://developer.microsoft.com/en-us/microsoft-edge/webview2/");
        panel.Children.Add(button);
        web.Visibility = Visibility.Collapsed;
        if (Content is DockPanel layout) layout.Children.Add(panel);
    }

    private void OpenExternal(string url)
    {
        if (!IsExternal(url)) return;
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        { status.Text = "无法打开外部链接，请检查系统默认浏览器。"; }
    }

    private void HandleResource(object? sender, CoreWebView2WebResourceRequestedEventArgs e)
    {
        try
        {
            if (environment is null) return;
            if (!IsLocal(e.Request.Uri) || e.Request.Method != "GET")
            {
                Reply(e, 403, "text/plain; charset=utf-8", Encoding.UTF8.GetBytes("Forbidden"));
                return;
            }
            string path = Uri.UnescapeDataString(new Uri(e.Request.Uri).AbsolutePath);
            if (path == "/config.json")
            {
                string subscription = "";
                string configPath = Path.Combine(home, "web.config.json");
                if (File.Exists(configPath))
                {
                    using var config = JsonDocument.Parse(ReadSmallFile(configPath, 16_384));
                    if (config.RootElement.ValueKind == JsonValueKind.Object &&
                        config.RootElement.TryGetProperty("subscriptionUrl", out var value) &&
                        value.ValueKind == JsonValueKind.String && IsExternal(value.GetString()))
                        subscription = value.GetString()!;
                }
                Reply(e, 200, "application/json; charset=utf-8", JsonSerializer.SerializeToUtf8Bytes(new { subscriptionUrl = subscription }));
                return;
            }
            if (path is "/data/snapshot.json" or "/data/health.json")
            {
                string file = Path.Combine(home, "data", Path.GetFileName(path));
                if (!File.Exists(file)) { Reply(e, 404, "application/json", "{}"u8.ToArray()); return; }
                Reply(e, 200, "application/json; charset=utf-8", ReadSmallFile(file, 8_000_000));
                return;
            }
            string relative = path == "/" ? "index.html" : path.TrimStart('/');
            // Serve only known frontend asset shapes; never map arbitrary paths under the user's home.
            if (relative.Contains('\\') || relative.Split('/').Any(part => part is "." or "..") ||
                !(relative is "index.html" or "favicon.svg" || relative.StartsWith("assets/", StringComparison.Ordinal)))
            { Reply(e, 404, "text/plain", []); return; }
            string mime = Path.GetExtension(relative).ToLowerInvariant() switch
            {
                ".html" => "text/html; charset=utf-8",
                ".js" => "text/javascript; charset=utf-8",
                ".css" => "text/css; charset=utf-8",
                ".svg" => "image/svg+xml",
                ".png" => "image/png",
                ".ico" => "image/x-icon",
                ".woff2" => "font/woff2",
                _ => ""
            };
            string target = Path.GetFullPath(Path.Combine(site, relative));
            if (mime == "" || !target.StartsWith(Path.GetFullPath(site) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) || !File.Exists(target))
            { Reply(e, 404, "text/plain", []); return; }
            Reply(e, 200, mime, ReadSmallFile(target, 16_000_000));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or ArgumentException)
        { Reply(e, 500, "application/json", "{}"u8.ToArray()); }
    }

    private static byte[] ReadSmallFile(string path, long limit)
    {
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        if (file.Length > limit) throw new IOException("Local resource exceeds size limit");
        using var bytes = new MemoryStream();
        file.CopyTo(bytes);
        if (bytes.Length > limit) throw new IOException("Local resource exceeds size limit");
        return bytes.ToArray();
    }

    private void Reply(CoreWebView2WebResourceRequestedEventArgs e, int code, string contentType, byte[] bytes)
    {
        const string csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
        e.Response = environment!.CreateWebResourceResponse(new MemoryStream(bytes, writable: false), code,
            code == 200 ? "OK" : code == 404 ? "Not Found" : code == 403 ? "Forbidden" : "Internal Error",
            "Content-Type: " + contentType + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: " + csp);
    }

    private async Task CheckAsync()
    {
        if (closing || web.CoreWebView2 is null) return;
        if (!await pollGate.WaitAsync(0))
        {
            await EmitAsync(JsonSerializer.SerializeToElement(new { status = "running" }));
            return;
        }
        try
        {
            await EmitAsync(JsonSerializer.SerializeToElement(new { status = "running" }));
            if (closing) return;
            string executable = Path.Combine(AppContext.BaseDirectory, "monitor", "TiboMonitorHelper.exe");
            if (!File.Exists(executable)) throw new FileNotFoundException("Monitor helper missing");
            var start = new ProcessStartInfo(executable)
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = home
            };
            foreach (string arg in new[] { "--once", "--config", Path.Combine(home, "monitor.config.json"),
                         "--state", Path.Combine(home, "state.sqlite3"), "--output", Path.Combine(home, "data") })
                start.ArgumentList.Add(arg);
            if (dryRun) start.ArgumentList.Add("--dry-run");
            // Desktop sending requires an explicit local-file switch, never an inherited process flag.
            start.Environment.Remove("TIBO_SEND_EMAIL");
            start.Environment["PYTHONUTF8"] = "1";
            start.Environment["PYTHONIOENCODING"] = "utf-8";
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            timeout.CancelAfter(TimeSpan.FromSeconds(90));
            helper = Process.Start(start) ?? throw new IOException("Monitor helper did not start");
            var stdout = ReadBoundedAsync(helper.StandardOutput, timeout.Token);
            var stderr = ReadBoundedAsync(helper.StandardError, timeout.Token);
            await helper.WaitForExitAsync(timeout.Token);
            string result = await stdout;
            await stderr; // Drain without displaying or logging potentially sensitive diagnostics.
            using var parsed = JsonDocument.Parse(result.Trim());
            JsonElement value = parsed.RootElement;
            if (value.ValueKind != JsonValueKind.Object ||
                !value.TryGetProperty("status", out var state) || state.ValueKind != JsonValueKind.String ||
                state.GetString() is not ("ok" or "cooldown" or "source-unavailable" or "failed") ||
                helper.ExitCode is not (0 or 1 or 2))
                throw new InvalidDataException("Unexpected monitor result");
            await EmitAsync(value.Clone());
        }
        catch (OperationCanceledException)
        {
            KillHelper();
            if (!closing) await EmitAsync(JsonSerializer.SerializeToElement(new { status = "failed", reason = "Timeout", action = "本次检查超过 90 秒，已停止。等待下一轮检查。" }));
        }
        catch (Exception ex)
        {
            KillHelper();
            if (!closing) await EmitAsync(JsonSerializer.SerializeToElement(new { status = "failed", reason = ex.GetType().Name, action = "请检查本机配置与安装包；未自动重试邮件。" }));
        }
        finally
        {
            helper?.Dispose();
            helper = null;
            pollGate.Release();
        }
    }

    private static async Task<string> ReadBoundedAsync(StreamReader reader, CancellationToken cancellation)
    {
        var output = new StringBuilder();
        var buffer = new char[2048];
        int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), cancellation)) > 0)
        {
            if (output.Length + count > 65_536) throw new InvalidDataException("Monitor output exceeds limit");
            output.Append(buffer, 0, count);
        }
        return output.ToString();
    }

    private async Task EmitAsync(JsonElement result)
    {
        latestResult = result.Clone();
        string? state = result.GetProperty("status").GetString();
        status.Text = state switch
        {
            "running" => "正在检查公开 RSS…",
            "cooldown" => "15 分钟间隔内不重复请求来源。",
            "source-unavailable" => "公开来源暂时不可用，已保留历史记录，本轮不发信。",
            "ok" => "检查完成 · 每 15 分钟检查一次" + (dryRun ? " · 仅测试，不发信" : ""),
            _ => "本次检查未完成，请检查本机配置。"
        };
        if (closing || web.CoreWebView2 is null || !IsPage(web.CoreWebView2.Source) || WindowState == WindowState.Minimized) return;
        try
        {
            await web.CoreWebView2.ExecuteScriptAsync(
                "window.__TIBO_DESKTOP_STATUS__ = " + JsonSerializer.Serialize(result) + "; window.dispatchEvent(new CustomEvent('tibo:monitor',{detail:window.__TIBO_DESKTOP_STATUS__}));");
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.Runtime.InteropServices.COMException) { }
    }

    private async Task UpdateVisibilityAsync()
    {
        if (closing || web.CoreWebView2 is null) return;
        try
        {
            if (WindowState == WindowState.Minimized)
            {
                web.Visibility = Visibility.Collapsed;
                await web.CoreWebView2.TrySuspendAsync();
            }
            else
            {
                web.CoreWebView2.Resume();
                web.Visibility = Visibility.Visible;
                if (latestResult.HasValue) await EmitAsync(latestResult.Value);
            }
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.Runtime.InteropServices.COMException or NotImplementedException)
        { if (!closing && WindowState != WindowState.Minimized) web.Visibility = Visibility.Visible; }
    }

    private void KillHelper()
    {
        try { if (helper is not null && !helper.HasExited) helper.Kill(entireProcessTree: true); }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception) { }
    }

    protected override void OnClosed(EventArgs e)
    {
        closing = true;
        timer.Stop();
        lifetime.Cancel();
        KillHelper();
        web.Dispose();
        base.OnClosed(e);
    }
}
