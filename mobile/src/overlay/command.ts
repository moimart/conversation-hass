// Send a text command to HAL. The server echoes it back as a `transcription`
// broadcast over /ws/ui, so it appears on the mirrored display automatically —
// we don't echo locally.

import type { HalConfig } from "../config/hal-config";

export async function sendCommand(cfg: HalConfig, text: string): Promise<boolean> {
  const t = text.trim();
  if (!t) return false;
  try {
    const res = await fetch(`${cfg.serverBaseUrl}/api/command`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
      },
      body: JSON.stringify({ text: t }),
    });
    if (!res.ok) {
      console.warn(`[hal] /api/command -> ${res.status}`);
      return false;
    }
    return true;
  } catch (e) {
    console.warn("[hal] /api/command failed", e);
    return false;
  }
}
