// App entry. Reads stored config (or runs onboarding), injects HAL_CONFIG for
// the reused kiosk display, mounts the display + input overlay.

import { loadConfig, saveConfig, injectHalConfig } from "./config/hal-config";
import { runOnboarding } from "./onboarding/onboarding";
import { mountDisplay } from "./display/inject";
import { mountOverlay } from "./overlay/overlay";
import { observeResponses } from "./overlay/tts-local";
import { configurePlatform, hideSplash } from "./platform/platform";

async function main(): Promise<void> {
  await configurePlatform();

  let cfg = await loadConfig();
  if (!cfg) {
    cfg = await runOnboarding();
    await saveConfig(cfg);
  }

  injectHalConfig(cfg);
  await mountDisplay();   // loads app.js → connects to HAL_CONFIG.wsUrl
  mountOverlay(cfg);      // text + mic input bar
  observeResponses(false); // on-device TTS off by default (RPi voices responses)

  await hideSplash();
}

main().catch((e) => {
  console.error("[hal] boot failed", e);
  void hideSplash();
});
