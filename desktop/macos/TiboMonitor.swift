import AppKit
import WebKit
import UniformTypeIdentifiers
import Darwin
import UserNotifications

private let localScheme = "tibo"
private let localHost = "app"
private let interval: TimeInterval = 900

final class LocalResources: NSObject, WKURLSchemeHandler {
    let site: URL
    let state: URL
    init(site: URL, state: URL) {
        self.site = site.resolvingSymlinksInPath()
        self.state = state
    }
    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let url = task.request.url, url.scheme == localScheme, url.host == localHost,
              task.request.httpMethod == "GET" else {
            task.didFailWithError(URLError(.unsupportedURL)); return
        }
        let path = url.path == "/" ? "/index.html" : url.path
        var content: Data?
        var mime = "application/octet-stream"
        if ["/data/snapshot.json", "/data/health.json"].contains(path) {
            content = try? Data(contentsOf: state.appendingPathComponent(String(path.dropFirst())))
            mime = "application/json"
        } else if path == "/codex-usage.json" {
            content = try? Data(contentsOf: state.appendingPathComponent("codex-usage.json"))
            mime = "application/json"
        } else if path == "/notification-status.json" {
            content = try? Data(contentsOf: state.appendingPathComponent("notification-status.json"))
            mime = "application/json"
        } else if path == "/config.json" {
            let access = EmailAccess(configURL: state.appendingPathComponent("web.config.json"))
            content = try? JSONSerialization.data(withJSONObject: access.publicState)
            mime = "application/json"
        } else {
            let file = site.appendingPathComponent(String(path.dropFirst())).resolvingSymlinksInPath()
            guard file.path.hasPrefix(site.path + "/") else {
                task.didFailWithError(URLError(.noPermissionsToReadFile)); return
            }
            let types = ["html": "text/html", "js": "application/javascript", "css": "text/css",
                         "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"]
            guard let known = types[file.pathExtension] else {
                task.didFailWithError(URLError(.fileDoesNotExist)); return
            }
            mime = known
            content = try? Data(contentsOf: file)
        }
        let data = content ?? Data("Resource unavailable".utf8)
        let response = HTTPURLResponse(url: url, statusCode: content == nil ? 404 : 200,
                                       httpVersion: "HTTP/1.1", headerFields: [
            "Content-Type": mime + (mime.hasPrefix("text/") || mime == "application/json" ? "; charset=utf-8" : ""),
            "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; object-src 'none'; frame-src 'none'",
        ])!
        task.didReceive(response)
        task.didReceive(data)
        task.didFinish()
    }
    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, UNUserNotificationCenterDelegate, NSMenuDelegate {
    var window: NSWindow?
    var webView: WKWebView?
    var statusItem: NSStatusItem!
    let statusMenu = NSMenu()
    var timer: Timer?
    var worker: Process?
    var timeout: DispatchWorkItem?
    var localQueue: [([String], ([String: Any]) -> Void)] = []
    var lastStatus: [String: Any] = ["status": "running"]
    var notificationState = "系统通知：等待授权"
    var notificationReviewCount = 0
    var notificationDiagnostics: [String: Any] = [:]
    var paused: Bool {
        get { UserDefaults.standard.bool(forKey: "monitorPaused") }
        set { UserDefaults.standard.set(newValue, forKey: "monitorPaused") }
    }
    let state = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".tibo-reset")
    let notifications = UNUserNotificationCenter.current()
    var config: URL {
        if let saved = UserDefaults.standard.string(forKey: "monitorConfigPath") { return URL(fileURLWithPath: saved) }
        return state.appendingPathComponent("monitor.config.json")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        setupMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.autosaveName = "TiboObservatoryStatus"
        if let button = statusItem.button {
            button.image = Self.menuIcon()
            button.setAccessibilityLabel("Token重置")
            button.toolTip = "Token重置 · 左键打开，右键菜单"
            button.target = self
            button.action = #selector(statusClicked)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        statusMenu.delegate = self
        notifications.delegate = self
        authorizeNotifications()
        // Establish the local-notification baseline before the first poll.
        localCommand(["--local-notification-candidate"]) { [weak self] result in
            self?.recordNotificationReview(result)
            self?.checkNow()
        }
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(wokeUp), name: NSWorkspace.didWakeNotification, object: nil)
    }

    static func menuIcon() -> NSImage {
        let image = NSImage(size: NSSize(width: 19, height: 19), flipped: false) { _ in
            NSColor.black.set()
            let outline = NSBezierPath(roundedRect: NSRect(x: 1, y: 1, width: 17, height: 17), xRadius: 4, yRadius: 4)
            outline.fill()
            NSColor.white.set()
            NSBezierPath(rect: NSRect(x: 4, y: 12, width: 8, height: 2.3)).fill()
            NSBezierPath(rect: NSRect(x: 7, y: 5, width: 2.3, height: 8)).fill()
            NSBezierPath(roundedRect: NSRect(x: 13.1, y: 8, width: 1.8, height: 6.3), xRadius: 0.7, yRadius: 0.7).fill()
            NSBezierPath(ovalIn: NSRect(x: 13.1, y: 4.7, width: 1.8, height: 1.8)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }

    @objc func statusClicked() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            rebuildStatusMenu()
            if let button = statusItem.button { statusMenu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button) }
        } else { openDashboard() }
    }

    func menuWillOpen(_ menu: NSMenu) { rebuildStatusMenu() }

    func rebuildStatusMenu() {
        statusMenu.removeAllItems()
        statusMenu.autoenablesItems = false
        func item(_ title: String, _ action: Selector? = nil, _ key: String = "") -> NSMenuItem {
            let result = statusMenu.addItem(withTitle: title, action: action, keyEquivalent: key)
            result.target = self
            result.isEnabled = action != nil
            return result
        }
        _ = item("Token重置 · 菜单栏监控")
        _ = item(statusDescription())
        statusMenu.addItem(.separator())
        _ = item("打开观察面板", #selector(openDashboard))
        let check = item("立即检查", #selector(checkNow), "r")
        check.isEnabled = worker == nil && !paused
        _ = item(paused ? "恢复监控" : "暂停监控", #selector(togglePaused))
        statusMenu.addItem(.separator())
        let advanced = NSMenu(title: "高级选项")
        advanced.autoenablesItems = false
        let stateItem = advanced.addItem(withTitle: notificationState, action: nil, keyEquivalent: "")
        stateItem.isEnabled = false
        for (title, action) in [("检查通知权限…", #selector(authorizeNotifications)), ("发送测试通知", #selector(testNotification)), ("选择监控配置…", #selector(selectConfig))] {
            let entry = advanced.addItem(withTitle: title, action: action, keyEquivalent: "")
            entry.target = self; entry.isEnabled = true
        }
        if notificationReviewCount > 0 {
            let review = advanced.addItem(withTitle: "有 \(notificationReviewCount) 条通知状态待核查", action: nil, keyEquivalent: "")
            review.isEnabled = false
        }
        let advancedItem = item("高级选项")
        advancedItem.submenu = advanced; advancedItem.isEnabled = true
        statusMenu.addItem(.separator())
        _ = item("退出 Token重置", #selector(quit), "q")
    }

    func statusDescription() -> String {
        if paused { return "监控已暂停" }
        switch lastStatus["status"] as? String {
        case "running": return "正在检查公开动态…"
        case "ok": return "已检查 \(lastStatus["posts"] as? Int ?? 0) 条动态"
        case "cooldown":
            if let text = lastStatus["nextCheckAt"] as? String, let date = Self.parseDate(text) {
                let f = DateFormatter(); f.dateFormat = "HH:mm"
                return "监控中 · 下次可检查 \(f.string(from: date))"
            }
            return "监控中 · 每 15 分钟检查一次"
        case "source-unavailable": return "来源暂时不可用 · 保留历史"
        default: return "检查未完成 · 请核对配置"
        }
    }

    static func parseDate(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    @objc func togglePaused() {
        paused.toggle()
        if paused {
            timer?.invalidate()
            if let process = worker, process.isRunning { process.terminate() }
            emit(["status": "paused"])
        }
        statusItem.button?.toolTip = "Token重置 · " + statusDescription()
        if !paused { checkNow() }
    }
    @objc func wokeUp() { checkNow() }
    @objc func quit() { NSApp.terminate(nil) }

    func setupMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 Token重置", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu; menu.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit; menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    @objc func openDashboard() {
        if let window = window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        guard let resources = Bundle.main.resourceURL else { return }
        let controller = WKUserContentController()
        controller.add(self, name: "tibo")
        controller.addUserScript(WKUserScript(source: "Object.defineProperty(window, '__TIBO_DESKTOP__', {value: Object.freeze({platform:'macos'}), writable:false});", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let preferences = Dictionary(uniqueKeysWithValues: ["reset:personalTime", "reset:plan"].compactMap { key in
            UserDefaults.standard.string(forKey: key).map { (key, $0) }
        })
        if let raw = try? JSONSerialization.data(withJSONObject: preferences), let json = String(data: raw, encoding: .utf8) {
            controller.addUserScript(WKUserScript(source: "try { const values = \(json); for (const key of Object.keys(values)) localStorage.setItem(key, values[key]); } catch {}", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        let settings = WKWebViewConfiguration()
        settings.userContentController = controller
        settings.setURLSchemeHandler(LocalResources(site: resources.appendingPathComponent("site"), state: state), forURLScheme: localScheme)
        settings.preferences.javaScriptCanOpenWindowsAutomatically = false
        // A private store dies with the panel instead of retaining a browser
        // session in the background. The two local preferences live in defaults.
        settings.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: settings)
        view.navigationDelegate = self; view.uiDelegate = self
        view.underPageBackgroundColor = .clear
        webView = view
        let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 460, height: 520), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        panel.title = "Token重置"
        panel.minSize = NSSize(width: 360, height: 440)
        panel.backgroundColor = .white
        panel.isReleasedWhenClosed = false
        panel.contentView = view; panel.delegate = self
        panel.setFrameAutosaveName("TokenResetTabbedWindow"); panel.center()
        window = panel
        panel.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        view.load(URLRequest(url: URL(string: "tibo://app/index.html")!))
    }

    func windowWillClose(_ notification: Notification) { releaseDashboard() }
    func releaseDashboard() {
        webView?.stopLoading()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "tibo")
        webView?.navigationDelegate = nil; webView?.uiDelegate = nil
        window?.contentView = nil
        webView = nil; window = nil
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { emit(lastStatus) }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, message.frameInfo.securityOrigin.protocol == localScheme,
              message.frameInfo.securityOrigin.host == localHost, let body = message.body as? [String: Any] else { return }
        if body["type"] as? String == "monitor.check" { checkNow() }
        if body["type"] as? String == "weekly.email", let enabled = body["enabled"] as? Bool {
            emit(["status": "running"])
            localCommand(["--weekly-email", enabled ? "on" : "off", "--config", config.path]) { [weak self] result in
                var detail = result
                if result["status"] as? String == "unavailable" { detail["status"] = "failed" }
                self?.emit(detail)
            }
        }
        if body["type"] as? String == "notification.authorize" { authorizeNotifications() }
        if body["type"] as? String == "notification.test" { testNotification() }
        if body["type"] as? String == "email.invite.verify", let code = body["code"] as? String {
            let passed = emailAccess.verify(code)
            emitEmail(passed ? "邀请码已验证。完成邮箱确认后才算订阅成功。" : "邀请码不正确，请检查后重试。", success: passed)
        }
        if body["type"] as? String == "email.configure", let url = body["subscriptionUrl"] as? String {
            do {
                try emailAccess.saveForm(url)
                emitEmail(url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "已移除订阅表单地址。" : "订阅入口已保存；后台自动发信仍需单独配置。", success: true)
            } catch { emitEmail("保存失败，请填写有效的 HTTPS 表单地址。", success: false) }
        }
        if body["type"] as? String == "email.subscribe" {
            if let url = emailAccess.subscriptionURL {
                let opened = NSWorkspace.shared.open(url)
                emitEmail(opened ? "已打开订阅表单，请按邮件提示确认邮箱。" : "未能打开订阅表单，请稍后重试。", success: opened)
            } else {
                emitEmail(emailAccess.unlocked ? "邀请码已通过，邮件服务尚未配置。" : "请先验证邀请码。", success: false)
            }
        }
        if body["type"] as? String == "preferences.save", let key = body["key"] as? String,
           ["reset:personalTime", "reset:plan"].contains(key), let value = body["value"] as? String, value.count <= 128 {
            UserDefaults.standard.set(value, forKey: key)
        }
    }
    var emailAccess: EmailAccess { EmailAccess(configURL: state.appendingPathComponent("web.config.json")) }
    func emitEmail(_ message: String, success: Bool) {
        var detail = emailAccess.publicState
        detail["message"] = message; detail["success"] = success
        guard let raw = try? JSONSerialization.data(withJSONObject: detail), let json = String(data: raw, encoding: .utf8) else { return }
        webView?.evaluateJavaScript("window.dispatchEvent(new CustomEvent('tibo:email', {detail: \(json)}));", completionHandler: nil)
    }
    func emit(_ detail: [String: Any]) {
        lastStatus = detail
        statusItem.button?.toolTip = "Token重置 · " + statusDescription() + " · 左键打开，右键菜单"
        guard let view = webView, let raw = try? JSONSerialization.data(withJSONObject: detail), let json = String(data: raw, encoding: .utf8) else { return }
        view.evaluateJavaScript("window.__TIBO_DESKTOP_STATUS__ = \(json); window.dispatchEvent(new CustomEvent('tibo:monitor', {detail:window.__TIBO_DESKTOP_STATUS__}));", completionHandler: nil)
    }

    @objc func checkNow() {
        guard !paused else { emit(["status": "paused"]); return }
        guard worker == nil else { emit(lastStatus); return }
        emit(["status": "running"])
        runHelper(["--once", "--config", config.path, "--output", state.appendingPathComponent("data").path], limit: 180) { [weak self] result in
            guard let self = self else { return }
            guard !self.paused else { self.emit(["status": "paused"]); return }
            // Personal quota reads run independently even if the mirror failed.
            self.localCommand(["--read-codex-usage", "--config", self.config.path]) { [weak self] usage in
                guard let self = self else { return }
                var detail = result
                detail["weeklyStatus"] = usage["weeklyStatus"] ?? usage["status"]
                self.emit(self.paused ? ["status": "paused"] : detail)
                self.scheduleNextCheck(result)
                if !self.paused { self.inspectNotification() }
            }
        }
    }
    func scheduleNextCheck(_ result: [String: Any]) {
        timer?.invalidate()
        guard !paused else { return }
        let next = (result["nextCheckAt"] as? String).flatMap(Self.parseDate)
        let delay = next.map { max(3, $0.timeIntervalSinceNow + 2) } ?? interval
        timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkNow() }
        }
        timer?.tolerance = min(30, delay * 0.05)
    }

    func runHelper(_ arguments: [String], limit: TimeInterval = 10, completion: @escaping ([String: Any]) -> Void) {
        guard worker == nil, let resources = Bundle.main.resourceURL else { completion(["status": "failed", "reason": "busy"]); return }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("monitor/TiboMonitorHelper")
        process.arguments = arguments + ["--state", state.appendingPathComponent("state.sqlite3").path]
        process.currentDirectoryURL = state; process.qualityOfService = .utility
        let output = Pipe(); process.standardOutput = output; process.standardError = FileHandle.nullDevice
        worker = process
        process.terminationHandler = { [weak self] _ in
            let raw = output.fileHandleForReading.readDataToEndOfFile()
            let last = String(data: raw.prefix(65536), encoding: .utf8)?.split(separator: "\n").last.map(String.init) ?? "{}"
            let parsed = (try? JSONSerialization.jsonObject(with: Data(last.utf8))) as? [String: Any]
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.timeout?.cancel(); self.timeout = nil; self.worker = nil
                completion(parsed ?? ["status": "failed", "reason": "worker-exit"])
                self.drainLocalQueue()
            }
        }
        do {
            try process.run()
            let deadline = DispatchWorkItem { [weak process] in
                guard let process = process, process.isRunning else { return }
                process.terminate()
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { if process.isRunning { kill(process.processIdentifier, SIGKILL) } }
            }
            timeout = deadline; DispatchQueue.main.asyncAfter(deadline: .now() + limit, execute: deadline)
        } catch { worker = nil; completion(["status": "failed", "reason": "worker-unavailable"]); drainLocalQueue() }
    }
    func localCommand(_ arguments: [String], completion: @escaping ([String: Any]) -> Void) {
        localQueue.append((arguments, completion)); drainLocalQueue()
    }
    func drainLocalQueue() {
        guard worker == nil, !localQueue.isEmpty else { return }
        let (arguments, completion) = localQueue.removeFirst()
        runHelper(arguments, limit: arguments.contains("--read-codex-usage") || arguments.contains("--weekly-email") ? 50 : 10, completion: completion)
    }
    func recordNotificationReview(_ result: [String: Any]) {
        if let count = result["needsReview"] as? Int { notificationReviewCount = count }
        if result["status"] as? String == "failed" { notificationState = "通知记录未确认 · 请检查本地状态" }
    }

    func inspectNotification(weekly: Bool = false) {
        let suffix = weekly ? ["--weekly"] : []
        localCommand(["--local-notification-candidate"] + suffix) { [weak self] result in
            guard let self = self else { return }
            self.recordNotificationReview(result)
            guard result["status"] as? String == "candidate", let candidate = result["candidate"] as? [String: Any], let eventID = candidate["eventId"] as? String else {
                if !weekly { self.inspectNotification(weekly: true) }
                return
            }
            self.notifications.getNotificationSettings { [weak self] settings in
                guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return }
                DispatchQueue.main.async {
                    guard let self = self, !self.paused else { return }
                    self.localCommand(["--claim-local-notification", eventID] + suffix) { [weak self] result in
                        guard let self = self, result["status"] as? String == "claimed",
                              let token = result["claimToken"] as? String,
                              let claimedCandidate = result["candidate"] as? [String: Any] else { return }
                        self.recordNotificationReview(result)
                        let content = UNMutableNotificationContent()
                        content.title = claimedCandidate["title"] as? String ?? "发现新的重置线索"
                        content.body = claimedCandidate["body"] as? String ?? "未来 48 小时的规则评分已达到 80 分，点击查看依据。"
                        content.sound = .default
                        content.userInfo = ["eventId": eventID]
                        self.notifications.add(UNNotificationRequest(identifier: "tibo-reset:" + eventID, content: content, trigger: nil)) { [weak self] error in
                            DispatchQueue.main.async {
                                let flag = error == nil ? "--ack-local-notification" : "--release-local-notification"
                                self?.localCommand([flag, eventID, "--claim-token", token] + suffix) { result in
                                    self?.recordNotificationReview(result)
                                    if result["status"] as? String == "acknowledged" { self?.inspectNotification(weekly: weekly) }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @objc func authorizeNotifications() {
        notifications.requestAuthorization(options: [.alert, .sound]) { [weak self] allowed, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.notificationState = allowed ? "系统通知：已开启 · 重置公告与每周恢复" : "系统通知未开启 · 请在系统设置中允许"
                self.logNotification(["permissionGranted": allowed, "permissionError": error?.localizedDescription ?? ""])
                if allowed && CommandLine.arguments.contains("--test-notification") { self.testNotification() }
            }
        }
    }
    func logNotification(_ values: [String: Any]) {
        notificationDiagnostics.merge(values) { _, new in new }
        notificationDiagnostics["updatedAt"] = ISO8601DateFormatter().string(from: Date())
        if let data = try? JSONSerialization.data(withJSONObject: notificationDiagnostics, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: state.appendingPathComponent("notification-status.json"), options: .atomic)
        }
        webView?.evaluateJavaScript("window.dispatchEvent(new Event('tibo:notification'));", completionHandler: nil)
    }
    @objc func testNotification() {
        let testID = "tibo-reset:test:" + UUID().uuidString
        for key in ["testAccepted", "testError", "testInNotificationCenter", "testRequested"] { notificationDiagnostics.removeValue(forKey: key) }
        logNotification(["testID": testID])
        notifications.getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.logNotification(["testRequested": true, "authorizationStatus": settings.authorizationStatus.rawValue,
                                      "alertSetting": settings.alertSetting.rawValue, "alertStyle": settings.alertStyle.rawValue])
                guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                    self.notificationState = "测试通知未发送：系统尚未授权"
                    self.logNotification(["permissionGranted": false, "testAccepted": false])
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = "Token重置 · 测试通知"
                content.body = "通知通道测试，不代表发现重置。关闭观察面板后，菜单栏会继续监控。"
                self.notifications.add(UNNotificationRequest(identifier: testID, content: content, trigger: nil)) { [weak self] error in
                    DispatchQueue.main.async {
                        self?.notificationState = error == nil ? "测试已提交 · 如未弹出请检查横幅/专注模式" : "测试通知未提交：\(error!.localizedDescription)"
                        self?.logNotification(["testAccepted": error == nil, "testError": error?.localizedDescription ?? ""])
                        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                            self?.notifications.getDeliveredNotifications { items in
                                DispatchQueue.main.async {
                                    self?.logNotification(["testInNotificationCenter": items.contains { $0.request.identifier == testID }])
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) { completionHandler([.banner, .list, .sound]) }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async { [weak self] in self?.openDashboard(); completionHandler() }
    }

    @objc func selectConfig() {
        let panel = NSOpenPanel(); panel.title = "选择本地监控配置"
        panel.allowedContentTypes = [.json]; panel.canChooseDirectories = false
        NSApp.activate(ignoringOtherApps: true)
        panel.begin { response in
            if response == .OK, let file = panel.url { UserDefaults.standard.set(file.path, forKey: "monitorConfigPath") }
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.scheme == localScheme && url.host == localHost { decisionHandler(.allow); return }
        if action.navigationType == .linkActivated, url.scheme == "https", url.user == nil, url.password == nil { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "https", url.user == nil, url.password == nil { NSWorkspace.shared.open(url) }
        return nil
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate(); timeout?.cancel()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        if let worker = worker, worker.isRunning {
            worker.terminate()
            let deadline = Date().addingTimeInterval(0.2)
            while worker.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
            if worker.isRunning { kill(worker.processIdentifier, SIGKILL) }
        }
        releaseDashboard()
        if let item = statusItem { NSStatusBar.system.removeStatusItem(item) }
    }
}

@main
struct TiboApplication {
    @MainActor static func main() {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let delegate = AppDelegate(); application.delegate = delegate
        application.run()
    }
}
