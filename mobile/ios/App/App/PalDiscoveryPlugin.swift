import Foundation
import Capacitor
import Network

/// Native LAN autodiscovery for the iPhone webview. This is an SPM-only Capacitor
/// project, so the podspec-only capacitor-zeroconf plugin can't be linked on iOS
/// — instead we expose a tiny native bridge over the same `NWBrowser` Bonjour API
/// the watch uses. `discover.ts` calls this first on iOS and falls back to
/// capacitor-zeroconf on Android.
///
/// `discover({ timeoutMs })` resolves `{ url, name }` for the first PAL server
/// found (`_pal._tcp`), or `{}` if none answered before the timeout. Never
/// rejects — discovery is best-effort and the caller treats empty as "type it".
@objc(PalDiscoveryPlugin)
public class PalDiscoveryPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "PalDiscoveryPlugin"
    public let jsName = "PalDiscovery"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "discover", returnType: CAPPluginReturnPromise),
    ]

    @objc func discover(_ call: CAPPluginCall) {
        let timeout = (call.getDouble("timeoutMs") ?? 3000) / 1000.0
        browse(timeout: timeout) { found in
            if let (url, name) = found {
                call.resolve(["url": url, "name": name])
            } else {
                call.resolve([:])
            }
        }
    }

    private func browse(timeout: TimeInterval, completion: @escaping ((String, String)?) -> Void) {
        let params = NWParameters()
        params.includePeerToPeer = false
        let browser = NWBrowser(for: .bonjour(type: "_pal._tcp", domain: "local."), using: params)

        var done = false
        let finish: ((String, String)?) -> Void = { value in
            if done { return }
            done = true
            browser.cancel()
            DispatchQueue.main.async { completion(value) }
        }

        browser.browseResultsChangedHandler = { results, _ in
            guard let first = results.first else { return }
            var name = "PAL"
            if case let .bonjour(txt) = first.metadata, let n = txt["name"], !n.isEmpty { name = n }
            resolveEndpoint(first.endpoint) { url in if let url { finish((url, name)) } }
        }
        browser.start(queue: .global())
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish(nil) }
    }
}

/// Resolve a Bonjour endpoint to `http://ip:port` by briefly connecting and
/// reading the kernel-resolved remote host/port (also a liveness check).
private func resolveEndpoint(_ endpoint: NWEndpoint, completion: @escaping (String?) -> Void) {
    let conn = NWConnection(to: endpoint, using: .tcp)
    var settled = false
    let done: (String?) -> Void = { url in
        if settled { return }
        settled = true
        conn.cancel()
        completion(url)
    }
    conn.stateUpdateHandler = { state in
        switch state {
        case .ready:
            if case let .hostPort(host, port)? = conn.currentPath?.remoteEndpoint,
               let ip = ipString(host) {
                done("http://\(ip):\(port.rawValue)")
            } else {
                done(nil)
            }
        case .failed, .cancelled:
            done(nil)
        default:
            break
        }
    }
    conn.start(queue: .global())
}

private func ipString(_ host: NWEndpoint.Host) -> String? {
    switch host {
    case .ipv4(let addr):
        return addr.debugDescription.split(separator: "%").first.map(String.init)
    case .name(let name, _):
        return name
    default:
        return nil  // IPv4 only — the ai-server URL is IPv4 on the LAN
    }
}
