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
  // Always full-bleed: the theme background paints under the status bar.
  try { await StatusBar.setOverlaysWebView({ overlay: true }); } catch { /* web */ }
  if (!webViewReportsTopInset()) {
    // env(safe-area-inset-top) is 0 here, so publish the real bar height
    // (getInfo().height is already in dp = CSS px) as --hal-inset-top; the
    // top-edge CSS takes max(env(...), var(--hal-inset-top)).
    try {
      const { height, overlays } = await StatusBar.getInfo();
      if (overlays && height > 0) {
        document.documentElement.style.setProperty("--hal-inset-top", `${height}px`);
      }
    } catch { /* web */ }
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
