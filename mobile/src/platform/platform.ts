// Thin wrappers around the device plugins, all best-effort (no-op on web / when
// a plugin is unavailable) so the same code runs in a browser during dev.

import { KeepAwake } from "@capacitor-community/keep-awake";
import { StatusBar, Style } from "@capacitor/status-bar";
import { SplashScreen } from "@capacitor/splash-screen";
import { App } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";

/** Android WebView ≥ 140 maps system-bar insets into env(safe-area-inset-*);
 * older ones report 0 when there's no display cutout — e.g. the Pixel Tablet
 * on Android 14 ships WebView 120 — so the app chrome would collide with the
 * OS clock / battery icons. (140 is the same cutoff Capacitor's SystemBars
 * plugin uses.) iOS/web always report safe areas correctly via env(). */
function webViewReportsTopInset(): boolean {
  if (Capacitor.getPlatform() !== "android") return true;
  const major = parseInt(/Chrome\/(\d+)/.exec(navigator.userAgent)?.[1] ?? "0", 10);
  return major >= 140;
}

export async function configurePlatform(): Promise<void> {
  try { await StatusBar.setStyle({ style: Style.Dark }); } catch { /* web */ }
  // The system status bar stays visible in its OWN space; the WebView lays out
  // BELOW it (overlay OFF). Full-bleed overlay rendered edge-to-edge on Pixel
  // but on MIUI it became a dead black strip the app didn't use — turning
  // overlay off makes the bar occupy its strip and the app take the rest. The
  // bar uses the theme's dark statusBarColor; Style.Dark keeps the icons light.
  try { await StatusBar.setOverlaysWebView({ overlay: false }); } catch { /* web */ }
  // Not overlaying → the WebView already starts below the bar, no top inset.
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
