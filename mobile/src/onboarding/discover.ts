// LAN autodiscovery: browse for the PAL ai-server's mDNS advertisement
// (`_pal._tcp.local.`, published by the host-networked pal-mdns container) and
// return its URL + friendly name so onboarding can prefill the field instead of
// making the user type `http://10.20.30.185:8765`.
//
// Best-effort: any failure (no plugin on web, mDNS blocked on the Wi-Fi, nothing
// found before the timeout) resolves to null and the caller falls back to the
// manual default — discovery is a convenience, never a gate.

import { ZeroConf, type ZeroConfWatchResult } from "capacitor-zeroconf";
import { checkServer } from "./pairing";

const SERVICE_TYPE = "_pal._tcp.";
const SERVICE_DOMAIN = "local.";

export interface DiscoveredServer {
  url: string;
  name: string;
}

/** Resolve the first reachable PAL server found on the LAN, or null on timeout. */
export async function discoverServer(timeoutMs = 3000): Promise<DiscoveredServer | null> {
  try {
    return await new Promise<DiscoveredServer | null>((resolve) => {
      let settled = false;
      const finish = (val: DiscoveredServer | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        // Tear the browse down (and the underlying native resolver) so we don't
        // leak a multicast listener past onboarding.
        void ZeroConf.unwatch({ type: SERVICE_TYPE, domain: SERVICE_DOMAIN }).catch(() => {});
        void ZeroConf.close().catch(() => {});
        resolve(val);
      };
      const timer = setTimeout(() => finish(null), timeoutMs);

      ZeroConf.watch({ type: SERVICE_TYPE, domain: SERVICE_DOMAIN }, (res: ZeroConfWatchResult) => {
        // Only 'resolved' carries the IP + port; 'added' is just a name.
        if (res.action !== "resolved") return;
        const svc = res.service;
        const ip = (svc.ipv4Addresses || [])[0];
        if (!ip || !svc.port) return;
        const scheme = svc.txtRecord?.scheme || "http";
        const url = `${scheme}://${ip}:${svc.port}`;
        const name = svc.txtRecord?.name || svc.name || "PAL";
        // Confirm it actually answers /health before suggesting it — guards
        // against a stale record or a same-name service on the LAN.
        void checkServer(url).then((ok) => {
          if (ok) finish({ url, name });
        });
      }).catch(() => finish(null));
    });
  } catch {
    return null; // web / plugin unavailable
  }
}
