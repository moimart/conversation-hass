// LAN autodiscovery: browse for the PAL ai-server's mDNS advertisement
// (`_pal._tcp.local.`, published by the host-networked pal-mdns container) and
// return its URL + friendly name so onboarding can prefill the field instead of
// making the user type `http://10.20.30.185:8765`.
//
// Two native paths, one JS API:
//   - iOS: a small native Capacitor bridge (PalDiscoveryPlugin.swift / NWBrowser)
//     — this is an SPM-only project, so the podspec-only capacitor-zeroconf can't
//     link on iOS. Tried first.
//   - Android: capacitor-zeroconf (NsdManager), used when the native bridge isn't
//     implemented (i.e. everywhere except iOS).
//
// Best-effort: any failure (no plugin on web, mDNS blocked on the Wi-Fi, nothing
// found before the timeout) resolves to null and the caller falls back to the
// manual default — discovery is a convenience, never a gate.

import { registerPlugin, Capacitor } from "@capacitor/core";
import { ZeroConf, type ZeroConfWatchResult } from "capacitor-zeroconf";
import { checkServer } from "./pairing";

const SERVICE_TYPE = "_pal._tcp.";
const SERVICE_DOMAIN = "local.";

export interface DiscoveredServer {
  url: string;
  name: string;
}

interface PalDiscoveryPlugin {
  discover(opts: { timeoutMs: number }): Promise<{ url?: string; name?: string }>;
}
const PalDiscovery = registerPlugin<PalDiscoveryPlugin>("PalDiscovery");

/** Resolve the first reachable PAL server found on the LAN, or null on timeout. */
export async function discoverServer(timeoutMs = 3000): Promise<DiscoveredServer | null> {
  // iOS: native NWBrowser bridge. On Android the plugin is unimplemented and the
  // call rejects → we fall through to the zeroconf path below.
  try {
    const r = await PalDiscovery.discover({ timeoutMs });
    if (r && r.url && (await checkServer(r.url))) {
      return { url: r.url, name: r.name || "PAL" };
    }
    // The native bridge answered (empty = nothing found) — on iOS that's final;
    // zeroconf isn't linked there anyway.
    if (Capacitor.getPlatform() === "ios") return null;
  } catch {
    // PalDiscovery not implemented here (Android / web) — try zeroconf next.
  }
  return zeroconfDiscover(timeoutMs);
}

/** Android (and any platform with capacitor-zeroconf) path. */
async function zeroconfDiscover(timeoutMs: number): Promise<DiscoveredServer | null> {
  try {
    return await new Promise<DiscoveredServer | null>((resolve) => {
      let settled = false;
      const finish = (val: DiscoveredServer | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
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
        // Confirm it actually answers /health before suggesting it.
        void checkServer(url).then((ok) => {
          if (ok) finish({ url, name });
        });
      }).catch(() => finish(null));
    });
  } catch {
    return null; // web / plugin unavailable
  }
}
