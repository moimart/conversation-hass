// On-device speech-to-text via @capacitor-community/speech-recognition
// (iOS Speech framework / Android SpeechRecognizer). Normalizes the two
// platforms behind a tiny session API: start(onPartial) streams interim text;
// stop() resolves the final transcript (the last partial we saw).
//
// We avoid the native popup and drive our own overlay UI, so partials render in
// the text field and the user can edit before sending.

import { SpeechRecognition } from "@capacitor-community/speech-recognition";
import type { PluginListenerHandle } from "@capacitor/core";

let listening = false;
let lastPartial = "";
let partialHandle: PluginListenerHandle | null = null;
let stateHandle: PluginListenerHandle | null = null;

export function isListening(): boolean {
  return listening;
}

export async function ensurePermission(): Promise<boolean> {
  try {
    const avail = await SpeechRecognition.available();
    if (!avail.available) return false;
    let perm = await SpeechRecognition.checkPermissions();
    if (perm.speechRecognition !== "granted") {
      perm = await SpeechRecognition.requestPermissions();
    }
    return perm.speechRecognition === "granted";
  } catch (e) {
    console.warn("[hal] speech availability/permission check failed", e);
    return false;
  }
}

export async function startListening(onPartial: (t: string) => void): Promise<void> {
  if (listening) return;
  if (!(await ensurePermission())) throw new Error("speech permission denied");
  lastPartial = "";
  listening = true;

  partialHandle = await SpeechRecognition.addListener("partialResults", (data: { matches?: string[] }) => {
    const t = data.matches && data.matches[0];
    if (typeof t === "string") {
      lastPartial = t;
      onPartial(t);
    }
  });
  // iOS emits a listeningState change when it stops on its own (silence).
  stateHandle = await SpeechRecognition.addListener("listeningState", (data: { status?: string }) => {
    if (data.status === "stopped") listening = false;
  });

  await SpeechRecognition.start({
    language: "en-US",
    partialResults: true,
    popup: false,
  });
}

export async function stopListening(): Promise<string> {
  if (!listening && !lastPartial) return "";
  try {
    await SpeechRecognition.stop();
  } catch (e) {
    console.warn("[hal] speech stop failed", e);
  }
  listening = false;
  if (partialHandle) { await partialHandle.remove(); partialHandle = null; }
  if (stateHandle) { await stateHandle.remove(); stateHandle = null; }
  const final = lastPartial.trim();
  lastPartial = "";
  return final;
}
