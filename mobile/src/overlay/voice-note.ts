// Voice-note recorder. Captures mic audio in the WebView the same way the
// server-STT fallback does (stt.ts: getUserMedia + ScriptProcessorNode), but
// downsamples to 16 kHz mono and quantizes to s16le ON THE FLY, so a 10-minute
// note is ~19 MB in memory/on the wire instead of ~115 MB of 48 kHz float32.
// Uploaded to POST /api/satellite/note, which transcribes + stores it.
//
// Deliberately separate state from stt.ts so a note can never clobber (or be
// clobbered by) a PTT capture — the overlay refuses to run both at once.

import type { HalConfig } from "../config/hal-config";

export const NOTE_MAX_SECONDS = 600;          // 10-minute cap
const TARGET_RATE = 16000;

export type NoteRow = {
  id: number;
  ts: string;
  text: string;
  origin: string | null;
  duration_s: number;
};

export type NoteUploadResult =
  | { ok: true; note: NoteRow }
  | { ok: false; reason: "no_speech" | "empty" | "error"; message?: string };

let ctx: AudioContext | null = null;
let stream: MediaStream | null = null;
let source: MediaStreamAudioSourceNode | null = null;
let proc: ScriptProcessorNode | null = null;
let recording = false;
let startedAt = 0;

// Streaming box-filter decimator state (input rate -> outRate).
let outRate = TARGET_RATE;
let ratio = 1;            // input samples per output sample (>= 1)
let acc = 0;              // running sum of the current output sample's inputs
let accN = 0;
let inPos = 0;            // input samples consumed (global)
let nextEdge = 1;         // input position where the current output sample ends
let out: Int16Array[] = [];
let outLen = 0;

export function isRecordingNote(): boolean { return recording; }
export function noteElapsedSeconds(): number {
  return recording ? (performance.now() - startedAt) / 1000 : 0;
}

function toS16(x: number): number {
  const v = x < -1 ? -1 : x > 1 ? 1 : x;
  return v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
}

function push(samples: Float32Array): void {
  const buf = new Int16Array(Math.ceil(samples.length / ratio) + 2);
  let n = 0;
  if (ratio === 1) {
    for (let i = 0; i < samples.length; i++) buf[n++] = toS16(samples[i]);
  } else {
    // Average every input sample that falls in each output sample's span: a
    // cheap low-pass that keeps speech intelligible without aliasing hiss.
    for (let i = 0; i < samples.length; i++) {
      acc += samples[i]; accN++; inPos++;
      if (inPos >= nextEdge) {
        buf[n++] = toS16(acc / accN);
        acc = 0; accN = 0;
        nextEdge += ratio;
      }
    }
  }
  if (n) { out.push(buf.subarray(0, n)); outLen += n; }
}

/** Start recording a note. Throws if the mic is unavailable / denied. */
export async function startNoteRecording(): Promise<void> {
  if (recording) return;
  stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  try {
    const Ctor =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    ctx = new Ctor();
    if (ctx.state === "suspended") await ctx.resume();
    const inRate = ctx.sampleRate;
    // Never upsample: a (rare) sub-16 kHz context is sent at its native rate.
    outRate = inRate > TARGET_RATE ? TARGET_RATE : Math.round(inRate);
    ratio = inRate > TARGET_RATE ? inRate / TARGET_RATE : 1;
    acc = 0; accN = 0; inPos = 0; nextEdge = ratio;
    out = []; outLen = 0;
    source = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = (e) => { if (recording) push(e.inputBuffer.getChannelData(0)); };
    source.connect(proc);
    // Connected to the destination so the callback fires; we never write the
    // output buffer, so it plays silence (no feedback).
    proc.connect(ctx.destination);
  } catch (e) {
    teardown();
    throw e;
  }
  recording = true;
  startedAt = performance.now();
}

function teardown(): void {
  try { if (proc) { proc.disconnect(); proc.onaudioprocess = null; } } catch { /* ignore */ }
  try { if (source) source.disconnect(); } catch { /* ignore */ }
  try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
  try { if (ctx) void ctx.close(); } catch { /* ignore */ }
  proc = null; source = null; stream = null; ctx = null; recording = false;
}

/** Stop and throw the recording away (e.g. the overlay is torn down). */
export function cancelNoteRecording(): void {
  teardown();
  out = []; outLen = 0;
}

/** Stop recording and upload the note. Resolves with the server's verdict. */
export async function stopNoteAndUpload(cfg: HalConfig): Promise<NoteUploadResult> {
  if (!recording) { teardown(); return { ok: false, reason: "empty" }; }
  teardown();
  const pcm = new Int16Array(outLen);
  let off = 0;
  for (const c of out) { pcm.set(c, off); off += c.length; }
  out = []; outLen = 0;
  // Under ~0.3 s there is nothing worth transcribing (an accidental double tap).
  if (pcm.length < outRate * 0.3) return { ok: false, reason: "empty" };

  const headers: Record<string, string> = {
    "Content-Type": "application/octet-stream",
    "X-Sample-Rate": String(outRate),
    "X-Sample-Format": "s16le",   // Int16Array is little-endian on every target CPU
  };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  try {
    const res = await fetch(`${cfg.serverBaseUrl.replace(/\/+$/, "")}/api/satellite/note`, {
      method: "POST", headers, body: pcm.buffer,
    });
    if (res.status === 422) return { ok: false, reason: "no_speech" };
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); if (j && (j.error || j.detail)) msg = String(j.error || j.detail); }
      catch { /* not JSON */ }
      return { ok: false, reason: "error", message: msg };
    }
    return { ok: true, note: (await res.json()) as NoteRow };
  } catch {
    return { ok: false, reason: "error", message: "Couldn't reach the server" };
  }
}
