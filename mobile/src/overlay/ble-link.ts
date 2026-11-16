// PAL Round BLE link (Android only, via the PalLink native plugin): this phone
// relays a PAL Round's TLS traffic to the PAL gateway over Bluetooth LE when
// the Round has no Wi-Fi. Spec: firmware/pal-round/BLE_LINK.md.
//
// Off Android (iOS/web) isBleLinkSupported() is false and the settings section
// never appears — same gating idea as the Home Assistant launcher (apps.ts).

import { registerPlugin, Capacitor, type PluginListenerHandle } from "@capacitor/core";
import type { HalConfig } from "../config/hal-config";

export interface BleLinkStatus {
  enabled: boolean;
  paired: boolean;
  deviceName: string | null;
  gatewayHost: string | null;
  connected: boolean;
  streams: number;
  permission: boolean;
}

interface PalLinkPlugin {
  pair(opts: { gatewayHost?: string }): Promise<BleLinkStatus>;
  cancelPair(): Promise<void>;
  unpair(): Promise<BleLinkStatus>;
  start(opts: { gatewayHost: string }): Promise<BleLinkStatus>;
  stop(): Promise<BleLinkStatus>;
  status(): Promise<BleLinkStatus>;
  addListener(event: "statusChange", cb: (s: BleLinkStatus) => void): Promise<PluginListenerHandle>;
  addListener(event: "pairState", cb: (s: { state: "scanning" | "bonding" }) => void): Promise<PluginListenerHandle>;
}

export const PalLink = registerPlugin<PalLinkPlugin>("PalLink");

export function isBleLinkSupported(): boolean {
  return Capacitor.getPlatform() === "android";
}

/** The host the phone may relay to: the paired gateway's hostname (e.g.
 *  "pal.martinez.sh"), or null when this app never got a gateway URL. */
export function gatewayHostFor(cfg: HalConfig): string | null {
  const url = (cfg.gatewayBaseUrl || "").trim();
  if (!url) return null;
  try {
    const host = new URL(url).hostname;
    return host || null;
  } catch {
    return null;
  }
}

/** Ask the PAL server to put online PAL Rounds into Bluetooth pairing mode.
 *  Resolves the number of Rounds that got the request, or null if the server
 *  couldn't be asked (offline / older server). */
export async function requestRoundPairingMode(cfg: HalConfig): Promise<number | null> {
  try {
    const res = await fetch(`${cfg.serverBaseUrl.replace(/\/+$/, "")}/api/satellite/ble_pair`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
      },
      body: "{}",
    });
    if (!res.ok) return null;
    const j = await res.json();
    return typeof j.devices === "number" ? j.devices : null;
  } catch {
    return null;
  }
}

/** Human status line for the settings sheet. */
export function bleStatusText(s: BleLinkStatus): string {
  if (!s.paired) return "Not paired";
  const name = s.deviceName || "PAL Round";
  if (!s.enabled) return `Off · ${name}`;
  if (!s.permission) return "Bluetooth permission needed";
  if (!s.connected) return `Waiting for ${name}`;
  return `Connected · ${s.streams} ${s.streams === 1 ? "stream" : "streams"}`;
}
