import Foundation

/// Persisted pairing config: the watch-scope token + the runtime base PAL is
/// reached at (the gateway HTTPS URL). App-sandboxed UserDefaults — same
/// posture as the Capacitor phone app's stored token; Keychain hardening is a
/// noted follow-up. Replaces the old gitignored DevConfig.swift.
enum ConfigStore {
    private static let d = UserDefaults.standard
    private static let kToken = "watch_token"
    private static let kBase = "watch_base"

    static var token: String? { d.string(forKey: kToken) }
    static var base: String? { d.string(forKey: kBase) }
    static var isPaired: Bool { !(token ?? "").isEmpty }

    static func save(token: String, base: String) {
        d.set(token, forKey: kToken)
        d.set(base, forKey: kBase)
    }

    static func clear() {
        d.removeObject(forKey: kToken)
        d.removeObject(forKey: kBase)
    }
}
