// Thin wrappers around the device plugins, all best-effort (no-op on web / when
// a plugin is unavailable) so the same code runs in a browser during dev.

import { KeepAwake } from "@capacitor-community/keep-awake";
import { SplashScreen } from "@capacitor/splash-screen";
import { App } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";

export async function configurePlatform(): Promise<void> {
  // Fullscreen is owned NATIVELY (immersive sticky in MainActivity on Android,
  // which hides BOTH system bars and draws into the cutout — like a game). The
  // Capacitor status-bar plugin can only recolor/inset the bars, never remove
  // them, so it's not used here. iOS uses its own status-bar handling. Just
  // clear the top inset (no bar to clear) and keep the screen awake.
  void Capacitor;
  document.documentElement.style.setProperty("--hal-inset-top", "0px");
  try { await KeepAwake.keepAwake(); } catch { /* web */ }
}

export async function hideSplash(): Promise<void> {
  try { await SplashScreen.hide(); } catch { /* web */ }
}

/** Run `cb` whenever the app returns to the foreground (to reconnect the WS). */
export function onResume(cb: () => void): void {
  try {
    App.addListener("appStateChange", ({ isActive }) => { if (isActive) cb(); });
  } catch { /* web */ }
}
