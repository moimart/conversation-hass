// The mobile input overlay: a fixed bottom bar (text field + send + mic) that
// floats above the mirrored display without disturbing the orb. It lives in
// #hal-overlay-root (a sibling of the display, NOT inside #orientation-wrapper)
// so it never inherits the orb's transform/perspective.

import { clearConfig, type HalConfig } from "../config/hal-config";
import { sendCommand } from "./command";
import { startListening, stopListening, isListening } from "./mic";
import { unlockAudio } from "./satellite-audio";
import { mountMirror, hasFrontCamera } from "./mirror";
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

  // Conversation log (next to the gear). Opens the full-screen history view
  // locally — app.js exposes window.HALConversationLog; no auto-dismiss on
  // mobile (the view shows its own ✕ button).
  const logBtn = document.createElement("button");
  logBtn.className = "hal-gear hal-log";
  logBtn.setAttribute("aria-label", "Conversation log");
  logBtn.innerHTML = logIcon();
  logBtn.addEventListener("click", () => {
    const getApi = () => (window as unknown as {
      HALConversationLog?: { open: () => void; close: () => void };
    }).HALConversationLog;
    const api = getApi();
    if (api) {
      if (document.body.classList.contains("show-conversation-log")) api.close();
      else api.open();
    } else {
      // app.js may still be booting right after launch — try again shortly
      // instead of silently doing nothing.
      setTimeout(() => getApi()?.open(), 800);
    }
  });
  root.appendChild(logBtn);

  // Mirror (front camera) — sits left of the conversation-log button. Created
  // hidden; revealed only once hasFrontCamera() confirms the device has one, so
  // a camera-less device never shows a dead button.
  const mirrorBtn = document.createElement("button");
  mirrorBtn.className = "hal-gear hal-mirror";
  mirrorBtn.setAttribute("aria-label", "Mirror");
  mirrorBtn.hidden = true;
  mirrorBtn.innerHTML = mirrorIcon();
  mirrorBtn.addEventListener("click", () => { void haptic(); void mountMirror(); });
  root.appendChild(mirrorBtn);
  void hasFrontCamera().then((ok) => { mirrorBtn.hidden = !ok; });

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
      <div class="hal-sheet-title">This device</div>
      <div class="hal-sheet-namerow">
        <input class="hal-name-input" id="hal-name-input" type="text" maxlength="64"
               placeholder="Device name (e.g. Kitchen)" autocomplete="off" autocapitalize="words" />
        <button class="hal-sheet-btn hal-name-save" id="hal-name-save">Save</button>
      </div>
      <div class="hal-sheet-row-sub" id="hal-name-hint">Used when someone calls this device.</div>
      <div class="hal-sheet-title" style="margin-top:14px">Connection</div>
      <div class="hal-sheet-sub">${cfg.serverName ? cfg.serverName + " · " : ""}${cfg.serverBaseUrl}</div>
      <div class="hal-sheet-row" id="hal-cloud-row" hidden>
        <div class="hal-sheet-row-text">
          <div class="hal-sheet-row-label">Cloud LLM</div>
          <div class="hal-sheet-row-sub" id="hal-cloud-model"></div>
        </div>
        <button class="hal-switch" id="hal-cloud-toggle" role="switch" aria-checked="false" aria-label="Cloud LLM"></button>
      </div>
      <div class="hal-sheet-title" style="margin-top:14px">Theme</div>
      <div class="hal-sheet-row">
        <div class="hal-sheet-row-text">
          <div class="hal-sheet-row-label">Follow hub theme</div>
          <div class="hal-sheet-row-sub" id="hal-theme-mode-sub">This device matches the hub.</div>
        </div>
        <button class="hal-switch on" id="hal-theme-follow" role="switch" aria-checked="true" aria-label="Follow hub theme"></button>
      </div>
      <div id="hal-theme-local" hidden>
        <div class="hal-sheet-row">
          <div class="hal-sheet-row-text"><div class="hal-sheet-row-label">Day theme</div></div>
          <select class="hal-sheet-select" id="hal-theme-day"></select>
        </div>
        <div class="hal-sheet-row">
          <div class="hal-sheet-row-text"><div class="hal-sheet-row-label">Night theme</div></div>
          <select class="hal-sheet-select" id="hal-theme-night"></select>
        </div>
        <div class="hal-sheet-row">
          <div class="hal-sheet-row-text">
            <div class="hal-sheet-row-label">Follow system dark mode</div>
            <div class="hal-sheet-row-sub">Switch day/night with the OS. Off = always day.</div>
          </div>
          <button class="hal-switch" id="hal-theme-os" role="switch" aria-checked="false" aria-label="Follow system dark mode"></button>
        </div>
      </div>
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
  void mountDeviceRename(cfg, back);
  void mountCloudToggle(cfg, back);
  mountDeviceTheme(back);
}

// Per-device theme: follow the hub (default) or pick a local day/night theme
// that can track the OS dark-mode setting (off ⇒ always day). Lives entirely in
// the display WebView via window.HALThemeLocal (exposed by app.js) — device-
// local, no server, no effect on the hub or other devices.
type ThemeLocalApi = {
  get(): { mode: string; day: string; night: string; followOs: boolean };
  getThemes(): Array<{ name: string; display_name?: string }>;
  set(partial: Record<string, unknown>): unknown;
};
function mountDeviceTheme(back: HTMLElement): void {
  const api = (window as unknown as { HALThemeLocal?: ThemeLocalApi }).HALThemeLocal;
  if (!api) return;   // app.js not ready (or not a satellite)
  const followSw = back.querySelector<HTMLButtonElement>("#hal-theme-follow");
  const localBox = back.querySelector<HTMLElement>("#hal-theme-local");
  const daySel = back.querySelector<HTMLSelectElement>("#hal-theme-day");
  const nightSel = back.querySelector<HTMLSelectElement>("#hal-theme-night");
  const osSw = back.querySelector<HTMLButtonElement>("#hal-theme-os");
  const modeSub = back.querySelector<HTMLElement>("#hal-theme-mode-sub");
  if (!followSw || !localBox || !daySel || !nightSel || !osSw) return;

  const cfg = api.get();
  let themes = api.getThemes() || [];
  if (themes.length === 0) {
    themes = [{ name: cfg.day, display_name: cfg.day }, { name: cfg.night, display_name: cfg.night }];
  }
  const fill = (sel: HTMLSelectElement, current: string) => {
    sel.innerHTML = "";
    for (const t of themes) {
      const o = document.createElement("option");
      o.value = t.name; o.textContent = t.display_name || t.name;
      if (t.name === current) o.selected = true;
      sel.appendChild(o);
    }
  };
  const setSwitch = (el: HTMLButtonElement, on: boolean) => {
    el.classList.toggle("on", on); el.setAttribute("aria-checked", String(on));
  };
  const render = () => {
    const c = api.get();
    const follow = c.mode !== "local";
    setSwitch(followSw, follow);
    setSwitch(osSw, !!c.followOs);
    localBox.hidden = follow;
    if (modeSub) modeSub.textContent = follow
      ? "This device matches the hub."
      : "This device uses its own theme.";
  };
  fill(daySel, cfg.day);
  fill(nightSel, cfg.night);
  render();

  followSw.addEventListener("click", () => {
    // Clicking flips it: currently following → switch to local, and vice-versa.
    const following = followSw.classList.contains("on");
    api.set({ mode: following ? "local" : "global" });
    render();
  });
  osSw.addEventListener("click", () => {
    api.set({ followOs: !osSw.classList.contains("on") });
    render();
  });
  daySel.addEventListener("change", () => api.set({ day: daySel.value }));
  nightSel.addEventListener("change", () => api.set({ night: nightSel.value }));
}

// Self-rename: each device names itself (authenticated by its own token). iOS
// reports "iPhone" for every device, so the intercom directory + voice calls
// ("call the kitchen") need user-chosen names to tell devices apart.
async function mountDeviceRename(cfg: HalConfig, back: HTMLElement): Promise<void> {
  const input = back.querySelector<HTMLInputElement>("#hal-name-input");
  const save = back.querySelector<HTMLButtonElement>("#hal-name-save");
  const hint = back.querySelector<HTMLElement>("#hal-name-hint");
  if (!input || !save) return;
  const headers = {
    "Content-Type": "application/json",
    ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
  };
  // Prefill with the current name (pair/status now returns device_name).
  try {
    const res = await fetch(`${cfg.serverBaseUrl}/api/pair/status`, { headers });
    if (res.ok) {
      const s = await res.json();
      if (s.device_name) input.value = s.device_name;
    }
  } catch { /* offline — leave blank */ }
  save.addEventListener("click", async () => {
    const name = input.value.trim();
    if (!name) return;
    save.disabled = true;
    try {
      const res = await fetch(`${cfg.serverBaseUrl}/api/pair/rename`, {
        method: "POST", headers, body: JSON.stringify({ name }),
      });
      if (hint) hint.textContent = res.ok ? `Saved — this device is now “${name}”.`
                                          : "Couldn't save the name.";
    } catch {
      if (hint) hint.textContent = "Couldn't reach the server.";
    } finally {
      save.disabled = false;
    }
  });
}

// "Cloud LLM" switch (the server calls it the cloud override). Only shown when
// the server reports a configured provider (`available`) — most installs are
// fully local and shouldn't see a dead toggle. State lives server-side; the
// switch reflects whatever the server answers (HA/MQTT stay in sync because
// POST /api/cloud_llm dispatches through the same config callbacks).
async function mountCloudToggle(cfg: HalConfig, back: HTMLElement): Promise<void> {
  const row = back.querySelector<HTMLElement>("#hal-cloud-row");
  const toggle = back.querySelector<HTMLButtonElement>("#hal-cloud-toggle");
  const modelEl = back.querySelector<HTMLElement>("#hal-cloud-model");
  if (!row || !toggle || !modelEl) return;
  const headers = {
    "Content-Type": "application/json",
    ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
  };
  const render = (s: { enabled: boolean; model: string }) => {
    toggle.classList.toggle("on", s.enabled);
    toggle.setAttribute("aria-checked", String(s.enabled));
    modelEl.textContent = s.model || "";
  };
  try {
    const res = await fetch(`${cfg.serverBaseUrl}/api/cloud_llm`, { headers });
    if (!res.ok) return;
    const status = await res.json();
    if (!status.available) return;   // no provider configured — keep hidden
    render(status);
    row.hidden = false;
  } catch {
    return;   // unreachable server: settings stays connection-only
  }
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      const res = await fetch(`${cfg.serverBaseUrl}/api/cloud_llm`, {
        method: "POST",
        headers,
        body: JSON.stringify({ enabled: !toggle.classList.contains("on") }),
      });
      if (res.ok) render(await res.json());
    } catch (e) {
      console.warn("[hal] cloud llm toggle failed", e);
    } finally {
      toggle.disabled = false;
    }
  });
}

async function haptic(): Promise<void> {
  try { await Haptics.impact({ style: ImpactStyle.Light }); } catch { /* web/no-op */ }
}

function gearIcon(): string {
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
}

function logIcon(): string {
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>`;
}

function mirrorIcon(): string {
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`;
}

function micIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="11" rx="3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`;
}

function sendIcon(): string {
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
}
