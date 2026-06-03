// Plays HAL's server (Wyoming) TTS audio on THIS device in satellite mode.
//
// In satellite mode the server caches the turn's audio and sends a `tts_play`
// message (dispatched by the reused app.js to window.HALSatelliteAudio.play).
// We fetch the cached WAV from GET /api/satellite/tts (Bearer) and play it.
//
// Playback uses the Web Audio API rather than an <audio> element: the response
// audio arrives AFTER an async fetch, i.e. outside the user-gesture, so a plain
// <audio>.play() gets blocked by the WebView autoplay policy. Instead we create
// and resume() an AudioContext on the command-send/mic tap (a real gesture),
// which keeps programmatic playback allowed for the rest of the session.

import type { HalConfig } from "../config/hal-config";

let ctx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (!ctx) {
    const Ctor =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    try { ctx = new Ctor(); } catch { return null; }
  }
  return ctx;
}

/** Unlock audio on a user gesture (call from the command-send / mic tap). */
export function unlockAudio(): void {
  const c = getCtx();
  if (!c) return;
  if (c.state === "suspended") void c.resume();
  // A 1-frame silent buffer started within the gesture fully unlocks playback.
  try {
    const buf = c.createBuffer(1, 1, 22050);
    const src = c.createBufferSource();
    src.buffer = buf;
    src.connect(c.destination);
    src.start(0);
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
        const data = await res.arrayBuffer();
        const c = getCtx();
        if (!c) { console.warn("[hal] no AudioContext"); return; }
        if (c.state === "suspended") await c.resume();
        const audio = await c.decodeAudioData(data);
        const src = c.createBufferSource();
        src.buffer = audio;
        src.connect(c.destination);
        src.start(0);
      } catch (e) {
        console.warn("[hal] satellite audio play failed", e);
      }
    },
  };
}
