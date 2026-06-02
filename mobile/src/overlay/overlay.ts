// The mobile input overlay: a fixed bottom bar (text field + send + mic) that
// floats above the mirrored display without disturbing the orb. It lives in
// #hal-overlay-root (a sibling of the display, NOT inside #orientation-wrapper)
// so it never inherits the orb's transform/perspective.

import type { HalConfig } from "../config/hal-config";
import { sendCommand } from "./command";
import { startListening, stopListening, isListening } from "./mic";
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
  input.placeholder = "Message HAL…";
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

  async function submit() {
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    void haptic();
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
      mic.classList.add("active");
      input.value = "";
      input.placeholder = "Listening…";
      await startListening((partial) => { input.value = partial; });
    } catch (e) {
      mic.classList.remove("active");
      input.placeholder = "Message HAL…";
      console.warn("[hal] mic start failed", e);
    }
  }
  async function endPtt() {
    if (!isListening()) return;
    mic.classList.remove("active");
    input.placeholder = "Message HAL…";
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

async function haptic(): Promise<void> {
  try { await Haptics.impact({ style: ImpactStyle.Light }); } catch { /* web/no-op */ }
}

function micIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="11" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`;
}

function sendIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
}
