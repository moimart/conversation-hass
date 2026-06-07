// Persisted connection config for the companion app.
//
// Split storage by sensitivity: the device TOKEN (a bearer credential = a house
// key) lives in the iOS Keychain / Android Keystore-backed secure store; the
// rest of the config (server URLs, names) is non-secret and stays in
// app-sandboxed Preferences. Existing installs that kept the token inline in
// the Preferences blob are migrated to secure storage on first load.

import { Preferences } from "@capacitor/preferences";
import { SecureStorage } from "@aparajita/capacitor-secure-storage";
import { Capacitor } from "@capacitor/core";

const KEY = "hal.config.v1";
const TOKEN_KEY = "hal.device.token";

// --- token secure store (Keychain/Keystore on native; Preferences on web) ----
// On web/dev there's no native secure enclave; fall back to Preferences so the
// browser build keeps working. Native builds always hit the real secure store.
const _isWeb = Capacitor.getPlatform() === "web";

async function tokenGet(): Promise<string | null> {
  if (_isWeb) return (await Preferences.get({ key: TOKEN_KEY })).value ?? null;
  try {
    const v = await SecureStorage.get(TOKEN_KEY);
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

async function tokenSet(token: string): Promise<void> {
  if (_isWeb) { await Preferences.set({ key: TOKEN_KEY, value: token }); return; }
  await SecureStorage.set(TOKEN_KEY, token);
}

async function tokenClear(): Promise<void> {
  if (_isWeb) { await Preferences.remove({ key: TOKEN_KEY }); return; }
  try { await SecureStorage.remove(TOKEN_KEY); } catch { /* already absent */ }
}

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
  let stored: Partial<HalConfig> & { token?: string };
  try {
    stored = JSON.parse(value);
  } catch {
    return null;   // corrupt config
  }
  if (!stored || !stored.serverBaseUrl || !stored.wsUrl) return null;

  let token = await tokenGet();
  // Migration: older installs kept the token inline in the Preferences blob.
  // Move it into secure storage and strip it from the plaintext blob.
  if (!token && stored.token) {
    token = stored.token;
    await tokenSet(token);
    const { token: _dropped, ...rest } = stored;
    await Preferences.set({ key: KEY, value: JSON.stringify(rest) });
  }
  return { ...(stored as HalConfig), token: token ?? "" };
}

export async function saveConfig(cfg: HalConfig): Promise<void> {
  const { token, ...rest } = cfg;
  await Preferences.set({ key: KEY, value: JSON.stringify(rest) });
  if (token) await tokenSet(token);
  else await tokenClear();
}

export async function clearConfig(): Promise<void> {
  await Preferences.remove({ key: KEY });
  await tokenClear();
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
