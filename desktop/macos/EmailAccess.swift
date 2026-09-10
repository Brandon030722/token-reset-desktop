import Foundation
import CryptoKit

// Local beta access gate, not a server-side authorization boundary.
final class EmailAccess {
    static let defaultInviteHash = "5d304f5bd982309df1317922062f97a4e9eed44e98ecb4e4edce1304e894852b"
    let configURL: URL
    let defaults: UserDefaults
    init(configURL: URL, defaults: UserDefaults = .standard) {
        self.configURL = configURL; self.defaults = defaults
    }
    private var config: [String: Any] {
        guard let data = try? Data(contentsOf: configURL), data.count <= 16_384,
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return value
    }
    private var inviteHash: String? {
        let value = config["emailInviteHash"] as? String ?? Self.defaultInviteHash
        return value.range(of: "^[a-fA-F0-9]{64}$", options: .regularExpression) == nil ? nil : value.lowercased()
    }
    var unlocked: Bool {
        guard let expected = inviteHash else { return false }
        return defaults.string(forKey: "email.invite.acceptedHash") == expected
    }
    @discardableResult func verify(_ code: String) -> Bool {
        let value = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, value.count <= 128, let expected = inviteHash else { return false }
        let actual = SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
        guard actual == expected else { return false }
        defaults.set(expected, forKey: "email.invite.acceptedHash")
        return true
    }
    static func formURL(_ value: String) -> URL? {
        guard value.count <= 2048, let url = URL(string: value), url.scheme == "https",
              let host = url.host, !host.isEmpty, url.user == nil, url.password == nil else { return nil }
        return url
    }
    private var configuredURL: URL? { Self.formURL(config["subscriptionUrl"] as? String ?? "") }
    var subscriptionURL: URL? { unlocked ? configuredURL : nil }
    var publicState: [String: Any] {
        ["emailDelivery": config["emailDelivery"] as? String ?? "local", "inviteUnlocked": unlocked, "subscriptionConfigured": configuredURL != nil,
         "subscriptionUrl": subscriptionURL?.absoluteString ?? ""]
    }
    func saveForm(_ value: String) throws {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.isEmpty || Self.formURL(trimmed) != nil else { throw URLError(.badURL) }
        var updated = config
        updated["subscriptionUrl"] = trimmed
        let data = try JSONSerialization.data(withJSONObject: updated, options: [.prettyPrinted, .sortedKeys])
        try FileManager.default.createDirectory(at: configURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: configURL, options: .atomic)
    }
}
