// Pairing client: redeem a code shown on the HAL display for a device token,
// and validate the server URL.

import { wsUrlFromBase, type HalConfig } from "../config/hal-config";

export async function checkServer(serverBaseUrl: string): Promise<boolean> {
  try {
    const res = await fetch(`${serverBaseUrl.replace(/\/+$/, "")}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export interface RedeemResult {
  ok: boolean;
  config?: HalConfig;
  error?: string;
}

export async function redeemCode(
  serverBaseUrl: string,
  code: string,
  deviceName: string,
): Promise<RedeemResult> {
  const base = serverBaseUrl.replace(/\/+$/, "");
  try {
    const res = await fetch(`${base}/api/pair/redeem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code.trim(), device_name: deviceName }),
    });
    if (res.status === 200) {
      const data = (await res.json()) as { token: string; server_name?: string };
      return {
        ok: true,
        config: {
          serverBaseUrl: base,
          wsUrl: wsUrlFromBase(base),
          token: data.token,
          serverName: data.server_name,
        },
      };
    }
    if (res.status === 429) return { ok: false, error: "Too many attempts — wait a moment." };
    return { ok: false, error: "Invalid or expired code." };
  } catch (e) {
    return { ok: false, error: `Could not reach the server (${String(e)}).` };
  }
}
