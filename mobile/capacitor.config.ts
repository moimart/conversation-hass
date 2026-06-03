import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "sh.martinez.hal.companion",
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
  },
};

export default config;
