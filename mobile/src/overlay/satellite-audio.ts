// Plays HAL's server (Wyoming) TTS audio on THIS device in satellite mode.
//
// In satellite mode the server caches the turn's audio and sends a `tts_play`
// message (dispatched by the reused app.js to window.HALSatelliteAudio.play).
// We fetch the cached WAV from GET /api/satellite/tts (Bearer-authenticated) and
// play it via a single reused <audio> element. Autoplay is unlocked on the
// command-send tap (a user gesture) so subsequent plays don't get blocked.

import type { HalConfig } from "../config/hal-config";

let audioEl: HTMLAudioElement | null = null;
let unlocked = false;

function el(): HTMLAudioElement {
  if (!audioEl) {
    audioEl = document.createElement("audio");
    audioEl.preload = "auto";
    audioEl.setAttribute("playsinline", "");
    document.body.appendChild(audioEl);
  }
  return audioEl;
}

/** Prime autoplay on a user gesture (call from the command-send / mic tap). */
export function unlockAudio(): void {
  if (unlocked) return;
  const a = el();
  try {
    a.muted = true;
    void a
      .play()
      .then(() => { a.pause(); a.currentTime = 0; a.muted = false; unlocked = true; })
      .catch(() => { a.muted = false; });
  } catch { /* ignore */ }
}

export function mountSatelliteAudio(cfg: HalConfig): void {
  (window as unknown as { HALSatelliteAudio: unknown }).HALSatelliteAudio = {
    async play(url: string, _mime?: string): Promise<void> {
      try {
        const abs = url.startsWith("http")
          ? url
          : cfg.serverBaseUrl.replace(/\/+$/, "") + url;
        const res = await fetch(abs, {
          headers: cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {},
          cache: "no-store",
        });
        if (!res.ok) { console.warn(`[hal] tts fetch -> ${res.status}`); return; }
        const blob = await res.blob();
        const a = el();
        const obj = URL.createObjectURL(blob);
        a.onended = () => URL.revokeObjectURL(obj);
        a.src = obj;
        await a.play();
      } catch (e) {
        console.warn("[hal] satellite audio play failed", e);
      }
    },
  };
}
