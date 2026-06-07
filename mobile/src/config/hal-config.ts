// Persisted connection config for the companion app.
//
// Stored via @capacitor/preferences (app-sandboxed). NOTE: the device token is
// a bearer credential — a hardening follow-up is to move `token` into Keychain/
// Keystore via a secure-storage plugin; Preferences is used in v1 so the first
// build has no extra native plugin to reconcile.

import { Preferences } from "@capacitor/preferences";

const KEY = "hal.config.v1";

export interface HalConfig {
  /** http(s)://host:8765 — the HOME (LAN/Tailscale) base for /api/* and /themes/* */
  serverBaseUrl: string;
  /** ws(s)://host:8765/ws/ui — the read-only display feed (home base) */
  wsUrl: string;
  /** paired device token (Bearer / ?token=) */
  token: string;
  /** demo mode flips off real auth assumptions in the UI */
  demo?: boolean;
  /** friendly server name returned at redeem time */
  serverName?: string;
  /** optional public satellite-gateway base (https://pal.example.com). When the
   *  home base is unreachable (away from home), the app fails over to this. */
  gatewayBaseUrl?: string;
}

/** Derive the ws://.../ws/ui URL from an http(s) base. */
export function wsUrlFromBase(serverBaseUrl: string): string {
  const u = serverBaseUrl.replace(/\/+$/, "");
  const ws = u.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  return `${ws}/ws/ui`;
}

/** Pick the base URL to use this session: the home base if its /health answers
 *  within `timeoutMs`, otherwise the gateway base (when configured). Pairing
 *  is always done against the home base, so a fresh config with no gateway
 *  simply always returns home. */
export async function resolveActiveBase(
  cfg: HalConfig,
  timeoutMs = 2000,
): Promise<{ serverBaseUrl: string; wsUrl: string; usingGateway: boolean }> {
  const home = cfg.serverBaseUrl.replace(/\/+$/, "");
  const gw = (cfg.gatewayBaseUrl || "").replace(/\/+$/, "");
  if (gw && gw !== home) {
    const reachable = await probeHealth(home, timeoutMs);
    if (!reachable) {
      return { serverBaseUrl: gw, wsUrl: wsUrlFromBase(gw), usingGateway: true };
    }
  }
  return { serverBaseUrl: home, wsUrl: wsUrlFromBase(home), usingGateway: false };
}

async function probeHealth(base: string, timeoutMs: number): Promise<boolean> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}/health`, { cache: "no-store", signal: ctrl.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
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

/** Inject config for the reused display scripts (read by rpi/web/app.js).
 *  `active` overrides the base/ws when the app has failed over to the gateway;
 *  defaults to the stored home base. */
export function injectHalConfig(
  cfg: HalConfig,
  active?: { serverBaseUrl: string; wsUrl: string },
): void {
  (window as unknown as { HAL_CONFIG: unknown }).HAL_CONFIG = {
    serverBaseUrl: active?.serverBaseUrl ?? cfg.serverBaseUrl,
    wsUrl: active?.wsUrl ?? cfg.wsUrl,
    token: cfg.token,
    // Pin landscape so the kiosk's portrait mounting doesn't rotate the phone UI.
    pinLandscape: true,
  };
}
