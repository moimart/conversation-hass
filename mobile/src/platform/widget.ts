// Bridge to the Android home-screen widget (PalWidgetPlugin.java +
// NotesWidgetProvider.java): the latest voice notes. The widget fetches on its
// own, so the app hands it the home + gateway bases and the token (stored
// natively, Keystore-encrypted), pokes it after a note is recorded, and opens
// the Notes view when the app was launched from it. Android only; no-ops
// elsewhere and on builds without the plugin.

import { registerPlugin, Capacitor } from "@capacitor/core";
import type { HalConfig } from "../config/hal-config";

interface PalWidgetPlugin {
  configure(opts: { home: string; gateway: string; token: string }): Promise<void>;
  clear(): Promise<void>;
  refresh(): Promise<void>;
  takeLaunchAction(): Promise<{ action: string }>;
  addListener(ev: "launchAction", cb: (e: { action: string }) => void): Promise<unknown>;
}

const PalWidget = registerPlugin<PalWidgetPlugin>("PalWidget");
const enabled = Capacitor.getPlatform() === "android";

export function configureWidget(cfg: HalConfig): void {
  if (!enabled) return;
  PalWidget.configure({
    home: cfg.serverBaseUrl || "", gateway: cfg.gatewayBaseUrl || "", token: cfg.token || "",
  }).catch(() => {});
}

export function clearWidget(): void {
  if (enabled) PalWidget.clear().catch(() => {});
}

export function refreshWidget(): void {
  if (enabled) PalWidget.refresh().catch(() => {});
}

/** Run `open` when the app is (or was just) opened from the widget. */
export function onWidgetLaunch(open: (action: string) => void): void {
  if (!enabled) return;
  PalWidget.takeLaunchAction().then((r) => { if (r && r.action) open(r.action); }).catch(() => {});
  PalWidget.addListener("launchAction", (e) => { if (e && e.action) open(e.action); }).catch(() => {});
}
