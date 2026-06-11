// Send a text command to HAL. We pass `wait_reply: true` so the server routes
// the turn PRIVATELY to this device (origin = our token) regardless of whether
// our /ws/ui socket is connected at that instant. Without it, a command sent
// while the socket is briefly down (app resume, network flap) is treated as a
// GLOBAL turn and broadcast to the kiosk — i.e. your phone command shows + speaks
// on the wall display. With the socket up (the normal case) the transcript,
// reply, and TTS still route to this device exactly as before — the mirrored
// display is unchanged — the only difference is the kiosk never sees it.

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
      body: JSON.stringify({ text: t, wait_reply: true }),
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
