// Thin wrappers around the device plugins, all best-effort (no-op on web / when
// a plugin is unavailable) so the same code runs in a browser during dev.

import { KeepAwake } from "@capacitor-community/keep-awake";
import { StatusBar, Style } from "@capacitor/status-bar";
import { SplashScreen } from "@capacitor/splash-screen";
import { App } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";

/** Whether drawing under the Android status bar is safe: WebView ≥ 140 maps
 * system-bar insets into env(safe-area-inset-*); older ones report 0 (no
 * cutout = no inset), so the app chrome would collide with the OS clock /
 * battery icons — e.g. the Pixel Tablet on Android 14 ships WebView 120.
 * (140 is the same cutoff Capacitor's SystemBars plugin uses.) iOS always
 * reports safe areas, so overlay is always fine there. */
function canOverlayStatusBar(): boolean {
  if (Capacitor.getPlatform() !== "android") return true;
  const major = parseInt(/Chrome\/(\d+)/.exec(navigator.userAgent)?.[1] ?? "0", 10);
  return major >= 140;
}

export async function configurePlatform(): Promise<void> {
  try { await StatusBar.setStyle({ style: Style.Dark }); } catch { /* web */ }
  if (canOverlayStatusBar()) {
    try { await StatusBar.setOverlaysWebView({ overlay: true }); } catch { /* web */ }
  } else {
    // Classic opaque status bar: the WebView starts below it, env() = 0 is
    // then CORRECT and all the safe-area CSS degrades cleanly.
    try { await StatusBar.setOverlaysWebView({ overlay: false }); } catch { /* web */ }
    try { await StatusBar.setBackgroundColor({ color: "#0a0c10" }); } catch { /* web */ }
  }
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
