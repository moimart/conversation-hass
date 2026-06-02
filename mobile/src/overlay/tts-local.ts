// Optional on-device TTS of HAL's responses. DEFAULT OFF: the RPi speaker
// already voices responses, so speaking on the phone too would double up. When
// enabled (a future setting), we watch the mirrored display's response text and
// speak new responses locally — useful when away from the RPi.

import { TextToSpeech } from "@capacitor-community/text-to-speech";

let observer: MutationObserver | null = null;

export async function speak(text: string): Promise<void> {
  const t = text.trim();
  if (!t) return;
  try {
    await TextToSpeech.speak({ text: t, lang: "en-US", rate: 1.0 });
  } catch (e) {
    console.warn("[hal] local TTS failed", e);
  }
}

/** Speak each new #response-text the display renders. enabled=false → no-op. */
export function observeResponses(enabled: boolean): void {
  if (observer) { observer.disconnect(); observer = null; }
  if (!enabled) return;
  const el = document.getElementById("response-text");
  if (!el) return;
  let last = "";
  observer = new MutationObserver(() => {
    const txt = el.textContent || "";
    if (txt && txt !== last) { last = txt; void speak(txt); }
  });
  observer.observe(el, { childList: true, characterData: true, subtree: true });
}
