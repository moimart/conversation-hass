import Foundation

/// Minimal PAL API client for the watch: one call, `command`, which POSTs the
/// dictated text with wait_reply=true so PAL's reply comes back in the HTTP
/// response (the watch scope has no /ws/ui channel for async replies). Talks
/// to the gateway base over HTTPS — same path as away-from-home use.
enum PALClient {
    struct CommandRequest: Encodable {
        let text: String
        let wait_reply: Bool
    }

    struct CommandResponse: Decodable {
        let status: String
        let reply: String?
        let message: String?
    }

    enum Failure: LocalizedError {
        case httpStatus(Int)
        case badPayload

        var errorDescription: String? {
            switch self {
            case .httpStatus(let code): return "HTTP \(code)"
            case .badPayload: return "unexpected response"
            }
        }
    }

    /// Send a command and return PAL's spoken reply text ("" if none).
    static func command(_ text: String) async throws -> String {
        var request = URLRequest(
            url: DevConfig.serverBase.appending(path: "api/command"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(DevConfig.watchToken)",
                         forHTTPHeaderField: "Authorization")
        // The turn can take a while (LLM + tools); server caps at 90s.
        request.timeoutInterval = 95
        request.httpBody = try JSONEncoder().encode(
            CommandRequest(text: text, wait_reply: true))

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw Failure.badPayload
        }
        guard http.statusCode == 200 else {
            throw Failure.httpStatus(http.statusCode)
        }
        let decoded = try JSONDecoder().decode(CommandResponse.self, from: data)
        return (decoded.reply ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
