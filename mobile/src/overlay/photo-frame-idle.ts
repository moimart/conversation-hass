// Satellite idle screensaver: after N minutes with no input, ask the server to
// start THIS device's ambient photo frame (targeted to it). Any input stops it.
//
// The server pushes show_photo_frame/photo_frame_update to this device only; the
// reused photo_frame.js renders them. Because the phone's /ws/ui doesn't process
// the photo_frame_dismissed message the kiosk uses, we explicitly POST stop on
// activity so the server tears the per-device session (and its HA subscription)
// down.

import type { HalConfig } from "../config/hal-config";

const IDLE_MS = 4 * 60 * 1000; // 4 minutes (client-side; configurable later)

let timer: ReturnType<typeof setTimeout> | null = null;
let framed = false;

export function startPhotoFrameIdle(cfg: HalConfig): void {
  const base = cfg.serverBaseUrl.replace(/\/+$/, "");
  const auth: Record<string, string> = cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {};

  async function startFrame(): Promise<void> {
    if (framed) return;
    framed = true;
    try {
      await fetch(`${base}/api/satellite/photo_frame/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...auth },
        body: "{}",
      });
    } catch (e) {
      framed = false;
      console.warn("[hal] photo-frame start failed", e);
    }
  }

  async function stopFrame(): Promise<void> {
    if (!framed) return;
    framed = false;
    try {
      await fetch(`${base}/api/satellite/photo_frame/stop`, { method: "POST", headers: auth });
    } catch { /* ignore */ }
  }

  function reset(): void {
    void stopFrame();
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => void startFrame(), IDLE_MS);
  }

  for (const ev of ["pointerdown", "keydown", "touchstart"]) {
    window.addEventListener(ev, reset, { passive: true });
  }
  reset();
}
