using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Security.AccessControl;
using System.Security.Principal;
using Forms = System.Windows.Forms;
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
    private readonly string home;
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
    private bool paused;
    private bool emailBusy;
    private readonly Forms.NotifyIcon tray;
    private readonly bool smokeTest;
    private readonly EmailAccess emailAccess;
    private JsonElement? latestResult;
    private CoreWebView2Environment? environment;

    public MainWindow(bool dryRun, string? smokeDirectory = null)
    {
        this.dryRun = dryRun;
        smokeTest = smokeDirectory != null;
        home = smokeDirectory ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".tibo-reset");
        Directory.CreateDirectory(home);
        // Python replacements inherit these permissions as well; no shared-user credentials.
        var acl = new DirectorySecurity();
        acl.SetAccessRuleProtection(true, false);
        foreach (var identity in new[] { WindowsIdentity.GetCurrent().User!, new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null) })
            acl.AddAccessRule(new FileSystemAccessRule(identity, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        new DirectoryInfo(home).SetAccessControl(acl);
        emailAccess = new EmailAccess(home, site);
        tray = new Forms.NotifyIcon { Text = "Token重置", Icon = System.Drawing.Icon.ExtractAssociatedIcon(Environment.ProcessPath!), Visible = true };
        var trayMenu = new Forms.ContextMenuStrip();
        trayMenu.Items.Add("打开面板", null, (_, _) => ShowPanel());
        trayMenu.Items.Add("立即检查", null, async (_, _) => await CheckAsync());
        var pause = trayMenu.Items.Add("暂停监控");
        pause.Click += async (_, _) => { paused = !paused; pause.Text = paused ? "恢复监控" : "暂停监控"; if (paused) await EmitAsync(JsonSerializer.SerializeToElement(new { status = "paused" })); else await CheckAsync(); };
        var advanced = new Forms.ToolStripMenuItem("高级选项");
        advanced.DropDownItems.Add("发送测试通知", null, (_, _) => TestNotification());
        advanced.DropDownItems.Add("选择 Codex 程序…", null, async (_, _) => await SelectCodexAsync());
        trayMenu.Items.Add(advanced);
        trayMenu.Items.Add("退出", null, (_, _) => { closing = true; Close(); });
        tray.ContextMenuStrip = trayMenu;
        tray.MouseClick += (_, e) => { if (e.Button == Forms.MouseButtons.Left) ShowPanel(); };
        tray.BalloonTipClicked += (_, _) => ShowPanel();
        Title = "Token重置";
        Width = 620;
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
        Loaded += async (_, _) => { if (!didStart && web.CoreWebView2 is null) await InitializeAsync(); };
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
                    if (e.WebMessageAsJson.Length > 2048) return;
                    using var message = JsonDocument.Parse(e.WebMessageAsJson);
                    var body = message.RootElement;
                    if (body.ValueKind != JsonValueKind.Object || !body.TryGetProperty("type", out var type) || type.ValueKind != JsonValueKind.String) return;
                    switch (type.GetString())
                    {
                        case "monitor.check": await CheckAsync(); break;
                        case "weekly.email":
                            if (body.TryGetProperty("enabled", out var enabled) && enabled.ValueKind is JsonValueKind.True or JsonValueKind.False)
                                await WeeklyEmailAsync(enabled.GetBoolean());
                            break;
                        case "email.subscribe": case "email.status": case "email.cancel":
                            string Get(string key) => body.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString()! : "";
                            await EmailAsync(type.GetString()![6..], Get("email"), Get("code")); break;
                        case "notification.test": TestNotification(); break;
                        case "notification.authorize":
                            await web.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new Event('tibo:notification'))"); break;
                    }
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
                    if (smokeTest) { await SmokeTestAsync(); return; }
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
                Reply(e, 200, "application/json; charset=utf-8", Encoding.UTF8.GetBytes(emailAccess.PublicState().ToJsonString()));
                return;
            }
            if (path == "/notification-status.json")
            {
                Reply(e, 200, "application/json; charset=utf-8", JsonSerializer.SerializeToUtf8Bytes(new { platform = "windows", permissionGranted = (bool?)null }));
                return;
            }
            if (path == "/codex-usage.json")
            {
                string file = Path.Combine(home, "codex-usage.json");
                Reply(e, File.Exists(file) ? 200 : 404, "application/json; charset=utf-8", File.Exists(file) ? ReadSmallFile(file, 65536) : "{}"u8.ToArray());
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
        if (closing || smokeTest) return;
        if (paused) { await EmitAsync(JsonSerializer.SerializeToElement(new { status = "paused" })); return; }
        if (!await pollGate.WaitAsync(0)) return;
        try
        {
            await EmitAsync(JsonSerializer.SerializeToElement(new { status = "running" }));
            // Suppress pre-install history before the first collection.
            await RunHelperAsync(["--local-notification-candidate"], 10);
            await RunHelperAsync(["--local-notification-candidate", "--weekly"], 10);
            var result = await RunHelperAsync(["--once", "--output", Path.Combine(home, "data")], 90);
            await RunHelperAsync(["--read-codex-usage"], 50);
            await EmitAsync(paused ? JsonSerializer.SerializeToElement(new { status = "paused" }) : result);
            if (!paused && !dryRun) { await InspectNotificationAsync(false); await InspectNotificationAsync(true); }
        }
        catch (Exception ex)
        {
            if (!closing) await EmitAsync(JsonSerializer.SerializeToElement(new { status = "failed", reason = ex.GetType().Name }));
        }
        finally { pollGate.Release(); }
    }

    private async Task<JsonElement> RunHelperAsync(string[] arguments, int seconds)
    {
        string executable = Path.Combine(AppContext.BaseDirectory, "monitor", "TiboMonitorHelper.exe");
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            WorkingDirectory = home
        };
        foreach (string arg in arguments.Concat(new[] { "--config", Path.Combine(home, "monitor.config.json"), "--state", Path.Combine(home, "state.sqlite3") })) start.ArgumentList.Add(arg);
        if (dryRun) start.ArgumentList.Add("--dry-run");
        start.Environment.Remove("TIBO_SEND_EMAIL");
        start.Environment["PYTHONUTF8"] = "1"; start.Environment["PYTHONIOENCODING"] = "utf-8";
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        timeout.CancelAfter(TimeSpan.FromSeconds(seconds));
        try
        {
            helper = Process.Start(start) ?? throw new IOException("Monitor helper did not start");
            var stdout = ReadBoundedAsync(helper.StandardOutput, timeout.Token);
            var stderr = ReadBoundedAsync(helper.StandardError, timeout.Token);
            await helper.WaitForExitAsync(timeout.Token);
            string output = await stdout; await stderr;
            using var parsed = JsonDocument.Parse(output.Trim());
            if (parsed.RootElement.ValueKind != JsonValueKind.Object || !parsed.RootElement.TryGetProperty("status", out _) || helper.ExitCode is not (0 or 1 or 2)) throw new IOException("Invalid helper result");
            return parsed.RootElement.Clone();
        }
        finally { KillHelper(); helper?.Dispose(); helper = null; }
    }

    private async Task WeeklyEmailAsync(bool enabled)
    {
        if (emailBusy) return;
        await pollGate.WaitAsync();
        try
        {
            await EmitAsync(JsonSerializer.SerializeToElement(new { status = "running" }));
            var result = await RunHelperAsync(["--weekly-email", enabled ? "on" : "off"], 50);
            await EmitAsync(JsonSerializer.SerializeToElement(new { status = result.GetProperty("status").GetString() == "failed" ? "failed" : "cooldown" }));
        }
        catch (Exception) { if (!closing) await EmitAsync(JsonSerializer.SerializeToElement(new { status = "failed" })); }
        finally { pollGate.Release(); }
    }

    private async Task EmailAsync(string action, string email, string code)
    {
        if (emailBusy) return;
        emailBusy = true;
        string message;
        // Serialize session writes with the helper's weekly preference writes.
        await pollGate.WaitAsync();
        try { message = await emailAccess.RequestAsync(action, email, code, lifetime.Token); }
        catch (Exception) { message = "暂时无法连接邮件服务，已有云端预约不受影响。"; }
        finally { pollGate.Release(); emailBusy = false; }
        if (closing || web.CoreWebView2 is null) return;
        var state = emailAccess.PublicState(); state["message"] = message;
        try { await web.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('tibo:email',{detail:" + state.ToJsonString() + "}))"); }
        catch (Exception) { }
        if (!smokeTest) await CheckAsync();
    }

    private async Task InspectNotificationAsync(bool weekly)
    {
        string[] suffix = weekly ? ["--weekly"] : [];
        var candidate = await RunHelperAsync(["--local-notification-candidate", .. suffix], 10);
        if (candidate.GetProperty("status").GetString() != "candidate" || paused || closing) return;
        string eventId = candidate.GetProperty("candidate").GetProperty("eventId").GetString()!;
        var claim = await RunHelperAsync(["--claim-local-notification", eventId, .. suffix], 10);
        if (claim.GetProperty("status").GetString() != "claimed") return;
        var content = claim.GetProperty("candidate");
        // ShowBalloonTip acceptance is not proof of a visible banner (Focus Assist may hide it).
        // Claim before submission; uncertain outcomes stay claimed and are never resent automatically.
        tray.ShowBalloonTip(8000, content.GetProperty("title").GetString(), content.GetProperty("body").GetString(), Forms.ToolTipIcon.Info);
        await RunHelperAsync(["--ack-local-notification", eventId, "--claim-token", claim.GetProperty("claimToken").GetString()!, .. suffix], 10);
    }

    private async Task SelectCodexAsync()
    {
        var dialog = new Microsoft.Win32.OpenFileDialog { Title = "选择本机 Codex 程序", Filter = "Codex 程序 (codex.exe)|codex.exe", CheckFileExists = true };
        if (dialog.ShowDialog() != true || !Path.GetFileName(dialog.FileName).Equals("codex.exe", StringComparison.OrdinalIgnoreCase)) return;
        await pollGate.WaitAsync();
        try
        {
            string path = Path.Combine(home, "monitor.config.json");
            var config = File.Exists(path) ? System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(path)) as System.Text.Json.Nodes.JsonObject : new System.Text.Json.Nodes.JsonObject();
            if (config is null) throw new IOException("Invalid local configuration");
            config["codexExecutable"] = dialog.FileName;
            string temporary = path + ".new";
            File.WriteAllText(temporary, config.ToJsonString(), new UTF8Encoding(false));
            File.Move(temporary, path, true);
        }
        catch (Exception) { status.Text = "未能保存 Codex 位置，请检查本机配置。"; }
        finally { pollGate.Release(); }
        await CheckAsync();
    }

    private void TestNotification()
    {
        tray.ShowBalloonTip(8000, "Token重置 · 测试通知", "通知已提交给 Windows。若未显示，请检查通知设置或勿扰模式。", Forms.ToolTipIcon.Info);
    }

    private void ShowPanel()
    {
        Show(); WindowState = WindowState.Normal; Activate();
        _ = UpdateVisibilityAsync();
    }

    private async Task SmokeTestAsync()
    {
        try
        {
            // Isolated build validation. No collection, subscription or email requests.
            await Task.Delay(1500);
            string page = await web.CoreWebView2.ExecuteScriptAsync("JSON.stringify({title:document.title,tabs:document.querySelectorAll('[role=tab]').length,bridge:window.__TIBO_DESKTOP__?.platform})");
            using var result = JsonDocument.Parse(JsonSerializer.Deserialize<string>(page)!);
            if (result.RootElement.GetProperty("tabs").GetInt32() != 4 || result.RootElement.GetProperty("bridge").GetString() != "windows") throw new IOException("Dashboard did not load");
            if (EmailAccess.ValidateEndpoint("http://example.com") != null || EmailAccess.ValidateEndpoint("https://example.com/path") != null) throw new IOException("Unsafe endpoint accepted");
            var state = emailAccess.PublicState();
            if (state["subscriptionConfigured"]?.GetValue<bool>() != true || state.ContainsKey("token")) throw new IOException("Invalid mail configuration");
            // Exercise the actual JS-to-native bridge; no saved token means no HTTP request.
            await web.CoreWebView2.ExecuteScriptAsync("window.addEventListener('tibo:email',e=>window.__qaMail=e.detail,{once:true});window.chrome.webview.postMessage({type:'email.status'})");
            await Task.Delay(500);
            string mail = await web.CoreWebView2.ExecuteScriptAsync("window.__qaMail?.message");
            if (JsonSerializer.Deserialize<string>(mail) != "请先使用邀请码订阅。") throw new IOException("Mail bridge unavailable");
            await web.CoreWebView2.ExecuteScriptAsync("Promise.all(['/mail-session.json','/monitor.config.json','/state.sqlite3'].map(p=>fetch(p).then(r=>r.status))).then(r=>window.__qaPrivate=r)");
            await Task.Delay(500);
            string boundary = await web.CoreWebView2.ExecuteScriptAsync("JSON.stringify(window.__qaPrivate)");
            if (JsonSerializer.Deserialize<string>(boundary) != "[404,404,404]") throw new IOException("Private resources exposed");
            using (var screenshot = File.Create(Path.Combine(home, "windows-panel.png")))
                await web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, screenshot);
            Close();
            if (IsVisible || closing || !tray.Visible) throw new IOException("Close did not retain tray");
            ShowPanel();
            if (!IsVisible) throw new IOException("Tray did not reopen window");
            File.WriteAllText(Path.Combine(home, "smoke-result.json"), JsonSerializer.Serialize(new { status = "passed", checks = new[] { "webview-dashboard", "four-tabs", "private-resource-boundary", "email-native-bridge", "close-to-tray", "reopen" } }));
            closing = true; Close();
        }
        catch (Exception ex)
        {
            File.WriteAllText(Path.Combine(home, "smoke-result.json"), JsonSerializer.Serialize(new { status = "failed", reason = ex.Message }));
            closing = true; Close();
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
            "running" => "正在检查公开动态与每周额度…",
            "paused" => "监控已暂停 · 右键 T! 可恢复",
            "cooldown" => "15 分钟间隔内不重复请求来源。",
            "source-unavailable" => "公开来源暂时不可用，已保留历史记录，本轮不发信。",
            "ok" => "检查完成 · 每 15 分钟检查一次" + (dryRun ? " · 仅测试，不发信" : ""),
            _ => "本次检查未完成，请检查本机配置。"
        };
        if (closing || !IsVisible || web.CoreWebView2 is null || !IsPage(web.CoreWebView2.Source) || WindowState == WindowState.Minimized) return;
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
            if (!IsVisible || WindowState == WindowState.Minimized)
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

    protected override void OnClosing(CancelEventArgs e)
    {
        if (!closing) { e.Cancel = true; Hide(); _ = UpdateVisibilityAsync(); }
        base.OnClosing(e);
    }

    protected override void OnClosed(EventArgs e)
    {
        closing = true;
        timer.Stop();
        lifetime.Cancel();
        KillHelper();
        tray.Visible = false; tray.ContextMenuStrip?.Dispose(); tray.Icon?.Dispose(); tray.Dispose();
        web.Dispose();
        base.OnClosed(e);
    }
}
