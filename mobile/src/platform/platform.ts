// Thin wrappers around the device plugins, all best-effort (no-op on web / when
// a plugin is unavailable) so the same code runs in a browser during dev.

import { KeepAwake } from "@capacitor-community/keep-awake";
import { StatusBar, Style } from "@capacitor/status-bar";
import { SplashScreen } from "@capacitor/splash-screen";
import { App } from "@capacitor/app";

export async function configurePlatform(): Promise<void> {
  try { await StatusBar.setStyle({ style: Style.Dark }); } catch { /* web */ }
  try { await StatusBar.setOverlaysWebView({ overlay: true }); } catch { /* web */ }
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
