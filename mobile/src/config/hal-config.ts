// Persisted connection config for the companion app.
//
// Stored via @capacitor/preferences (app-sandboxed). NOTE: the device token is
// a bearer credential — a hardening follow-up is to move `token` into Keychain/
// Keystore via a secure-storage plugin; Preferences is used in v1 so the first
// build has no extra native plugin to reconcile.

import { Preferences } from "@capacitor/preferences";

const KEY = "hal.config.v1";

export interface HalConfig {
  /** http(s)://host:8765 — used for /api/* and /themes/* */
  serverBaseUrl: string;
  /** ws(s)://host:8765/ws/ui — the read-only display feed */
  wsUrl: string;
  /** paired device token (Bearer / ?token=) */
  token: string;
  /** demo mode flips off real auth assumptions in the UI */
  demo?: boolean;
  /** friendly server name returned at redeem time */
  serverName?: string;
}

/** Derive the ws://.../ws/ui URL from an http(s) base. */
export function wsUrlFromBase(serverBaseUrl: string): string {
  const u = serverBaseUrl.replace(/\/+$/, "");
  const ws = u.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  return `${ws}/ws/ui`;
}

export async function loadConfig(): Promise<HalConfig | null> {
  const { value } = await Preferences.get({ key: KEY });
  if (!value) return null;
  try {
    const c = JSON.parse(value) as HalConfig;
    if (c && c.serverBaseUrl && c.wsUrl) return c;
  } catch {
    /* ignore corrupt config */
  }
  return null;
}

export async function saveConfig(cfg: HalConfig): Promise<void> {
  await Preferences.set({ key: KEY, value: JSON.stringify(cfg) });
}

export async function clearConfig(): Promise<void> {
  await Preferences.remove({ key: KEY });
}

/** Inject config for the reused display scripts (read by rpi/web/app.js). */
export function injectHalConfig(cfg: HalConfig): void {
  (window as unknown as { HAL_CONFIG: unknown }).HAL_CONFIG = {
    serverBaseUrl: cfg.serverBaseUrl,
    wsUrl: cfg.wsUrl,
    token: cfg.token,
    // Pin landscape so the kiosk's portrait mounting doesn't rotate the phone UI.
    pinLandscape: true,
  };
}
