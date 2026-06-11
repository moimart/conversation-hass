import Foundation
import Network

/// LAN autodiscovery for the watch: browse for the PAL ai-server's mDNS advert
/// (`_pal._tcp`, published by the pal-mdns container) and report the first
/// resolved `http://ip:port`. Best-effort — any failure (mDNS blocked, nothing
/// found before the timeout, local-network permission denied) just calls back
/// with nil and enrollment falls back to the manually-typed URL.
enum Discovery {
    static func findServer(timeout: TimeInterval = 3, completion: @escaping (String?) -> Void) {
        let params = NWParameters()
        params.includePeerToPeer = false
        let browser = NWBrowser(for: .bonjour(type: "_pal._tcp", domain: "local."), using: params)

        var done = false
        let finish: (String?) -> Void = { url in
            if done { return }
            done = true
            browser.cancel()
            DispatchQueue.main.async { completion(url) }
        }

        browser.browseResultsChangedHandler = { results, _ in
            guard let first = results.first else { return }
            resolve(first.endpoint) { url in if let url { finish(url) } }
        }
        browser.start(queue: .global())
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish(nil) }
    }

    /// Resolve a Bonjour endpoint to `http://ip:port` by opening a short TCP
    /// connection and reading the kernel-resolved remote host/port. This also
    /// doubles as a reachability check (only a live server resolves to .ready).
    private static func resolve(_ endpoint: NWEndpoint, completion: @escaping (String?) -> Void) {
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

    private static func ipString(_ host: NWEndpoint.Host) -> String? {
        switch host {
        case .ipv4(let addr):
            // debugDescription can carry a "%interface" zone suffix — strip it.
            return addr.debugDescription.split(separator: "%").first.map(String.init)
        case .name(let name, _):
            return name
        default:
            return nil  // skip IPv6 — the ai-server URL is IPv4 on the LAN
        }
    }
}
