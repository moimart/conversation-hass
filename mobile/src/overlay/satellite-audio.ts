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
let playing = false;
let activeSrc: AudioBufferSourceNode | null = null;

function setOrb(state: "speaking" | "idle"): void {
  const fn = (window as unknown as { HALSetState?: (s: string) => void }).HALSetState;
  if (typeof fn === "function") {
    try { fn(state); } catch { /* ignore */ }
  }
}

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
    // True from the moment a turn's TTS arrives until ITS playback ends. The
    // reused app.js uses this to keep the orb in the speaking state while our
    // local audio plays (the server ends the turn early — see app.js
    // `case "state"`).
    isPlaying(): boolean { return playing; },
    async play(url: string, _mime?: string): Promise<void> {
      // Drive the orb to "speaking" as soon as the turn's audio is incoming, so
      // the animation runs even before decode finishes.
      playing = true;
      setOrb("speaking");
      try {
        const abs = url.startsWith("http")
          ? url
          : cfg.serverBaseUrl.replace(/\/+$/, "") + url;
        const res = await fetch(abs, {
          headers: cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {},
          cache: "no-store",
        });
        if (!res.ok) {
          console.warn(`[hal] tts fetch -> ${res.status}`);
          playing = false; setOrb("idle"); return;
        }
        const data = await res.arrayBuffer();
        const c = getCtx();
        if (!c) {
          console.warn("[hal] no AudioContext");
          playing = false; setOrb("idle"); return;
        }
        if (c.state === "suspended") await c.resume();
        const audio = await c.decodeAudioData(data);
        // A newer turn may have superseded this one while we were fetching.
        if (activeSrc) { try { activeSrc.onended = null; activeSrc.stop(); } catch { /* ignore */ } }
        const src = c.createBufferSource();
        src.buffer = audio;
        src.connect(c.destination);
        src.onended = () => {
          if (src !== activeSrc) return; // superseded
          activeSrc = null;
          playing = false;
          setOrb("idle");
        };
        activeSrc = src;
        src.start(0);
      } catch (e) {
        console.warn("[hal] satellite audio play failed", e);
        playing = false; setOrb("idle");
      }
    },
  };
}
