// The mobile input overlay: a fixed bottom bar (text field + send + mic) that
// floats above the mirrored display without disturbing the orb. It lives in
// #hal-overlay-root (a sibling of the display, NOT inside #orientation-wrapper)
// so it never inherits the orb's transform/perspective.

import { clearConfig, type HalConfig } from "../config/hal-config";
import { sendCommand } from "./command";
import { startListening, stopListening, isListening } from "./mic";
import { unlockAudio } from "./satellite-audio";
import { Haptics, ImpactStyle } from "@capacitor/haptics";

export function mountOverlay(cfg: HalConfig): void {
  const root = document.getElementById("hal-overlay-root");
  if (!root) return;
  root.innerHTML = "";

  const bar = document.createElement("div");
  bar.className = "hal-input-bar";

  const input = document.createElement("input");
  input.className = "hal-input";
  input.type = "text";
  input.placeholder = "Message PAL…";
  input.autocomplete = "off";
  input.autocapitalize = "sentences";
  input.enterKeyHint = "send";

  const mic = document.createElement("button");
  mic.className = "hal-btn hal-mic";
  mic.setAttribute("aria-label", "Voice");
  mic.innerHTML = micIcon();

  const send = document.createElement("button");
  send.className = "hal-btn hal-send";
  send.setAttribute("aria-label", "Send");
  send.innerHTML = sendIcon();

  bar.append(mic, input, send);
  root.appendChild(bar);

  // Settings / re-pair affordance (top-right). Lets you point at a different
  // server or re-pair — clears the stored config and returns to onboarding.
  const gear = document.createElement("button");
  gear.className = "hal-gear";
  gear.setAttribute("aria-label", "Settings");
  gear.innerHTML = gearIcon();
  gear.addEventListener("click", () => showSettings(cfg));
  root.appendChild(gear);

  async function submit() {
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    void haptic();
    unlockAudio();   // this tap is the gesture that lets HAL's TTS autoplay
    await sendCommand(cfg, text);
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); void submit(); }
  });
  send.addEventListener("click", () => void submit());

  // Mic: PUSH-TO-TALK. Hold the button to dictate (partials stream into the
  // field); release to send the final transcript. Pointer capture keeps the
  // release bound to the button even if the finger drifts off it.
  async function startPtt() {
    if (isListening()) return;
    try {
      void haptic();
      unlockAudio();   // mic tap doubles as the autoplay-unlock gesture
      mic.classList.add("active");
      input.value = "";
      input.placeholder = "Listening…";
      await startListening((partial) => { input.value = partial; });
    } catch (e) {
      mic.classList.remove("active");
      input.placeholder = "Message PAL…";
      console.warn("[hal] mic start failed", e);
    }
  }
  async function endPtt() {
    if (!isListening()) return;
    mic.classList.remove("active");
    input.placeholder = "Message PAL…";
    const final = await stopListening();
    if (final) { input.value = final; void submit(); }
  }
  mic.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { mic.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    void startPtt();
  });
  mic.addEventListener("pointerup", (e) => { e.preventDefault(); void endPtt(); });
  mic.addEventListener("pointercancel", () => { void endPtt(); });
}

function showSettings(cfg: HalConfig): void {
  const root = document.getElementById("hal-overlay-root");
  if (!root) return;
  const back = document.createElement("div");
  back.className = "hal-sheet-backdrop";
  back.innerHTML = `
    <div class="hal-sheet">
      <div class="hal-sheet-title">Connection</div>
      <div class="hal-sheet-sub">${cfg.serverName ? cfg.serverName + " · " : ""}${cfg.serverBaseUrl}</div>
      <button class="hal-sheet-btn danger" id="hal-repair">Re-pair / change server</button>
      <button class="hal-sheet-btn" id="hal-cancel">Cancel</button>
    </div>`;
  root.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#hal-cancel")!.addEventListener("click", close);
  back.querySelector("#hal-repair")!.addEventListener("click", async () => {
    await clearConfig();
    location.reload();   // boot.ts re-runs with no config → onboarding (code entry)
  });
}

async function haptic(): Promise<void> {
  try { await Haptics.impact({ style: ImpactStyle.Light }); } catch { /* web/no-op */ }
}

function gearIcon(): string {
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
}

function micIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="11" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`;
}

function sendIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
}
