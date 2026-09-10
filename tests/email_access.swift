import Foundation
@main struct EmailAccessChecks {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let suite = "token-reset-test-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite); try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("web.config.json")
        let access = EmailAccess(configURL: file, defaults: defaults)
        try access.saveForm("https://example.org/subscribe")
        precondition(!access.unlocked && access.subscriptionURL == nil)
        precondition(!access.verify("wrong") && !access.verify("tokenemail"))
        precondition(access.verify(" TOKENEMAIL\n"))
        precondition(access.subscriptionURL?.host == "example.org")
        let reopened = EmailAccess(configURL: file, defaults: defaults)
        precondition(reopened.unlocked)
        for url in ["http://example.org", "javascript:alert(1)", "https://user:password@example.org"] {
            do { try access.saveForm(url); preconditionFailure("Invalid URL accepted") } catch {}
        }
        try Data("{\"emailInviteHash\":\"invalid\",\"subscriptionUrl\":\"https://example.org\"}".utf8).write(to: file)
        precondition(!access.unlocked && !access.verify("TOKENEMAIL"))
        print("Email access checks passed: validation, persistence, rotation, URL rejection")
    }
}
