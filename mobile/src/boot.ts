// App entry. Reads stored config (or runs onboarding), injects HAL_CONFIG for
// the reused kiosk display, mounts the display + input overlay.

import {
  loadConfig, saveConfig, injectHalConfig, resolveActiveBase, type HalConfig,
} from "./config/hal-config";
import { runOnboarding } from "./onboarding/onboarding";
import { mountDisplay } from "./display/inject";
import { mountOverlay } from "./overlay/overlay";
import { observeResponses } from "./overlay/tts-local";
import { mountSatelliteAudio } from "./overlay/satellite-audio";
import { startPhotoFrameIdle } from "./overlay/photo-frame-idle";
import { configurePlatform, hideSplash, onResume } from "./platform/platform";

async function main(): Promise<void> {
  await configurePlatform();

  const stored = await loadConfig();
  const cfg = stored ?? (await runOnboarding());
  if (!stored) await saveConfig(cfg);

  // Pick home-vs-gateway for this session (home preferred; gateway when away).
  // The stored config keeps BOTH bases; we substitute the active one into the
  // session cfg so every surface (input bar, TTS fetch, photo frame, the
  // injected HAL_CONFIG) targets the same reachable host without threading a
  // URL through each mount.
  const active = await resolveActiveBase(cfg);
  console.log(`[hal] using ${active.usingGateway ? "gateway" : "home"} base: ${active.serverBaseUrl}`);
  const sessionCfg: HalConfig = {
    ...cfg, serverBaseUrl: active.serverBaseUrl, wsUrl: active.wsUrl,
  };

  injectHalConfig(cfg, active);
  mountSatelliteAudio(sessionCfg);  // window.HALSatelliteAudio (HAL's server TTS)
  await mountDisplay();   // loads app.js → connects to HAL_CONFIG.wsUrl
  mountOverlay(sessionCfg);      // text + mic input bar
  observeResponses(false); // on-device TTS off (server voice via satellite-audio)
  startPhotoFrameIdle(sessionCfg); // ambient photo frame after the phone goes idle

  // On foreground resume, re-resolve: if home↔gateway reachability flipped
  // (left the house / came back), reload so every surface re-targets the now-
  // correct base. No-op when nothing changed.
  let usingGateway = active.usingGateway;
  onResume(async () => {
    try {
      const next = await resolveActiveBase(cfg);
      if (next.usingGateway !== usingGateway) {
        usingGateway = next.usingGateway;
        console.log(`[hal] base changed → ${next.usingGateway ? "gateway" : "home"}; reloading`);
        location.reload();
      }
    } catch { /* keep current base */ }
  });

  await hideSplash();
}

main().catch((e) => {
  console.error("[hal] boot failed", e);
  void hideSplash();
});
