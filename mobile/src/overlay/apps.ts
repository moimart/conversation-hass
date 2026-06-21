// Detect + launch other installed apps (Android-only, via the PalApps native
// plugin). Used to gate the Home Assistant launcher shortcut in the top-right
// cluster: show it only when the HA companion app is actually installed, the
// same way mirror.ts gates the mirror button on hasFrontCamera().
//
// Off Android (iOS/web) hasHomeAssistant() resolves false, so the button never
// appears there — an iOS version would need the homeassistant:// URL scheme.

import { registerPlugin, Capacitor } from "@capacitor/core";

interface PalAppsPlugin {
  isInstalled(opts: { packageName: string }): Promise<{ installed: boolean }>;
  openApp(opts: { packageName: string }): Promise<void>;
}
const PalApps = registerPlugin<PalAppsPlugin>("PalApps");

// Full (Play) + minimal (F-Droid) companion builds.
const HA_PACKAGES = [
  "io.homeassistant.companion.android",
  "io.homeassistant.companion.android.minimal",
];

let cachedHaPackage: string | null | undefined;   // undefined = not checked, null = absent

/** Is the Home Assistant companion app installed? Resolves the package name into
 *  a cached boolean; false (and no-op) off Android. Never throws. */
export async function hasHomeAssistant(): Promise<boolean> {
  if (cachedHaPackage !== undefined) return cachedHaPackage !== null;
  if (!Capacitor.isNativePlatform()) { cachedHaPackage = null; return false; }
  for (const pkg of HA_PACKAGES) {
    try {
      if ((await PalApps.isInstalled({ packageName: pkg })).installed) {
        cachedHaPackage = pkg;
        return true;
      }
    } catch {
      /* plugin unimplemented (non-Android) — treat as absent */
    }
  }
  cachedHaPackage = null;
  return false;
}

/** Foreground the Home Assistant app (no-op if not found). */
export async function openHomeAssistant(): Promise<void> {
  if (cachedHaPackage === undefined) await hasHomeAssistant();
  const pkg = cachedHaPackage;
  if (!pkg) return;
  try { await PalApps.openApp({ packageName: pkg }); } catch { /* ignore */ }
}
