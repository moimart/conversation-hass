// Server-side speech-to-text fallback for satellites whose on-device recognizer
// can't be used (e.g. a Meta Portal, whose only recognizer is Home Assistant's
// and which PAL can't bind to). Captures mic audio in the WebView as raw float32
// PCM and POSTs it to /api/satellite/stt, which resamples to 16 kHz and runs the
// SAME server STT the hub uses (hal-stt-service), returning the transcript.
// Bypasses the device recognizer entirely.

import type { HalConfig } from "../config/hal-config";

const SAT_STT_KEY = "hal-sat-stt-mode";   // "" / "auto" (default) | "server"

/** Device-local STT mode. "server" is latched after an on-device start failure. */
export function sttMode(): "auto" | "server" {
  try { return localStorage.getItem(SAT_STT_KEY) === "server" ? "server" : "auto"; }
  catch { return "auto"; }
}
export function setSttMode(m: "auto" | "server"): void {
  try { localStorage.setItem(SAT_STT_KEY, m); } catch { /* ignore */ }
}

let ctx: AudioContext | null = null;
let stream: MediaStream | null = null;
let source: MediaStreamAudioSourceNode | null = null;
let proc: ScriptProcessorNode | null = null;
let chunks: Float32Array[] = [];
let capturing = false;

export function isCapturing(): boolean { return capturing; }

/** Begin capturing mic audio (call on PTT down). Throws if the mic is unavailable. */
export async function startCapture(): Promise<void> {
  if (capturing) return;
  stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  ctx = new Ctor();
  if (ctx.state === "suspended") await ctx.resume();
  source = ctx.createMediaStreamSource(stream);
  proc = ctx.createScriptProcessor(4096, 1, 1);
  chunks = [];
  proc.onaudioprocess = (e) => {
    // Copy — the inputBuffer is reused across callbacks. We never write the
    // output buffer, so the node plays silence (no feedback) despite being
    // connected to the destination (required for the callback to fire).
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };
  source.connect(proc);
  proc.connect(ctx.destination);
  capturing = true;
}

function teardown(): number {
  const rate = ctx ? ctx.sampleRate : 16000;
  try { if (proc) { proc.disconnect(); proc.onaudioprocess = null; } } catch { /* ignore */ }
  try { if (source) source.disconnect(); } catch { /* ignore */ }
  try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
  try { if (ctx) void ctx.close(); } catch { /* ignore */ }
  proc = null; source = null; stream = null; ctx = null; capturing = false;
  return rate;
}

/** Stop capturing, POST the audio to the server, and return the transcript (""). */
export async function stopCaptureAndTranscribe(cfg: HalConfig): Promise<string> {
  if (!capturing) { teardown(); return ""; }
  const rate = teardown();
  let total = 0;
  for (const c of chunks) total += c.length;
  const pcm = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { pcm.set(c, off); off += c.length; }
  chunks = [];
  if (pcm.length === 0) return "";
  const headers: Record<string, string> = {
    "Content-Type": "application/octet-stream",
    "X-Sample-Rate": String(Math.round(rate)),
  };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  try {
    const res = await fetch(`${cfg.serverBaseUrl}/api/satellite/stt`, {
      method: "POST", headers, body: pcm.buffer,
    });
    if (!res.ok) return "";
    const j = await res.json();
    return typeof j.text === "string" ? j.text.trim() : "";
  } catch {
    return "";
  }
}
