// First-run onboarding: (1) server URL, (2) pairing code → token. Resolves with
// a HalConfig the caller persists. Demo URL prefilled (hosted demo server).

import { type HalConfig } from "../config/hal-config";
import { DEFAULT_SERVER_BASE_URL, isDemoUrl } from "../config/demo-config";
import { checkServer, redeemCode } from "./pairing";
import { discoverServer } from "./discover";

function deviceName(): string {
  const ua = navigator.userAgent;
  if (/iPad/.test(ua)) return "iPad";
  if (/iPhone/.test(ua)) return "iPhone";
  if (/Android/.test(ua)) return "Android device";   // covers phones AND tablets
  return "device";
}

export function runOnboarding(): Promise<HalConfig> {
  return new Promise((resolve) => {
    const root = document.getElementById("hal-onboarding-root")!;
    root.classList.add("visible");

    const card = document.createElement("div");
    card.className = "hal-ob-card";
    root.innerHTML = "";
    root.appendChild(card);

    let serverBaseUrl = "";

    const stepUrl = () => {
      card.innerHTML = `
        <h1 class="hal-ob-title">Connect to PAL</h1>
        <p class="hal-ob-sub">Enter your PAL server address.</p>
        <input class="hal-ob-input" id="ob-url" type="url" inputmode="url"
               autocapitalize="off" autocorrect="off" spellcheck="false"
               value="${DEFAULT_SERVER_BASE_URL}" placeholder="http://10.20.30.185:8765" />
        <div class="hal-ob-found" id="ob-found" hidden></div>
        <div class="hal-ob-err" id="ob-err"></div>
        <button class="hal-ob-btn" id="ob-next">Continue</button>`;
      const urlEl = card.querySelector<HTMLInputElement>("#ob-url")!;
      const errEl = card.querySelector<HTMLDivElement>("#ob-err")!;
      const foundEl = card.querySelector<HTMLDivElement>("#ob-found")!;
      const nextEl = card.querySelector<HTMLButtonElement>("#ob-next")!;

      // Best-effort LAN autodiscovery: while the user reads the screen, browse
      // for the ai-server's mDNS advert and, IF the field is still the untouched
      // default, prefill it + offer a one-tap "Found …" suggestion. Never clobber
      // anything the user has started typing; silent no-op off-network/on web.
      let userEdited = false;
      urlEl.addEventListener("input", () => { userEdited = true; foundEl.hidden = true; });
      foundEl.textContent = "Searching for PAL on your network…";
      foundEl.hidden = false;
      void discoverServer().then((found) => {
        if (!found) { foundEl.hidden = true; return; }
        const untouched = !userEdited && urlEl.value.trim() === DEFAULT_SERVER_BASE_URL;
        if (untouched) urlEl.value = found.url;
        foundEl.innerHTML = "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hal-ob-found-chip";
        btn.textContent = `Found ${found.name} at ${found.url}`;
        btn.addEventListener("click", () => { urlEl.value = found.url; userEdited = true; });
        foundEl.appendChild(btn);
        foundEl.hidden = false;
      });

      nextEl.addEventListener("click", async () => {
        const raw = urlEl.value.trim().replace(/\/+$/, "");
        if (!/^https?:\/\//.test(raw)) { errEl.textContent = "Start with http:// or https://"; return; }
        nextEl.disabled = true; errEl.textContent = "Checking…";
        const reachable = await checkServer(raw);
        nextEl.disabled = false;
        if (!reachable && !isDemoUrl(raw)) { errEl.textContent = "Couldn't reach that server."; return; }
        serverBaseUrl = raw;
        stepCode();
      });
    };

    const stepCode = () => {
      card.innerHTML = `
        <h1 class="hal-ob-title">Pair your device</h1>
        <p class="hal-ob-sub">On your PAL display, ask to pair a device, then enter the 6-digit code shown.</p>
        <input class="hal-ob-input hal-ob-code" id="ob-code" type="text" inputmode="numeric"
               maxlength="6" autocomplete="one-time-code" placeholder="------" />
        <div class="hal-ob-err" id="ob-err"></div>
        <button class="hal-ob-btn" id="ob-pair">Pair</button>
        <button class="hal-ob-link" id="ob-back">Back</button>`;
      const codeEl = card.querySelector<HTMLInputElement>("#ob-code")!;
      const errEl = card.querySelector<HTMLDivElement>("#ob-err")!;
      const pairEl = card.querySelector<HTMLButtonElement>("#ob-pair")!;
      card.querySelector<HTMLButtonElement>("#ob-back")!.addEventListener("click", stepUrl);
      codeEl.focus();
      pairEl.addEventListener("click", async () => {
        const code = codeEl.value.trim();
        if (!/^\d{6}$/.test(code)) { errEl.textContent = "Enter the 6-digit code."; return; }
        pairEl.disabled = true; errEl.textContent = "Pairing…";
        const r = await redeemCode(serverBaseUrl, code, deviceName());
        pairEl.disabled = false;
        if (!r.ok || !r.config) { errEl.textContent = r.error || "Pairing failed."; return; }
        root.classList.remove("visible");
        root.innerHTML = "";
        resolve(r.config);
      });
    };

    stepUrl();
  });
}
