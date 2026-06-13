<h1 align="center">PAL — Local Voice Assistant for Home Assistant</h1>

<p align="center"><em>A fully local, always-listening voice assistant with a personality, a face, and a memory.<br>No cloud. No subscriptions. Everything runs on your network.</em></p>

<p align="center">
  <img src="docs/themes/sunset_animated.jpg" width="420" alt="PAL hub — Sunset Animated theme with golden-hour bokeh and looping in-orb state video">
</p>

---

## What PAL does

- 🎙️ **Listens continuously** through a USB speakerphone, transcribing every utterance in the room
- 🗣️ **Talks back** in a voice you pick, through a Wyoming-protocol TTS service
- 🏠 **Controls Home Assistant** via MCP tool-calling — switches, scenes, climate, media, all of it
- 🧠 **Remembers** what you've told it (Shodh Hebbian long-term memory) so context survives across days
- 👁️ **Shows itself** through a hub styled after fiction's iconic AIs — an animated eye and 16 switchable themes, including two with looping in-orb state videos that change with PAL's mood
- 📸 **Displays cameras, images, and videos** inside the orb (HA snapshots, live WebRTC, RTSP, HLS playlists)
- 📅 **Pops up a calendar overlay** (month / week / day) pulled from any HA calendar, on voice or HA button
- 📜 **Keeps a conversation log** — every request, answer, and announcement persisted forever in PostgreSQL, browsable full-screen on the hub (by voice or HA button) and in the companion app, with timestamps, origin labels (which phone asked, which channel announced), and inline thumbnails of every image shown on the orb
- ⏲️ **Voice timers** — "set a timer for 5 minutes" from the hub or any phone; auto-named (Timer 1, Timer 2, …, templates configurable in any language), the asking device shows the last 10 seconds as a big countdown inside the orb, and **every** device announces when it's done. Mirrored to HA `timer.*` helpers for dashboards/automations, with an Active Timers MQTT sensor
- 🖼️ **Photo frame mode** — ambient full-screen image from a configurable HA `image.*` entity, white drop-shadow clock + Ken-Burns zoom, auto-crossfades when HA rotates the photo, dismisses on any hub action; **optional auto-activate after N minutes of inactivity**
- 💤 **Display power (DPMS)** — actually powers the panel off (not just a black overlay), with optional idle auto-blank; wakes automatically on the next wake word / PTT / TTS / takeover. Same code path on RPi (Wayland) and x86 (Wayland or X11).
- 🎯 **Wake word** *or* **Push-to-Talk** — your choice per situation (PTT triggerable from HA, an HTTP call, a WebSocket, or the desktop popup app)
- 📱 **Companion app (iOS + Android)** — pair a phone as a **satellite**: talk to PAL by text or push-to-talk voice and hear replies in PAL's own server voice, while household broadcasts (spoken announcements, theme changes, live cameras/RTSP) get pushed to your screen — all sharing the hub's conversation, history, and memory. Capacitor app that reuses the hub display verbatim. See [`mobile/`](./mobile/README.md).
- 🎵 **Multi-room audio** via an optional Music-Assistant Sendspin sidecar with PulseAudio role-ducking
- 🔌 **Speaks every protocol your house already speaks** — REST, MQTT, WebSocket — and HA auto-discovers it as a single device with sensors, switches, selects, text inputs, and buttons

Setup is two `docker compose` commands. See [Quick start](#quick-start) below.

---

## Themes

Sixteen built-in themes, switchable from the hub's picker, the LLM (`ui_set_theme` tool), the MQTT `Theme` select, or the auto day/night scheduler. Two of them (`birch_animated`, `sunset_animated`) declare per-state looping videos in their manifest — short clips that play inside the orb and crossfade as PAL moves between idle / listening / processing / speaking. Any theme can opt in via the `state_videos` field. Authoring guide: [`THEMES.md`](./THEMES.md).

| Preview | Theme | Vibe |
|---|---|---|
| <img src="docs/themes/dark.jpg" width="200"> | `dark` | The *2001: A Space Odyssey* look — matte black panel, deep red eye, white-hot core when speaking |
| <img src="docs/themes/sal.jpg" width="200"> | `sal` | SAL 9000 — the twin from *2010*, cyan eye on deep blue-black |
| <img src="docs/themes/glados.jpg" width="200"> | `glados` | Portal 2 — Aperture Science amber optic on warm black |
| <img src="docs/themes/matrix.jpg" width="200"> | `matrix` | Phosphor green on pitch black — old-CRT terminal with digital-rain canvas |
| <img src="docs/themes/mother.jpg" width="200"> | `mother` | Alien *Nostromo* — industrial dim amber on grimy near-black |
| <img src="docs/themes/joi.jpg" width="200"> | `joi` | Blade Runner 2049 — hot pink/magenta on deep teal-blue |
| <img src="docs/themes/kitt.jpg" width="200"> | `kitt` | Knight Rider — saturated crimson on chrome-edged black, with the iconic red scanner sweeping along the bottom |
| <img src="docs/themes/cyberpunk.jpg" width="200"> | `cyberpunk` | Night City — high-contrast neon yellow + electric cyan on black with animated scanlines and occasional glitch bars |
| <img src="docs/themes/birch.jpg" width="200"> | `birch` | Warm beige Scandinavian wood tones — light-room friendly |
| <img src="docs/themes/birch_animated.jpg" width="200"> | `birch_animated` | Birch palette with stylised autumn leaves drifting across the page, plus looping in-orb state videos that change with PAL's mood |
| <img src="docs/themes/odyssey.jpg" width="200"> | `odyssey` | Bright white background, minimalist — for very bright rooms |
| <img src="docs/themes/japandi.jpg" width="200"> | `japandi` | Earthy Japandi with subtle decorative background patterns |
| <img src="docs/themes/material_you.jpg" width="200"> | `material_you` | Material You light theme tuned for birch wood + white furniture, with a slow lava-lamp drift of the Google brand colours in the background |
| <img src="docs/themes/forest.jpg" width="200"> | `forest` | Moss green + amber on dark walnut — calm and organic |
| <img src="docs/themes/sunset.jpg" width="200"> | `sunset` | Coral + peach on dusk plum — warm and gentle, with drifting golden-hour bokeh |
| <img src="docs/themes/sunset_animated.jpg" width="200"> | `sunset_animated` | Sunset's plum/coral palette and bokeh drift, plus looping in-orb state videos that change with PAL's mood |

---

## How you talk to PAL

| Mode                              | What it is                                                                                                                            |
|-----------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| **Wake word**                     | Say `"hey hal"` (or whatever `WAKE_WORD` you set), pause, then your command. Chime fires + eye flashes when wake is detected.        |
| **Push-to-Talk (PTT)**            | Bypass the wake word. Trigger via the desktop popup's hold-button, an HA dashboard button, an HTTP POST, an MQTT publish, or a persistent WebSocket. Hold-to-talk via Zigbee remote works too. See [`API.md`](./API.md#push-to-talk) + [`MQTT.md`](./MQTT.md#push-to-talk). |
| **Typed text**                    | `POST /api/command` (or write to HA's `text.<id>_command` entity, or publish to MQTT `hal/<id>/command`) — runs the full LLM round with tools. |
| **Verbatim announcement**         | `POST /api/speak` (or `text.<id>_speak`, or MQTT `hal/<id>/speak`) — PAL says the exact words you wrote, no LLM in the loop. |
| **Companion app (satellite)**     | Pair an iOS/Android phone. Text or PTT voice runs in the shared conversation, but that turn's transcript, orb, reply, and voice route **only to your phone** — plus it receives household broadcasts. See [`mobile/`](./mobile/README.md). |
| **Watch (wrist PTT)**             | Apple Watch or Pixel Watch: tap the orb, dictate, and PAL's reply lands back on your wrist — standalone via the gateway, least-privilege `watch`-scope token, hub stays quiet. Timer haptics + quick-launch (Tile / complication). See [`mobile/`](./mobile/README.md#watch-apps). |
| **Follow-up window**              | After a turn ends, you have ~10 s to reply without repeating the wake word.                                                          |
| **Always-on mode**                | Set `WAKE_WORD=` (empty) to process every transcribed line through the LLM.                                                           |

---

## Companion app (satellites)

Pair an **iOS or Android** phone and it becomes a **satellite** — not a mirror of the hub. A satellite shares the same household conversation, history, and long-term memory, but anything *you* trigger on the phone (its transcript, orb state, reply text, and PAL's **server-voice TTS**) routes **only to that phone**, never the hub. The home display keeps doing its own thing.

Phones also receive **household broadcasts** — proactive actions fired from voice, HA, or MQTT propagate to every connected satellite:

- 🔊 Spoken announcements (`/api/speak`) — the text **and** PAL's voice
- 🎨 Theme changes
- 📸 Camera snapshots, images, and on-orb video (auto-dismiss on the same `duration_s` as the hub)
- 🎥 Live streams — RTSP (via go2rtc) and HA-camera WebRTC, each phone negotiating its own peer

Each phone also gets its own idle **photo-frame** screensaver, dismissed automatically when a broadcast arrives. Per-phone pairing tokens gate access, and turns from different phones stay isolated from one another.

> Live video is peer-to-peer to your cameras / go2rtc on the LAN, so streams need a phone on home Wi-Fi; text, announcements, themes, and snapshots work from anywhere the server is reachable.

Build & install steps (Android + iOS, Capacitor): [`mobile/README.md`](./mobile/README.md).

### Remote access (optional satellite gateway)

By default a phone reaches PAL only on your LAN (or a VPN like Tailscale). The
optional **`hal-gateway`** container (in `docker-compose.server*.yml`) is a
token-gated reverse proxy you can expose to the internet — paired phones then
work from anywhere, while the AI server itself stays LAN-only:

- It forwards **only** the satellite-capable routes (talk, conversation log,
  server-voice TTS, camera MJPEG fallback, photo-frame, themes) and rejects
  everything else — the home-control surface (pairing, announce, display,
  volume, MCP, MQTT) is never reachable from outside.
- Every private route is authenticated **at the edge** by the device's pairing
  token (validated against the server's own `/api/pair/status`). The gateway
  holds no secrets.
- **Pairing is local-only**: you can only add a phone at home; afterwards it
  uses the gateway. The app tries your home URL first and falls back to the
  gateway when away (set `HAL_GATEWAY_URL`, pair once at home).

Put it behind any TLS ingress — a Cloudflare Tunnel hostname, a reverse proxy,
or Tailscale Funnel. Allowlist details in [`API.md`](./API.md) and `gateway/`.

### Watch apps (push-to-talk)

Native watch apps for **Apple Watch** (`mobile/ios/App/PALWatch`, SwiftUI) and
**Pixel Watch / Wear OS** (`mobile/wear`, Compose) turn the wrist into PAL's
fastest input: **tap the orb → dictate → PAL runs the command → the reply
lands on your wrist** with a haptic. Both are **standalone** — on a cellular
watch they work with no phone present.

- 🎙️ **Dictation** — the watch transcribes and sends only the final *text*.
  Apple Watch uses the system dictation screen (watchOS has no in-app
  recognizer); the Pixel does in-app live recognition (orb-as-mic).
- 🔐 **Least-privilege, self-enrolled** — each watch types the hub's 6-digit
  code and redeems a `watch`-scope token (`POST /api/pair/redeem`, scope
  `watch`), persisted on the watch. That token may only command PAL, probe its
  validity, and register for push — *nothing else* (no live mirror, no cloud
  override, no admin) — and revokes on its own without touching the phone.
- 🌍 **Works anywhere** — talks to the satellite gateway over HTTPS and uses
  `wait_reply` on `POST /api/command`, so PAL's reply returns in the same
  request (no WebSocket). The turn is **private** — the hub stays quiet,
  exactly like a phone satellite.
- ⏲️ **Push haptics** — finished timers/announcements buzz the wrist while the
  app is closed: the Pixel registers its own FCM token; the Apple Watch mirrors
  the iPhone's PAL notifications.
- ⚡ **Quick-launch** — a Pixel **Tile** (swipe → tap → listening) and an Apple
  Watch **complication** (tap the face → PAL).

Build & deploy notes (plus the platform gotchas): [`mobile/README.md`](./mobile/README.md).

### Push notifications (when the app is closed)

When a paired phone's app is **closed or backgrounded**, PAL can still reach it
with native OS notifications — via **APNs** (iOS) and **FCM** (Android):

- 🔔 **Spoken announcements** — the text PAL announced
- ⏲️ **Finished timers** — on their own notification channel/sound
- 🖼️ **Orb images** — shown as an **inline thumbnail** in the notification

Privacy-preserving by design: the notification text/URL transits Apple/Google,
but the **image bytes are fetched from your own gateway** via a short-lived
signed link — they never touch Apple or Google. Delivery only happens when the
device is offline (the app open = it gets the live `/ws/ui` feed instead, no
push). Requires an Apple Developer account (APNs key) and a Firebase project
(FCM); credentials live in `server/runtime/push_providers.json` (hot-reloaded,
never committed). Setup notes in `CLAUDE.md`; the build/install is in
[`mobile/README.md`](./mobile/README.md).

---

## Integrations & extension points

| Doc                                       | What's in it                                                                                                       |
|-------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| [`API.md`](./API.md)                      | Full REST + WebSocket reference (PTT, command, speak, mute, volume, snapshots, themes; `/ws/{ptt,ui,audio}`)        |
| [`MQTT.md`](./MQTT.md)                    | Every MQTT topic the bridge subscribes to or publishes; complete HA Discovery entity table; automation snippets    |
| [`THEMES.md`](./THEMES.md)                | Plug-in theme authoring (CSS variable reference, `effect.js` API, manifest schema, hot-reload behaviour)            |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md)    | Node layout, pipeline + LLM routing (local/agentic/cloud), STT, PTT, orb camera/video, conversation log, timers, satellites + gateway, push notifications, project structure |
| [`openclaw-channel/hal/`](./openclaw-channel/hal/README.md) | OpenClaw channel plugin — routes voice through an OpenClaw agent with full mcporter/MCP tool access, Ollama fallback |
| `openclaw-skill/hal/SKILL.md`             | OpenClaw skill teaching the agent how to control PAL's hub via mcporter                                          |
| `desktop/`                                | Rust/GTK4 Wayland overlay for typing commands + hold-to-talk PTT from your Linux desktop                            |
| [`mobile/`](./mobile/README.md)           | iOS + Android companion app (Capacitor) — pair a phone as a satellite for text/voice + household broadcasts          |

---

## Quick start

> Two `docker compose` commands once `.env` is configured. Full env variable reference at the [bottom of this README](#configuration-reference); pipeline internals in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

### Prerequisites

| Component                       | Requirement                                                                              |
|---------------------------------|------------------------------------------------------------------------------------------|
| AI Server                       | Linux box with NVIDIA GPU, Docker + nvidia-container-toolkit                             |
| Raspberry Pi                    | Pi 4/5 with Docker, USB speakerphone (e.g. Anker PowerConf S330)                         |
| Ollama                          | Running on the AI server host with a tool-calling-capable model                          |
| Home Assistant                  | Running, with an MCP server exposed over HTTP(S)                                         |
| Wyoming TTS                     | Any Wyoming-protocol TTS service (e.g. [wyoming-piper](https://github.com/rhasspy/wyoming-piper)) |
| MQTT broker *(optional)*        | Mosquitto, EMQX, the HA Mosquitto add-on — anything Paho/aiomqtt can speak v3.1.1 to     |
| Music Assistant *(optional)*    | Required only for the Sendspin multi-room sidecar                                        |

### 1. Clone and configure

```bash
git clone https://github.com/moimart/conversation-hass.git
cd conversation-hass
cp .env.example .env
# Edit .env — see the Configuration reference at the bottom
```

### 2. Start the AI server

```bash
# Pre-built (recommended for first try):
docker compose -f docker-compose.server-ghcr.yml up -d

# Or build from source:
docker compose -f docker-compose.server.yml up --build -d
```

Brings up `hal-ai-server` (port **8765**), `hal-stt-service` (port **8770** — decoupled speech-to-text), `hal-shodh-memory` (port **3030**), and a `go2rtc` sidecar (host networking, port **1984**).

### 3. Start the Raspberry Pi

```bash
# Pre-built arm64:
docker compose -f docker-compose.rpi-ghcr.yml up -d

# Or build from source:
docker compose -f docker-compose.rpi.yml up --build -d
```

Brings up `hal-audio-streamer` (port **8080**, web UI + mic capture + speaker playback) and the optional `hal-sendspin` sidecar.

### 4. Open the hub

Navigate to `http://<rpi-ip>:8080`. For HA snapshots to work, launch the hub Chromium with the DevTools Protocol enabled:

```bash
chromium-browser --hub http://localhost:8080 \
  --autoplay-policy=no-user-gesture-required \
  --remote-debugging-port=9222
```

### 5. (Optional) Desktop command popup

A lightweight Rust/GTK4 Wayland overlay that types commands and hold-to-talks PTT from your Linux desktop.

```bash
cd desktop && ./install.sh
# Hyprland keybind:  bind = SUPER, H, exec, hal-command
```

### 6. (Optional) Companion app (iOS + Android)

Pair a phone as a satellite. Build and run from `mobile/` (Capacitor):

```bash
cd mobile && npm install && npm run build
npx cap run android      # or: npx cap run ios
```

On first launch, enter the server URL (`http://<ai-server-ip>:8765`), then ask PAL to pair and type the 6-digit code shown on the hub. Full build notes (emulator, iOS signing, Info.plist exceptions): [`mobile/README.md`](./mobile/README.md).

---

## Pre-built images

| Image                                                                  | Platform     | Purpose                                                            |
|------------------------------------------------------------------------|--------------|--------------------------------------------------------------------|
| `ghcr.io/moimart/conversation-hass/hal-ai-server:latest`               | `linux/amd64`| FastAPI server, VAD + transcription cadence, MCP routing, MQTT bridge |
| `ghcr.io/moimart/conversation-hass/hal-rpi:latest`                     | `linux/arm64`| Pi audio_streamer + hub web UI                                   |
| `ghcr.io/moimart/conversation-hass/hal-sendspin:latest`                | `linux/arm64`| Sendspin daemon for Music Assistant                                |

Tagged versions also published (`:0.10`, etc. — the previous stable is preserved with each release for easy revert).

> **`stt-service`** (decoupled speech-to-text) builds from source — it's defined in both server compose files with `build:` (context = repo root, `stt-service/Dockerfile`) and brought up automatically by `docker compose … up`. No pre-built image is published yet.

---

## Configuration reference

> Bootstrap-from-env on first run; runtime-changeable keys are listed in the [Live runtime config table](./ARCHITECTURE.md#live-runtime-config) — once changed from HA, the file (`server/runtime/config.json`) wins over `.env`.

### Network

| Variable             | Default                       | Description                                                          |
|----------------------|-------------------------------|----------------------------------------------------------------------|
| `AI_SERVER_HOST`     | —                             | IP of the AI server (used by RPi to connect)                         |
| `RPI_HOST`           | —                             | IP of the RPi (informational; not consumed by code)                  |
| `WEB_PORT`           | `8080`                        | Port for the RPi web UI                                              |
| `CHROMIUM_DEBUG_URL` | `http://127.0.0.1:9222`       | Hub Chromium's DevTools endpoint (for snapshot capture)            |
| `SNAPSHOT_INTERVAL_S`| `60`                          | Seconds between CDP screenshots posted to the AI server              |

### Speech & LLM

| Variable             | Default                              | Description                                                |
|----------------------|--------------------------------------|------------------------------------------------------------|
| `WAKE_WORD`          | `hey hal`                            | Activation phrase. Empty = always-on                       |
| `STT_ENGINE`         | `whisper`                            | STT engine the **`stt-service`** loads: `whisper` or `nemotron`. (The AI server itself is pinned to `remote` in compose and calls the stt-service.) |
| `STT_MODEL`          | (auto per engine)                    | Model for the stt-service engine — Whisper: `large-v3-turbo`; Nemotron: `nvidia/parakeet-tdt-0.6b-v2` |
| `STT_REMOTE_URL`     | `http://stt-service:8770`            | Where the AI server reaches the decoupled STT service      |
| `OLLAMA_HOST`        | `http://localhost:11434`             | Ollama API                                                 |
| `OLLAMA_MODEL`       | `llama3.2`                           | LLM name (must support tool calling)                       |
| `OLLAMA_NUM_CTX`     | `32768`                              | LLM context window in tokens                               |
| `OLLAMA_NUM_PREDICT` | `512`                                | Max tokens per LLM response                                |
| `WYOMING_TTS_HOST`   | `localhost`                          | Wyoming TTS host                                           |
| `WYOMING_TTS_PORT`   | `10200`                              | Wyoming TTS port                                           |
| `WYOMING_TTS_VOICE`  | (server default)                     | Voice name; empty = service default                        |
| `SYSTEM_PROMPT_FILE` | `/app/system_prompt.txt`             | LLM system prompt path inside the container                |

#### Cloud LLM override (optional)

A **Cloud Override** switch (HA / `POST /api/cloud_llm`) routes the entire
tool loop to a cloud OpenAI-compatible provider — skipping the router model
and OpenClaw. **It always boots OFF after a restart** (cloud spend requires a
deliberate flip); the chosen model persists. Providers + API keys live ONLY in
a hot-reloadable secrets file `server/runtime/cloud_providers.json` (gitignored
— keys never appear on MQTT, in logs, or in API responses):

```json
{
  "providers": [
    { "name": "openai",    "base_url": "https://api.openai.com/v1",    "api_key": "sk-..." },
    { "name": "anthropic", "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-..." }
  ]
}
```

The HA **Cloud Model** select lists `provider/model-id` entries fetched live
from every configured provider's `/models` API (DeepSeek, Kimi, OpenRouter —
any OpenAI-compatible endpoint works; Anthropic goes through its OpenAI-compat
layer). Editing the file needs **no restart** (mtime hot-reload). Env fallback:
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` auto-create those two profiles.
`CLOUD_PROVIDERS_PATH` overrides the file location.

### MCP & Memory

| Variable           | Default                              | Description                                       |
|--------------------|--------------------------------------|---------------------------------------------------|
| `MCP_SERVERS_FILE` | `/app/mcp_servers.json`              | Path to the MCP server list inside the container  |
| `MCP_SERVER_URL`   | —                                    | Single-server fallback if `mcp_servers.json` is absent |
| `MEMORY_URL`       | `http://shodh-memory:3030`           | Shodh Memory service URL                          |
| `MEMORY_USER_ID`   | `hal-default`                        | User ID for memory isolation                      |
| `MEMORY_API_KEY`   | —                                    | Shodh API key (if your instance requires auth)    |

### OpenClaw (optional agentic backend)

| Variable                | Default              | Description                                                          |
|-------------------------|----------------------|----------------------------------------------------------------------|
| `OPENCLAW_ENABLED`      | `false`              | Enable OpenClaw as conversation engine (falls back to Ollama on error) |
| `OPENCLAW_GATEWAY_URL`  | (empty)              | Gateway URL, e.g. `http://gateway-host:18789`                        |
| `OPENCLAW_WORKSPACE`    | (empty)              | Workspace name on the gateway                                        |

See [`openclaw-channel/hal/README.md`](./openclaw-channel/hal/README.md) for full setup.

### Home Assistant (live streaming, calendar, image fetch)

| Variable   | Default | Description                                                         |
|------------|---------|---------------------------------------------------------------------|
| `HA_URL`   | —       | HA base URL, e.g. `http://homeassistant.local:8123` (empty disables streaming/calendar/image-fetch) |
| `HA_TOKEN` | —       | HA Long-Lived Access Token                                          |
| `GO2RTC_URL` | `http://host.docker.internal:1984` | go2rtc HTTP/WS endpoint reachable from the AI server container |

### Audio (RPi)

| Variable      | Default   | Description                                       |
|---------------|-----------|---------------------------------------------------|
| `AUDIO_DEVICE`| `default` | ALSA device (auto-detects USB speakerphones)      |
| `SAMPLE_RATE` | `16000`   | Target audio sample rate (Hz)                     |
| `CHANNELS`    | `1`       | Target audio channels                             |
| `CHUNK_SIZE`  | `4096`    | Audio buffer size per WebSocket frame             |

### Themes

| Variable      | Default | Description                                                          |
|---------------|---------|----------------------------------------------------------------------|
| `AUTO_THEME`  | `true`  | Auto-switch themes at dusk/dawn via `sun.sun`                        |
| `THEME_DAY`   | `birch` | Theme used when sun is above horizon                                 |
| `THEME_NIGHT` | `dark`  | Theme used when sun is below horizon                                 |

### MQTT (HA auto-discovery)

| Variable           | Default              | Description                                            |
|--------------------|----------------------|--------------------------------------------------------|
| `MQTT_BROKER_HOST` | (empty = disabled)   | MQTT broker hostname/IP                                |
| `MQTT_BROKER_PORT` | `1883`               | MQTT broker port                                       |
| `MQTT_USERNAME`    | —                    | Broker username (optional)                             |
| `MQTT_PASSWORD`    | —                    | Broker password (optional)                             |
| `HAL_DEVICE_ID`    | `hal-default`        | MQTT object id (slug)                                  |
| `HAL_DEVICE_NAME`  | `PAL`                | Display name in HA                                     |
| `START_MUTED`      | `false`              | Boot with mic muted (live-toggleable from HA)          |

### Calendar overlay

| Variable                     | Default | Description                                                          |
|------------------------------|---------|----------------------------------------------------------------------|
| `CALENDAR_DEFAULT_SOURCE`    | (empty) | Default calendar name; empty = merge all HA calendars                |
| `CALENDAR_DISMISS_SECONDS`   | `30`    | Default duration the overlay stays up before auto-dismissing         |

### Conversation log

| Variable               | Default                                              | Description                                                       |
|------------------------|------------------------------------------------------|-------------------------------------------------------------------|
| `POSTGRES_PASSWORD`    | (empty = log disabled)                               | Password for the bundled `postgres` service (compose-network only, no published port) |
| `CONVERSATION_LOG_DSN` | `postgresql://hal:${POSTGRES_PASSWORD}@postgres:5432/hal` | Override to point at an external PostgreSQL instead              |

Every user request, assistant answer, and announcement is stored forever
(origin-labeled: which paired phone asked, which channel — `api` / `mqtt` /
`openclaw` / `voice-tool` — announced). Browse it full-screen: say *"show the
conversation log"* (hub, auto-dismisses after 30 s of no interaction), press
the HA **Show Conversation Log** button, or tap the list button in the
companion app (stays open until ✕). Scroll up to page older history in. A
postgres outage never breaks a turn — writes are dropped with a warning and
the connection heals itself.

### Voice timers

| Variable                  | Default            | Description                                                  |
|---------------------------|--------------------|--------------------------------------------------------------|
| `TIMER_NAME_TEMPLATE`     | `Timer {n}`        | How timers are named (`{n}` = number) — any language          |
| `TIMER_ANNOUNCE_TEMPLATE` | `{name} is ready.` | What PAL says when a timer finishes (`{name}` = timer name)  |

Both are live runtime config (HA text entities) — the env vars only seed the
first boot. Say *"set a timer for 5 minutes"* (hub or phone): the device that
asked shows the final 10 seconds as a countdown inside the orb, and every
device plays a kitchen-timer alarm followed by the spoken announcement (the
beep pattern is prepended to the TTS audio server-side, so even a TTS outage
still makes noise). Timers mirror onto pre-created HA helpers
`timer.pal_timer_1`–`timer.pal_timer_5` (live remaining time in dashboards,
`timer.finished` for automations) — PAL is the source of truth, so cancelling
the HA entity does **not** stop the timer; cancel by voice ("cancel timer 2",
"cancel all timers"). Active timers don't survive an AI-server restart.

### Sendspin (multi-room audio)

| Variable                     | Default              | Description                                                                          |
|------------------------------|----------------------|--------------------------------------------------------------------------------------|
| `SENDSPIN_PLAYER_ENTITY`     | (empty = disabled)   | MA `media_player.*` entity_id — enables button redirection and the optional Shape C  |
| `SENDSPIN_PAUSE_DURING_TTS`  | `false`              | Shape C: explicit pause/resume around TTS (only resumes if MA was playing)           |
| `SENDSPIN_LOG_LEVEL`         | `INFO`               | Sendspin daemon log level                                                            |

---

## Security model

PAL is a home appliance with a deliberately simple trust model. Read this before
exposing anything beyond your LAN.

### Trust boundaries

| Surface | Who can reach it | Auth |
|---|---|---|
| **AI server** `:8765` (hub, RPi, HA, MQTT, satellites-on-VPN) | Your LAN (and Tailscale, if you use it) | **LAN-trusted** — most routes need no token by default |
| **Satellite gateway** `:8766` (only if you expose it) | The internet, via your TLS ingress | **Token-gated allowlist** — every private route requires a paired-device token |

**The LAN is trusted.** By default (`HAL_REQUIRE_TOKEN` unset) the AI server
treats anything on your network as authorized — `/api/command`, `/ws/ui`, etc.
need no token. Anyone on your Wi-Fi can talk to PAL, and **talking to PAL can
control your house** (lights, locks, anything Home Assistant exposes), because
PAL drives HA through the LLM. This is fine for a home LAN; it is **not** a
public service. The satellite-only routes (`/api/satellite/*`) always require a
token even on the LAN. You can flip the whole server to token-required with
`HAL_REQUIRE_TOKEN=1` once every device is paired.

### Pairing & tokens

- A phone becomes a **satellite** by redeeming a 6-digit code **shown on the
  hub** for a long-lived device token (32-byte URL-safe random). Pairing
  therefore requires physical access to the display — it can only happen at
  home, never remotely.
- Tokens are persisted server-side (`runtime/pairing_tokens.json`) and, on the
  phone, in the **iOS Keychain / Android Keystore**-backed secure store (not
  plaintext Preferences); the non-secret config lives in Preferences. Still,
  treat a paired phone as a house key.
- **Revoke a lost/compromised device** with the LAN-only routes
  `GET /api/pair/devices` (lists names + token *prefixes*, never full tokens)
  and `POST /api/pair/revoke` (`{"device_name": "..."}` or `{"token": "..."}`).
  A revoked token dies immediately on the server and within ~30 s on the remote
  (gateway) path.

### Exposing to the internet (the gateway)

The optional `hal-gateway` (see *Remote access* above) is the **only** thing you
should put on the internet — never port-forward `:8765` directly. It is a
default-deny reverse proxy:

- It forwards an explicit **allowlist** of satellite routes (+ the `/ws/ui`
  upgrade) and `404`s everything else — pairing mint/redeem, device admin,
  `/api/speak`, `/api/display`, `/api/volume`, `/api/ptt/*`, `/mcp`, and MQTT
  are all unreachable from outside.
- Every private route is authenticated **at the edge** against the server's own
  `/api/pair/status`; `/ws/ui` requires a valid token (no public mirror mode).
- It **holds no secrets** — compromising the gateway container yields only the
  network position any LAN device already has.
- Put it behind real TLS (Cloudflare Tunnel, a reverse proxy, or Tailscale
  Funnel). Do **not** add a Cloudflare Access / SSO policy in front — the
  pairing token *is* the auth; an extra login layer would just break the app.

**Residual risk to accept before exposing:** a stolen device token lets the
holder talk to PAL from the internet — i.e. command the house through the LLM.
It's revocable (above) and rate-limited at the gateway, but it is real. If that
isn't acceptable for your threat model, keep satellites on Tailscale instead of
exposing the gateway.

### Secrets

The main repo (`moimart/conversation-hass`) is **public**. No secrets live in
it: `.env`, `cloud_providers.json`, and pairing tokens are gitignored; real
secrets live encrypted in a separate git-crypt vault. Cloud-LLM provider keys
stay server-side in `server/runtime/cloud_providers.json` (hot-reloaded, never
sent to the app, MQTT, or the conversation log). The **Cloud LLM override boots
OFF after every restart** so a cloud bill is never an accident. The conversation
log stores a device's friendly name as the origin of a turn — **never** its
token.

---

## Running tests

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements-test.txt
pytest tests/ -v
```

All tests run without GPU, ML models, or external services — every external dependency (HA WS, MCP servers, TTS, audio devices, runtime config) is mocked.

---

## License

MIT
