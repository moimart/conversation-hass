// The "mirror": a fullscreen front-camera view that turns the phone into a
// mirror. Opened from the top-right control cluster (overlay.ts). Mobile-only —
// it lives entirely here, not in the kiosk-shared rpi/web.
//
// The feed is a plain web <video> fed by getUserMedia({facingMode:'user'}),
// horizontally flipped in CSS so it behaves like a real mirror. Android grants
// the WebView camera at runtime via Capacitor's BridgeWebChromeClient; iOS needs
// the NSCameraUsageDescription string + a WKUIDelegate grant (MainViewController).
//
// Whether the button is shown at all is gated by hasFrontCamera(), which asks a
// tiny native plugin (PalMirror) for a definitive, prompt-free answer and falls
// back to a web enumerateDevices() probe off-device.

import { registerPlugin, Capacitor } from "@capacitor/core";
import { App } from "@capacitor/app";
import { Haptics, ImpactStyle } from "@capacitor/haptics";
import { type HalConfig } from "../config/hal-config";

interface PalMirrorPlugin {
  hasFrontCamera(): Promise<{ present: boolean }>;
}
const PalMirror = registerPlugin<PalMirrorPlugin>("PalMirror");

let stage: HTMLElement | null = null;
let stream: MediaStream | null = null;
let cachedHasCam: boolean | null = null;
let appStateHandle: { remove: () => void } | null = null;
let cfg: HalConfig | null = null;

async function haptic(): Promise<void> {
  try { await Haptics.impact({ style: ImpactStyle.Light }); } catch { /* web/no-op */ }
}

const onVisibility = (): void => { if (document.hidden) void closeMirror(); };

/** Does this device have a front camera? Native answer when available (no
 *  prompt, no capture), web enumerateDevices fallback otherwise. Cached. */
export async function hasFrontCamera(): Promise<boolean> {
  if (cachedHasCam !== null) return cachedHasCam;
  if (Capacitor.isNativePlatform()) {
    try {
      cachedHasCam = (await PalMirror.hasFrontCamera()).present;
      return cachedHasCam;
    } catch {
      /* plugin unimplemented (shouldn't happen on device) — fall through */
    }
  }
  try {
    const md = navigator.mediaDevices;
    if (!md?.enumerateDevices) { cachedHasCam = false; return false; }
    const devices = await md.enumerateDevices();
    cachedHasCam = devices.some((d) => d.kind === "videoinput");
  } catch {
    cachedHasCam = false;
  }
  return cachedHasCam;
}

/** Open the fullscreen mirror. Idempotent — a second tap while open is a no-op.
 *  `serverCfg` is the paired-server config used to broadcast a captured still. */
export async function mountMirror(serverCfg?: HalConfig): Promise<void> {
  if (stage) return;
  cfg = serverCfg ?? null;
  const root = document.getElementById("hal-overlay-root");
  if (!root) return;

  stage = document.createElement("div");
  stage.className = "mirror-stage";

  const video = document.createElement("video");
  video.className = "mirror-video";
  video.setAttribute("playsinline", "");   // inline playback in WKWebView
  video.muted = true;
  video.autoplay = true;

  const close = document.createElement("button");
  close.className = "mirror-close";
  close.setAttribute("aria-label", "Close mirror");
  close.innerHTML = closeIcon();
  close.addEventListener("click", () => void closeMirror());

  // Shutter: hidden until the stream is live (an error path has no usable frame).
  const shutter = document.createElement("button");
  shutter.className = "mirror-shutter";
  shutter.setAttribute("aria-label", "Take photo");
  shutter.hidden = true;
  shutter.addEventListener("click", () => { void haptic(); captureAndConfirm(video); });

  stage.append(video, close, shutter);
  root.appendChild(stage);
  document.body.classList.add("show-mirror");

  // Free the camera whenever we lose the foreground (tab hidden or app
  // backgrounded), rather than holding it live behind the lock screen.
  document.addEventListener("visibilitychange", onVisibility);
  try {
    appStateHandle = await App.addListener("appStateChange", ({ isActive }) => {
      if (!isActive) void closeMirror();
    });
  } catch {
    /* web / no native App plugin */
  }

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user" },
      audio: false,
    });
    video.srcObject = stream;
    // iOS 15+ WKWebView sometimes needs a tick before play() renders frames.
    try { await video.play(); } catch { setTimeout(() => { void video.play(); }, 0); }
    if (stream.getVideoTracks()[0]?.getSettings().facingMode) cachedHasCam = true;
    shutter.hidden = false;   // stream live — allow capture
  } catch (e) {
    showError(stage, errorMessage(e));
    if (e instanceof DOMException && (e.name === "NotFoundError" || e.name === "OverconstrainedError")) {
      cachedHasCam = false;   // no usable camera — button self-hides next mount
    }
  }
}

/** Close the mirror and release the camera. Single cleanup choke-point: every
 *  exit path (close button, background, error) routes through here. */
export async function closeMirror(): Promise<void> {
  if (!stage) return;
  document.removeEventListener("visibilitychange", onVisibility);
  if (appStateHandle) { try { appStateHandle.remove(); } catch { /* ignore */ } appStateHandle = null; }
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  stage.remove();
  stage = null;
  document.body.classList.remove("show-mirror");
}

/** Grab the current frame, mirrored to match the preview + downscaled, and open
 *  the broadcast-confirm sheet. */
function captureAndConfirm(video: HTMLVideoElement): void {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return;
  const scale = Math.min(1, 1280 / Math.max(vw, vh));   // cap longest edge
  const w = Math.round(vw * scale), h = Math.round(vh * scale);
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.translate(w, 0); ctx.scale(-1, 1);   // flip → matches the mirrored preview
  ctx.drawImage(video, 0, 0, w, h);
  let dataUrl: string;
  try { dataUrl = canvas.toDataURL("image/jpeg", 0.9); }
  catch { return; }   // tainted canvas — shouldn't happen for the device camera
  showConfirmSheet(dataUrl);
}

/** Preview + "Broadcast to home" / "Retake". The live mirror keeps running
 *  underneath, so Retake just dismisses the sheet. */
function showConfirmSheet(dataUrl: string): void {
  const root = document.getElementById("hal-overlay-root");
  if (!root) return;
  const back = document.createElement("div");
  back.className = "hal-sheet-backdrop";
  back.innerHTML = `
    <div class="hal-sheet">
      <div class="hal-sheet-title">Broadcast photo</div>
      <img class="mirror-shot-preview" alt="captured photo" />
      <div class="hal-sheet-sub" id="mirror-shot-status">Show this on every screen at home?</div>
      <button class="hal-sheet-btn" id="mirror-shot-send">Broadcast to home</button>
      <button class="hal-sheet-btn" id="mirror-shot-retake">Retake</button>
    </div>`;
  (back.querySelector(".mirror-shot-preview") as HTMLImageElement).src = dataUrl;
  root.appendChild(back);
  const close = (): void => back.remove();
  const send = back.querySelector("#mirror-shot-send") as HTMLButtonElement;
  const retake = back.querySelector("#mirror-shot-retake") as HTMLButtonElement;
  const status = back.querySelector("#mirror-shot-status") as HTMLElement;
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  retake.addEventListener("click", close);
  send.addEventListener("click", async () => {
    send.disabled = true; retake.disabled = true;
    status.textContent = "Sending…";
    try {
      await broadcastShot(dataUrl);
      void haptic();
      status.textContent = "Sent ✓";
      setTimeout(close, 700);
    } catch {
      status.textContent = "Couldn't broadcast — try again.";
      send.disabled = false; retake.disabled = false;
    }
  });
}

/** POST the captured JPEG to the paired server for household broadcast. */
async function broadcastShot(dataUrl: string): Promise<void> {
  if (!cfg) throw new Error("no server config");
  const blob = await (await fetch(dataUrl)).blob();
  const headers: Record<string, string> = { "Content-Type": "image/jpeg" };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  const res = await fetch(`${cfg.serverBaseUrl}/api/satellite/photo-broadcast`, {
    method: "POST", headers, body: blob,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

function errorMessage(e: unknown): string {
  const name = e instanceof DOMException ? e.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return "Camera access denied.";
  if (name === "NotReadableError") return "Camera unavailable — it may be in use by another app.";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "No front camera found.";
  return "Couldn't start the camera.";
}

function showError(host: HTMLElement, text: string): void {
  const msg = document.createElement("div");
  msg.className = "mirror-error";
  msg.textContent = text;
  host.insertBefore(msg, host.firstChild);
}

function closeIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
}
