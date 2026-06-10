import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "sh.martinez.pal.companion",
  appName: "PAL",
  webDir: "www",
  // The display connects to the AI server over ws:// / http:// on the LAN, so
  // Android must allow cleartext. iOS needs an ATS exception in Info.plist
  // (NSAllowsLocalNetworking) — applied in the native project, see README.
  server: {
    androidScheme: "http",
    cleartext: true,
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 600,
      backgroundColor: "#0a0c10",
    },
    // Don't let Capacitor pad the WebView's parent for the system bars — the
    // native MainActivity goes fully immersive (bars hidden, draw into the
    // cutout) and handles the IME inset itself, so the WebView fills the whole
    // screen with no window-background strip at the top.
    SystemBars: {
      insetsHandling: "disable",
    },
  },
};

export default config;
