import Foundation

// UI state is advisory. The service verifies every protected operation.
final class EmailAccess {
    let configURL: URL
    let defaults: UserDefaults
    init(configURL: URL, defaults: UserDefaults = .standard) { self.configURL = configURL; self.defaults = defaults }
    var sessionURL: URL { configURL.deletingLastPathComponent().appendingPathComponent("mail-session.json") }
    private func read(_ url: URL) -> [String: Any] {
        guard let data = try? Data(contentsOf: url), data.count <= 16_384,
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return value
    }
    private var session: [String: Any] { read(sessionURL) }
    static func serviceURL(_ value: String) -> URL? {
        guard let url = URL(string: value), value.count <= 2048, url.scheme == "https", let host = url.host,
              !host.isEmpty, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else { return nil }
        return url
    }
    var endpoint: URL? {
        let local = read(configURL)
        let bundled = Bundle.main.resourceURL.map { read($0.appendingPathComponent("site/config.json")) } ?? [:]
        return Self.serviceURL(local["mailServiceUrl"] as? String ?? bundled["mailServiceUrl"] as? String ?? "")
    }
    var publicState: [String: Any] {
        let saved = session
        let same = saved["serviceUrl"] as? String == endpoint?.absoluteString
        return ["emailDelivery":"cloud", "subscriptionConfigured":endpoint != nil,
                "subscriptionStatus":same ? saved["subscriptionStatus"] as? String ?? "none" : "none",
                "email":same ? saved["email"] as? String ?? "" : ""]
    }
    private func save(_ value: [String: Any]) throws {
        try FileManager.default.createDirectory(at: sessionURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        // Atomic replacement with restrictive permissions, before credentials are written.
        let temporary = sessionURL.deletingLastPathComponent().appendingPathComponent(".mail-" + UUID().uuidString)
        guard FileManager.default.createFile(atPath: temporary.path, contents: nil, attributes: [.posixPermissions: 0o600]) else { throw URLError(.cannotCreateFile) }
        defer { try? FileManager.default.removeItem(at: temporary) }
        try data.write(to: temporary)
        if FileManager.default.fileExists(atPath: sessionURL.path) { _ = try FileManager.default.replaceItemAt(sessionURL, withItemAt: temporary) }
        else { try FileManager.default.moveItem(at: temporary, to: sessionURL) }
    }
    func request(_ action: String, email: String = "", invite: String = "", completion: @escaping (String, Bool) -> Void) {
        guard ["subscribe","status","cancel"].contains(action), let base = endpoint else { completion("邮件服务尚未开放，看板可照常使用。", false); return }
        var req = URLRequest(url: base.appendingPathComponent("v1/" + action)); req.timeoutInterval = 30
        req.httpMethod = action == "status" ? "GET" : "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("TokenResetDesktop/0.1.2", forHTTPHeaderField: "User-Agent")
        let saved = session
        if action == "subscribe" {
            guard email.count <= 254, invite.count <= 128 else { completion("邮箱或邀请码过长。",false); return }
            req.httpBody = try? JSONSerialization.data(withJSONObject:["email":email,"invite":invite])
        } else {
            guard saved["serviceUrl"] as? String == base.absoluteString, let token = saved["token"] as? String,
                  token.range(of: "^[a-f0-9]{64}$", options:.regularExpression) != nil else { completion("请先使用邀请码订阅。",false); return }
            req.setValue("Bearer " + token, forHTTPHeaderField:"Authorization")
            if action == "cancel" { req.httpBody = Data("{}".utf8) }
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never; configuration.httpShouldSetCookies = false
        let network = URLSession(configuration:configuration, delegate:RejectMailRedirect(), delegateQueue:nil)
        network.dataTask(with:req) { data, response, error in
            defer { network.finishTasksAndInvalidate() }
            DispatchQueue.main.async {
                guard error == nil, let response = response as? HTTPURLResponse, let data = data, data.count <= 16_384,
                      let result = try? JSONSerialization.jsonObject(with:data) as? [String:Any] else {
                    completion("暂时无法连接邮件服务，已有云端预约不受本次连接失败影响。",false); return
                }
                guard (200...299).contains(response.statusCode) else {
                    if action == "status" && [401,403].contains(response.statusCode) {
                        var current = self.session
                        if current["token"] as? String == saved["token"] as? String {
                            current["subscriptionStatus"] = "inactive"; current["weeklyEnabled"] = false
                            try? self.save(current)
                        }
                    }
                    completion(result["message"] as? String ?? "验证失败，请稍后再试。",false); return
                }
                var updated = self.session
                if action != "subscribe" && updated["token"] as? String != saved["token"] as? String {
                    completion("订阅状态已更新，请重新检查。",false); return
                }
                if action == "subscribe" {
                    guard let token = result["token"] as? String, token.range(of:"^[a-f0-9]{64}$",options:.regularExpression) != nil else { completion("服务响应无效。",false); return }
                    updated = ["token":token,"serviceUrl":base.absoluteString,"weeklyEnabled":false]
                }
                for key in ["email","subscriptionStatus"] { if let value = result[key] as? String { updated[key] = value } }
                if action == "cancel" { updated["weeklyEnabled"] = false }
                do { try self.save(updated) } catch { completion("无法保存本机授权，请稍后重试。",false); return }
                completion(result["message"] as? String ?? ((updated["subscriptionStatus"] as? String) == "active" ? "邮箱已确认，邮件提醒已开启。" : "尚未确认，请先点击邮件中的按钮。"),true)
            }
        }.resume()
    }
}
private final class RejectMailRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
