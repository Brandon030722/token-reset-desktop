import Foundation
@main struct EmailAccessChecks {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let file = root.appendingPathComponent("web.config.json")
        // Old local authorization and public hosted form cannot unlock email.
        try Data("{\"emailInviteHash\":\"old-hash\",\"subscriptionUrl\":\"https://example.org/form\"}".utf8).write(to: file)
        let access = EmailAccess(configURL: file)
        precondition(access.endpoint == nil)
        precondition(access.publicState["subscriptionStatus"] as? String == "none")
        for value in ["http://example.org", "https://user:pass@example.org", "https://example.org?q=x", "https://example.org/#x", "https://example.org/form"] {
            precondition(EmailAccess.serviceURL(value) == nil)
        }
        try Data("{\"mailServiceUrl\":\"https://mail.example.org\"}".utf8).write(to: file)
        let secret = String(repeating: "a", count: 64)
        let saved: [String:Any] = ["serviceUrl":"https://mail.example.org","token":secret,"subscriptionStatus":"active","email":"qa@example.com"]
        try JSONSerialization.data(withJSONObject:saved).write(to: access.sessionURL)
        precondition(access.publicState["subscriptionStatus"] as? String == "active")
        precondition(access.publicState["token"] == nil)
        let publicJSON = try JSONSerialization.data(withJSONObject:access.publicState)
        precondition(!String(data:publicJSON,encoding:.utf8)!.contains(secret))
        try Data("{\"mailServiceUrl\":\"https://other.example.org\"}".utf8).write(to: file)
        precondition(access.publicState["subscriptionStatus"] as? String == "none")
        print("Email checks passed: legacy gate disabled, endpoint isolation, credentials hidden")
    }
}
