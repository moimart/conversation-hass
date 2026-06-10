// Satellite idle screensaver: after N minutes with no input, ask the server to
// start THIS device's ambient photo frame (targeted to it). Any input stops it.
//
// The server pushes show_photo_frame/photo_frame_update to this device only; the
// reused photo_frame.js renders them and toggles `body.photo-frame-active`.
// Because the phone's /ws/ui doesn't process the kiosk's photo_frame_dismissed
// message, we explicitly POST stop on activity so the server tears the per-device
// session (and its HA subscription) down.
//
// Source of truth is the DOM class, NOT a local flag: a household broadcast
// (announcement / timer / image) dismisses the frame server-side and removes
// `photo-frame-active` with no local input — a stale local flag would then wedge
// the screensaver "off" forever (the one-shot timer had already fired, so it
// never re-triggered until you physically touched the device). A periodic check
// against `photo-frame-active` self-heals: whenever we've been idle past the
// threshold and the frame is NOT showing, we (re)start it.

import type { HalConfig } from "../config/hal-config";

const IDLE_MS = 4 * 60 * 1000;  // 4 minutes of no input before the frame
const CHECK_MS = 20 * 1000;     // re-evaluate idleness this often

function frameShowing(): boolean {
  return document.body.classList.contains("photo-frame-active");
}

export function startPhotoFrameIdle(cfg: HalConfig): void {
  const base = cfg.serverBaseUrl.replace(/\/+$/, "");
  const auth: Record<string, string> = cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {};

  let lastActivity = Date.now();
  let starting = false;

  async function startFrame(): Promise<void> {
    if (starting || frameShowing()) return;
    starting = true;
    try {
      await fetch(`${base}/api/satellite/photo_frame/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...auth },
        body: "{}",
      });
    } catch (e) {
      console.warn("[hal] photo-frame start failed", e);
    } finally {
      starting = false;
    }
  }

  async function stopFrame(): Promise<void> {
    if (!frameShowing()) return;
    try {
      await fetch(`${base}/api/satellite/photo_frame/stop`, { method: "POST", headers: auth });
    } catch { /* ignore */ }
  }

  function onActivity(): void {
    lastActivity = Date.now();
    if (frameShowing()) void stopFrame();
  }

  for (const ev of ["pointerdown", "keydown", "touchstart"]) {
    window.addEventListener(ev, onActivity, { passive: true });
  }

  // Self-healing: (re)start the frame whenever we've been idle long enough and
  // it isn't already showing — recovers after a broadcast dismisses it.
  setInterval(() => {
    if (Date.now() - lastActivity >= IDLE_MS && !frameShowing()) void startFrame();
  }, CHECK_MS);
}
