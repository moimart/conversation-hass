import SwiftUI

/// One-time enrollment: enter PAL's LAN URL, get a code on the kiosk, type it,
/// redeem it scoped=watch, persist. Pairing is LAN-only by design.
struct EnrollView: View {
    let onPaired: () -> Void
    private let defaultLan: String

    @State private var lan: String
    @State private var code = ""
    @State private var status = ""
    @State private var busy = false

    init(defaultLan: String, onPaired: @escaping () -> Void) {
        self.onPaired = onPaired
        self.defaultLan = defaultLan
        _lan = State(initialValue: defaultLan)
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                Text("Pair PAL").font(.headline)

                Text("PAL server (LAN)").font(.system(size: 10)).foregroundStyle(.secondary)
                TextField("http://…:8765", text: $lan)
                    .font(.caption2)

                Button(action: showCode) {
                    Text("Show code on kiosk").font(.caption2)
                }
                .disabled(busy)

                Text("Code from kiosk").font(.system(size: 10)).foregroundStyle(.secondary)
                TextField("123456", text: $code)
                    .font(.caption2)

                Button(action: pair) {
                    Text(busy ? "…" : "Pair").font(.footnote)
                }
                .disabled(busy)
                .tint(.teal)

                if !status.isEmpty {
                    Text(status).font(.system(size: 10)).foregroundStyle(.cyan)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, 8)
        }
        .onAppear {
            // Best-effort LAN autodiscovery: while the field still holds the
            // untouched default, replace it with the server found on the network.
            Discovery.findServer { url in
                guard let url, lan == defaultLan else { return }
                lan = url
                status = "found PAL on your network"
            }
        }
    }

    private func showCode() {
        busy = true; status = "asking PAL…"
        Task {
            do { try await PairingClient.requestCode(lan); status = "code shown on kiosk" }
            catch { status = "err: \(error.localizedDescription)" }
            busy = false
        }
    }

    private func pair() {
        let digits = code.filter(\.isNumber)
        guard digits.count >= 4 else { status = "enter the code"; return }
        busy = true; status = "pairing…"
        Task {
            do {
                let p = try await PairingClient.redeem(lan, code: digits)
                ConfigStore.save(token: p.token, base: p.base)
                onPaired()
            } catch {
                status = "failed: \(error.localizedDescription)"
                busy = false
            }
        }
    }
}
