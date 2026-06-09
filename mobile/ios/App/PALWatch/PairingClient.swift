import Foundation

/// Enrollment client — talks to PAL's LAN base (pairing is LAN-only: the
/// gateway deliberately doesn't proxy request/redeem, so a token can only be
/// minted at home, over cleartext HTTP — see the ATS exception in Info.plist).
/// The watch self-enrolls least-privilege (scope="watch").
enum PairingClient {
    struct Paired { let token: String; let base: String }

    enum Failure: LocalizedError {
        case message(String)
        var errorDescription: String? {
            if case .message(let m) = self { return m }
            return nil
        }
    }

    /// Ask PAL to show a pairing code on the kiosk display.
    static func requestCode(_ lan: String) async throws {
        _ = try await post(base: lan, path: "api/pair/request", body: [:])
    }

    /// Redeem the kiosk code for a watch-scope token; returns the token + the
    /// runtime base to use afterwards (the gateway URL PAL hands back, falling
    /// back to the LAN base when none is configured).
    static func redeem(_ lan: String, code: String) async throws -> Paired {
        let json = try await post(base: lan, path: "api/pair/redeem", body: [
            "code": code, "device_name": "Apple Watch Ultra", "scope": "watch",
        ])
        guard let token = json["token"] as? String, !token.isEmpty else {
            throw Failure.message((json["error"] as? String) ?? "pairing failed")
        }
        let gw = (json["gateway_url"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? trim(lan)
        return Paired(token: token, base: gw)
    }

    private static func trim(_ s: String) -> String {
        s.hasSuffix("/") ? String(s.dropLast()) : s
    }

    private static func post(base: String, path: String, body: [String: Any]) async throws -> [String: Any] {
        guard let url = URL(string: "\(trim(base))/\(path)") else { throw Failure.message("bad url") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 15
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if !(200...299).contains(code) {
            throw Failure.message((json["error"] as? String) ?? "HTTP \(code)")
        }
        return json
    }
}
