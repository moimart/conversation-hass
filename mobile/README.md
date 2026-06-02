# HAL companion app (iOS + Android)

A Capacitor app that **mirrors the HAL kiosk display** and lets you talk to HAL
by **text** or **voice**. It reuses the kiosk web UI (`rpi/web`) verbatim inside
a native WebView and points it at the AI server directly.

## How it works
- **Display**: `scripts/sync-web.mjs` copies `rpi/web` → `www/` at build time
  (single source, no fork). `src/boot.ts` reads stored config, injects
  `window.HAL_CONFIG = {serverBaseUrl, wsUrl, token, pinLandscape}`, then loads
  the copied `app.js`, which connects to the server's read-only `ws://host:8765/ws/ui`
  feed and renders state/themes/photo-frame/calendar exactly like the kiosk.
- **Input**: a bottom overlay bar (`src/overlay`) sends text and on-device speech
  (Capacitor speech-recognition → text) to `POST /api/command`. The server echoes
  it back as a `transcription`, so it shows on the mirrored display automatically.
- **Pairing**: first run asks for the server URL + a 6-digit code. Ask HAL to
  pair (server `POST /api/pair/request`) → the code shows on the display →
  redeem it (`/api/pair/redeem`) for a device token, stored on-device. When the
  server runs with `HAL_REQUIRE_TOKEN=1`, the token gates `/api/command` + `/ws/ui`.
- **Demo**: `src/config/demo-config.ts` ships a default URL + code pointing at a
  hosted HTTPS/WSS HAL demo instance for App Store review (you deploy that
  instance and pre-seed the demo token).

## Build
```bash
cd mobile
npm install
npm run build            # sync-web + esbuild → www/
```

### Android (this Linux machine; Android SDK at /opt/android-sdk)
```bash
npx cap add android       # first time
npm run build && npx cap sync android
# emulator:
sdkmanager "system-images;android-34;google_apis;x86_64"
avdmanager create avd -n hal -k "system-images;android-34;google_apis;x86_64" -d pixel_6
emulator -avd hal &
npx cap run android
```
Onboard against the live server `http://10.20.30.185:8765`; ask HAL to pair and
enter the code shown on the kiosk.

### iOS (Mac 10.20.30.194; Xcode 26.4.1)
The Mac needs Node + CocoaPods first:
```bash
# install Node (nodejs.org pkg or Homebrew) and:
sudo gem install cocoapods
```
Then:
```bash
cd mobile && npm install && npm run build
npx cap add ios            # first time
npx cap sync ios           # runs pod install
npx cap run ios            # or open ios/App/App.xcworkspace in Xcode
```
**Info.plist** needs, for LAN cleartext + mic/speech:
`NSAppTransportSecurity → NSAllowsLocalNetworking = YES`,
`NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`.
The hosted demo uses WSS, so it needs no ATS exception.

## Layout
```
src/boot.ts            entry: config → display → overlay
src/config/            HalConfig (Preferences) + demo defaults
src/display/inject.ts  injects display.html body + loads app.js
src/overlay/           input bar, mic (STT), command, hide-kiosk CSS, local TTS
src/onboarding/        server URL + pairing screens, redeem client
src/platform/          keep-awake/status-bar/splash/resume wrappers
scripts/               sync-web + build (esbuild)
www/                   build output (gitignored)
android/ ios/          native projects (committed after `cap add`)
```

## Notes / follow-ups
- Token is stored via `@capacitor/preferences` (app-sandboxed). Hardening
  follow-up: move it to Keychain/Keystore via a secure-storage plugin.
- HLS (`.m3u8`) `play_video` uses the CDN hls.js (best-effort; offline → skip).
- No physical-device pass yet (emulator/simulator only): real mic STT cadence,
  keep-awake, and WS reconnect-on-resume should be validated on hardware.
