# HAL — Local Voice Assistant for Home Assistant

A fully local, always-listening voice assistant that controls your smart home through natural conversation. Runs on two nodes: a **Raspberry Pi** for audio I/O and a sleek web UI, and an **AI server** with a GPU for speech recognition, language understanding, and speech synthesis.

No cloud services. No subscriptions. All processing stays on your network.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                      LOCAL NETWORK                                      │
│                                                                                         │
│  ┌──────────────────────────────┐              ┌──────────────────────────────────────┐  │
│  │       RASPBERRY PI           │   WebSocket  │           AI SERVER (GPU)            │  │
│  │                              │    :8765     │                                      │  │
│  │  ┌────────────────────────┐  │              │  ┌──────────────────────────────┐    │  │
│  │  │   Audio Streamer       │  │  PCM 16-bit  │  │       FastAPI Server          │   │  │
│  │  │                        │──┼──────────────┼─►│                              │    │  │
│  │  │  Anker Powerconf S330  │  │   mono audio │  │  ┌────────┐  ┌───────────┐  │    │  │
│  │  │  (48kHz stereo → 16kHz │  │              │  │  │Silero  │  │ STT Engine│  │    │  │
│  │  │   mono, FIR filtered)  │  │              │  │  │VAD     │─►│ Whisper / │  │    │  │
│  │  │  ┌──────┐  ┌───────┐  │◄─┼──────────────┼──│  │(voice  │  │ Nemotron  │  │    │  │
│  │  │  │ Mic  │  │Speaker│  │  │  WAV audio   │  │  │activity│  │           │  │    │  │
│  │  │  └──────┘  └───────┘  │  │  + JSON msgs │  │  └────────┘  └─────┬─────┘  │    │  │
│  │  └────────────────────────┘  │              │  │                    │         │    │  │
│  │                              │              │  │  ┌────────────────▼──────┐  │    │  │
│  │  ┌────────────────────────┐  │              │  │  │  Speaker Filter       │  │    │  │
│  │  │   Web UI  (:8080)      │  │  transcript  │  │  │  (resemblyzer)        │  │    │  │
│  │  │                        │◄─┼──────────────┼──│  │  human vs AI voice    │  │    │  │
│  │  │  HAL 9000 Eye          │  │  + state     │  │  └──────────┬────────────┘  │    │  │
│  │  │  (4 themes,            │  │  + wake flash│  │             │               │    │  │
│  │  │   auto day/night)      │  │              │  │  ┌──────────▼────────────┐  │    │  │
│  │  │  Live Transcription    │  │  + JPEG      │  │  │ Conversation Manager  │  │    │  │
│  │  │  Response Display      │  │   snapshots  │  │  │                       │  │    │  │
│  │  └────────────────────────┘  │              │  │  │  Wake word + chime    │  │    │  │
│  │                              │              │  │  │  (or always-on mode)  │  │    │  │
│  │  ┌────────────────────────┐  │              │  │  │  Command accumulation │  │    │  │
│  │  │   Sendspin sidecar     │  │              │  │  │  Follow-up window     │  │    │  │
│  │  │   (Music Assistant     │  │              │  │  └─┬────────────┬───┬────┘  │    │  │
│  │  │    multi-room player)  │  │              │  │    │            │   │       │    │  │
│  │  │   + Pulse role-ducking │  │              │  │  ┌─▼─────┐ ┌───▼───▼────┐  │    │  │
│  │  └────────────────────────┘  │              │  │  │Ollama │ │ Wyoming    │  │    │  │
│  │                              │              │  │  │LLM    │ │ TTS        │  │    │  │
│  └──────────────────────────────┘              │  │  │(tools)│ └────────────┘  │    │  │
│                                                │  │  └───┬───┘                 │    │  │
│                                                │  │      │ MCP + LocalTools    │    │  │
│                                                │  │  ┌───▼──────────────────┐  │    │  │
│                                                │  │  │ Multi-MCP Client     │──┼────┼──► HA MCP Server(s)
│                                                │  │  └──────────────────────┘  │    │  │
│                                                │  └──────────────────────────────┘   │  │
│                                                │                                      │  │
│                                                │  ┌──────────────────────────────┐    │  │
│                                                │  │  MQTT Bridge                 │    │  │
│                                                │  │  HA auto-discovery → state,  │────┼──► MQTT broker → HA
│                                                │  │  volume, mute, theme, cam,   │    │  │
│                                                │  │  speak text                  │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                │  ┌──────────────────────────────┐    │  │
│                                                │  │  Shodh Memory (:3030)        │    │  │
│                                                │  │  Hebbian long-term memory    │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Continuous listening** — The Raspberry Pi captures audio from the Anker Powerconf S330 USB speakerphone (48kHz stereo, FIR anti-alias filtered and downmixed to 16kHz mono) and streams raw PCM audio over WebSocket to the AI server.

2. **Always-on transcription** — The AI server runs Silero VAD (voice activity detection) and a configurable STT engine to transcribe everything said in the room. All transcriptions appear live on the web UI regardless of whether a command was issued.

3. **Speaker identification** — Resemblyzer voice embeddings distinguish human speakers from the AI's own TTS output, preventing the assistant from responding to itself.

4. **Wake word or always-on** — Two modes:
   - **Wake word mode** (default): Transcribed text is monitored for the wake word (configurable). Only after detecting it does the system engage the LLM. A two-tone chime plays and the HAL eye flashes white as confirmation. A 10-second follow-up window allows natural back-and-forth conversation without repeating the wake word.
   - **Always-on mode**: Set `WAKE_WORD=` (empty) to process every transcribed line through the LLM automatically.

5. **LLM with multi-MCP tool calling** — The user's command is sent to Ollama (configurable context window, defaults to 32k) with tool definitions discovered at startup from one or more MCP servers (typically Home Assistant, plus a built-in `LocalTools` server that exposes `ui_set_theme`, `audio_set_volume`, `audio_toggle_mute`, `get_sun_times`, `speak_verbatim`, `show_camera`, `show_image`, `stream_camera`, `stream_rtsp`, `play_video`, and `stop_streaming`). The LLM searches for entities by friendly name, then calls HA services with the correct entity IDs. Conversational replies are returned as plain text. The system prompt is loaded from `server/system_prompt.txt` — edit it to customize HAL's personality.

6. **Long-term memory** — Before each LLM call, relevant memories are recalled from [Shodh Memory](https://www.shodh-memory.com/) and injected into the prompt as context. After each exchange, the conversation is stored as a new memory. Shodh uses Hebbian learning (connections that fire together wire together) and natural decay, so frequently referenced facts strengthen over time while irrelevant ones fade — like biological memory.

7. **Local TTS** — The response is synthesized via a Wyoming-protocol TTS service and streamed back to the Raspberry Pi for playback through the speaker (resampled to the device's native rate).

8. **Web UI with themes** — A modern HAL 9000-inspired interface with metallic bezel ring, animated red eye, live transcription, AI responses, and assistant state indicators. Four themes (`dark`, `birch`, `odyssey`, `japandi`); optional auto day/night switching driven by HA's `sun.sun` entity.

9. **Home Assistant integration** — When MQTT is configured, the AI server publishes HA Discovery messages so HAL appears as a single device exposing state, volume, mute, theme, a camera (the latest UI snapshot), and a text input that speaks anything you write into it. The audio_streamer captures the kiosk via the Chrome DevTools Protocol against the running kiosk Chromium and forwards the JPEG to the server every minute (live video frames, custom fonts, animations, masks, and filters all included — exact pixels, no html2canvas approximations).

10. **Multi-room audio (optional)** — A Sendspin sidecar on the Pi registers an [Open Home Foundation](https://www.openhomefoundation.org/) multi-room player in Music Assistant. HAL TTS and Sendspin music share the same PulseAudio socket; HA announcements duck the music via `module-role-ducking` while HAL speaks. Hardware volume buttons on the Anker target MA when music is playing and HAL TTS otherwise.

11. **Camera in the orb** — Ask HAL to show or stream any HA camera and it appears inside the eye (filling the area up to the metallic rim, with the bezel and crystal highlights still on top). `show_camera` paints a snapshot for 150 s by default; `stream_camera` opens a low-latency WebRTC stream against HA's built-in go2rtc (HA 2024.11+) for up to 5 min, ended early by saying "stop streaming". The kiosk owns the `RTCPeerConnection`; the server proxies SDP/ICE between kiosk and HA's WebSocket API. Live audio is dropped — only video is requested — to avoid feedback with HAL's own TTS.

## Speech-to-Text Engines

Two STT engines are available, selectable at runtime via the `STT_ENGINE` environment variable:

| Engine | Models | Speed | Best for |
|---|---|---|---|
| `whisper` (default) | `large-v3-turbo`, `large-v3`, `medium`, `base` | Baseline | Accuracy, multilingual, noisy environments |
| `nemotron` | `nvidia/parakeet-tdt-0.6b-v2`, `nvidia/parakeet-ctc-1.1b` | ~21x faster | Real-time streaming, low latency |

```ini
# .env — switch to Nemotron
STT_ENGINE=nemotron
STT_MODEL=nvidia/parakeet-tdt-0.6b-v2
```

Both engines are included in the Docker image and share the same interface. Models are cached in a Docker volume across restarts.

## Themes

Four built-in themes, switchable from the web UI's theme picker, the LLM (`ui_set_theme` tool), the MQTT theme select, or the auto day/night scheduler:

| Theme | Vibe |
|---|---|
| `dark` | Classic 2001 — matte black panel, deep red eye, white-hot core when speaking |
| `birch` | Warm beige Scandinavian wood tones — light room friendly |
| `odyssey` | Bright white background, minimalist — for very bright rooms |
| `japandi` | Earthy Japandi with subtle decorative background patterns |

**Auto day/night switching** (default on): the server polls `sun.sun` every 5 minutes and swaps `THEME_NIGHT` ↔ `THEME_DAY` at dusk/dawn. Disable with `AUTO_THEME=false`.

## Home Assistant Integration

When `MQTT_BROKER_HOST` is set, HAL appears in HA via auto-discovery as a single device with:

| Entity | Type | Purpose |
|---|---|---|
| `sensor.<id>_state` | sensor | `idle` / `listening` / `processing` / `speaking` |
| `number.<id>_volume` | number | TTS volume 0–100 % |
| `switch.<id>_mute` | switch | Mic mute state |
| `select.<id>_theme` | select | Live theme switching |
| `camera.<id>_screen` | camera | Latest JPEG snapshot of the kiosk UI |
| `text.<id>_speak` | text | Type anything → HAL speaks it via Wyoming TTS |
| `text.<id>_command` | text | Type a command → run through the conversation pipeline (LLM + tools) |
| `text.<id>_show_image` | text | Paste a URL (or short JSON) → show on the orb for 60 s |
| `text.<id>_stream_rtsp` | text | Paste an RTSP URL → live WebRTC stream in the orb (5 min default) |
| `text.<id>_play_video` | text | Paste an HTTP video URL (MP4/WebM/HLS) → play in the orb |
| `text.<id>_show_camera` | text | Paste a HA `camera.*` entity_id → snapshot in the orb. JSON `{"entity_id":"...","live":true,"duration_s":N}` for a live stream |
| `sensor.<id>_last_response` | sensor | Last thing HAL said (state truncated to 250 chars; full text in `full_text` attribute) |

Set `HAL_DEVICE_ID` (slug) and `HAL_DEVICE_NAME` (display name) to identify the device. State is republished on reconnect; availability uses MQTT Last-Will-Testament.

### Images and cameras in the orb

HAL can paint any image — HA camera snapshot, live WebRTC stream, or
arbitrary URL — inside the eye, filling the area up to the metallic
rim while keeping the bezel and crystal highlights on top. Three
modes, all mutually exclusive (newest replaces previous):

- **HA camera snapshot** — `show_camera(entity_id, duration_s=150)`
  fetches a single JPEG via the HA MCP server and displays it for
  5–900 s (default 150).
- **HA camera live stream** — `stream_camera(entity_id, duration_s=300)`
  opens a WebRTC peer connection. The kiosk owns the
  `RTCPeerConnection`; the server proxies SDP/ICE between kiosk and
  HA's `camera/webrtc/offer` subscription. Default 5 min, max 30.
  End early with `stop_streaming` ("stop streaming", "stop the
  video", etc.). Audio is dropped (video only).
- **Arbitrary RTSP live stream** — `stream_rtsp(rtsp_url, duration_s=300)`
  takes any RTSP URL (with optional inline credentials) and streams it
  via the bundled go2rtc sidecar. The server registers a temporary
  stream in go2rtc, then exchanges a non-trickle SDP offer/answer
  through go2rtc's HTTP API (kiosk waits for ICE gathering to
  complete before sending the offer; candidates are bundled in the
  SDP). Same `stop_streaming` ends it. Also exposed via MQTT topic
  `hal/<id>/rtsp/set` (URL or JSON `{"url":"...","duration_s":N}`)
  and the HA Discovery text entity `text.<id>_stream_rtsp`.
- **HTTP video / HLS** — `play_video(url, duration_s, loop, muted)`
  plays an MP4 / WebM / HLS playlist directly in the kiosk's
  `<video>` element. No server-side fetching, no transcoding — the
  browser handles playback. HLS is detected by `.m3u8` and routed
  through hls.js; everything else uses native `<video src>`. Audio
  plays by default and **auto-ducks while HAL is speaking**, then
  restores the user's muted preference when HAL goes back to idle.
  Optional `loop=true` for endless playback; optional `duration_s`
  for a hard cap (otherwise playback ends naturally on the video's
  end event). Mutually exclusive with all other orb modes. MQTT
  topic `hal/<id>/video/set` (URL or JSON
  `{"url":"...","loop":true,"muted":false,"duration_s":N}`); HA
  Discovery text entity `text.<id>_play_video`.
- **Arbitrary image push** — `show_image(url, duration_s=60)` from
  the LLM, **or** publish to MQTT `hal/<id>/image/set`, **or** write
  to the `text.<id>_show_image` HA entity. The MQTT topic accepts
  binary JPEG/PNG/GIF/WebP, a plain URL, or a JSON wrapper:
  `{"url": "...", "duration_s": 90}` or
  `{"image": "<base64>", "mime": "image/png", "duration_s": 30}`.
  Server-side URL fetcher caps responses at 8 MB and 10 s, refuses
  non-image content types, and auto-attaches `HA_TOKEN` when the URL
  starts with `HA_URL` (so `/local/` and `/api/camera_proxy/` work
  out of the box). Default duration 60 s, configurable per call,
  bounds 5–600 s.

Streaming requires HA 2024.11+ (built-in go2rtc) and a Long-Lived
Access Token in `HA_TOKEN` plus `HA_URL`. The `show_image` URL
fetcher uses the same token only when the URL targets HA.

Example HA automation pushing an image to HAL via the text helper:

```yaml
service: text.set_value
target:
  entity_id: text.hal_default_show_image
data:
  value: 'https://homeassistant.local:8123/local/dinner.jpg'
```

### Sendspin (multi-room audio)

A separate sidecar container registers a [Sendspin](https://www.music-assistant.io/player-support/sendspin/) player in Music Assistant named `${HAL_DEVICE_NAME} Speaker`. Channel mode (`Stereo` / `Left only` / `Right only` / `Mono`) is configured in MA's player settings — set `Mono` for the Anker since it's effectively a mono device.

**One-time host setup** — append this to `~/.config/pulse/default.pa` on the Pi:

```
load-module module-role-ducking trigger_roles=phone ducking_roles=music volume=-25dB
```

Then `systemctl --user restart pulseaudio`. HAL TTS streams (tagged `media.role=phone`) trigger ducking; music streams (`media.role=music`) duck by 25 dB while HAL speaks and resume automatically.

Set `SENDSPIN_PLAYER_ENTITY=media_player.hal_speaker` (or your actual entity) on the *server* `.env` to enable hardware-volume-button redirection (buttons drive MA when music is playing) and the optional Shape C explicit pause/resume (`SENDSPIN_PAUSE_DURING_TTS=true`). Shape C only resumes if MA was actually playing when HAL spoke — manual user pauses are never overridden.

**Deploying the sidecar** — on the Pi:

```bash
git pull
docker compose -f docker-compose.rpi-ghcr.yml pull
docker compose -f docker-compose.rpi-ghcr.yml up -d
docker compose -f docker-compose.rpi-ghcr.yml logs -f sendspin
```

The MA player should appear within ~30s via mDNS. Set channel mode in MA, then set `SENDSPIN_PLAYER_ENTITY` on the server and restart that stack.

See `rpi/sendspin/README.md` for details.

## Prerequisites

| Component | Requirement |
|---|---|
| AI Server | Linux machine with NVIDIA GPU, Docker with nvidia-container-toolkit |
| Raspberry Pi | Raspberry Pi 4/5 with Docker, USB speakerphone (e.g., Anker Powerconf S330) |
| Ollama | Running on the AI server host with a tool-calling model |
| Home Assistant | Running instance with a configured MCP server exposed over HTTP(S) |
| Wyoming TTS | A Wyoming-protocol TTS service (e.g., [wyoming-piper](https://github.com/rhasspy/wyoming-piper)) |
| MQTT broker (optional) | Any MQTT broker reachable from the AI server (Mosquitto, EMQX, HA add-on…) |
| Music Assistant (optional) | Required only if you use the Sendspin sidecar |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/moimart/conversation-hass.git
cd conversation-hass
cp .env.example .env
```

Edit `.env` — at minimum:

```ini
AI_SERVER_HOST=10.20.30.185       # IP of your GPU server
RPI_HOST=10.20.30.180             # IP of your Raspberry Pi
MCP_SERVER_URL=https://your-ha-mcp-server/endpoint
WAKE_WORD=hey homie               # leave empty for always-on
OLLAMA_HOST=http://10.20.30.185:11434
OLLAMA_MODEL=gpt-oss:20b
WYOMING_TTS_HOST=10.20.30.185
WYOMING_TTS_PORT=10300
STT_ENGINE=whisper
```

Optional integrations are commented in `.env.example` — uncomment as needed:
- `MQTT_BROKER_HOST=…` to expose HAL as a HA device
- `SENDSPIN_PLAYER_ENTITY=…` once the MA player appears, to enable button/pause coordination

### 2. Configure MCP servers

Edit `server/mcp_servers.json` to point at your Home Assistant MCP endpoint (and any other MCP servers you want to mount). The server merges tools from all configured servers and routes calls automatically:

```json
[
  { "name": "home-assistant", "url": "https://your-ha-mcp-server/endpoint" }
]
```

### 3. Start the AI server

**Option A — Build locally:**
```bash
docker compose -f docker-compose.server.yml up --build -d
```

**Option B — Use pre-built image from GHCR:**
```bash
docker compose -f docker-compose.server-ghcr.yml up -d
```

This starts:
- `hal-ai-server` — FastAPI WebSocket server on port **8765**
- `hal-shodh-memory` — Long-term memory service on port **3030**

Ollama must already be running on the host. STT models are cached in a Docker volume (`huggingface-cache`). Shodh Memory data persists in `shodh-data`.

### 4. Start the Raspberry Pi

**Option A — Build locally:**
```bash
docker compose -f docker-compose.rpi.yml up --build -d
```

**Option B — Use pre-built arm64 images from GHCR:**
```bash
docker compose -f docker-compose.rpi-ghcr.yml up -d
```

This starts:
- `hal-audio-streamer` — Mic capture, TTS playback, web UI on port **8080**
- `hal-sendspin` — Music Assistant multi-room audio player (host networking, port **8927**)
- `hal-go2rtc` — RTSP→WebRTC bridge sidecar for `stream_rtsp` (host networking, port **1984**)

The audio streamer auto-detects the USB speakerphone, probes its native sample rate and channel count, and resamples to 16kHz mono with FIR anti-aliasing for the AI server. TTS playback is upsampled to the device's native rate.

### 5. Open the web UI

Navigate to `http://<rpi-ip>:8080` in a browser, ideally as a kiosk on a monitor attached to the Pi.

For the HA camera entity to receive screenshots, launch the kiosk Chromium with the DevTools Protocol enabled:

```
chromium-browser --kiosk http://localhost:8080 \
  --autoplay-policy=no-user-gesture-required \
  --remote-debugging-port=9222
```

The audio_streamer container runs on host networking and reaches the kiosk Chromium via `127.0.0.1:9222`, then uploads a JPEG to the AI server every `SNAPSHOT_INTERVAL_S` seconds (default 60). Recent Chromium silently ignores `--remote-debugging-address` values other than `127.0.0.1`, which is why we use host networking instead of trying to bridge across `docker0`.

### 6. Desktop Command App (optional)

A lightweight Rust/GTK4 overlay for Hyprland/Wayland that sends text commands directly to HAL from your Linux desktop — bypassing speech entirely.

```bash
cd desktop
./install.sh
```

This builds the binary, copies it to `~/.local/bin/hal-command`, and installs default config/CSS to `~/.config/hal-command/`.

**Add Hyprland keybind** (in `~/.config/hypr/hyprland.conf`):
```
bind = SUPER, H, exec, hal-command
```

Press `SUPER+H` to open. Type a command. `Enter` sends (green tick on success, auto-dismisses). `ESC` cancels. Configure the server URL and styling in `~/.config/hal-command/{config.toml,style.css}`.

### 7. OpenClaw skill (optional)

`openclaw-skill/hal/` exposes HAL's REST API as an OpenClaw skill — drop it into any OpenClaw agent and it can speak through HAL, adjust volume, toggle mute, and switch themes via curl. Set `HAL_SERVER_URL` in the agent's config and the skill is ready.

## Pre-built Images

| Image | Platform | Purpose |
|---|---|---|
| `ghcr.io/moimart/conversation-hass/hal-ai-server:latest` | linux/amd64 | FastAPI server, STT, MCP routing, MQTT bridge |
| `ghcr.io/moimart/conversation-hass/hal-rpi:latest` | linux/arm64 | Pi audio streamer + web UI |
| `ghcr.io/moimart/conversation-hass/hal-sendspin:latest` | linux/arm64 | Sendspin daemon for Music Assistant |

## Project Structure

```
conversation-hass/
├── docker-compose.server.yml          # AI server (build locally)
├── docker-compose.server-ghcr.yml     # AI server (pre-built image)
├── docker-compose.rpi.yml             # RPi: audio_streamer + sendspin (build locally)
├── docker-compose.rpi-ghcr.yml        # RPi: audio_streamer + sendspin (pre-built)
├── .env.example                       # Configuration template
├── setup.sh                           # Interactive setup script
│
├── server/
│   ├── Dockerfile                     # NVIDIA CUDA + NeMo + Whisper
│   ├── requirements.txt               # faster-whisper, nemo_toolkit, silero-vad, mcp, aiomqtt
│   ├── system_prompt.txt              # LLM personality (mounted read-only)
│   ├── mcp_servers.json               # MCP server list (multi-server support)
│   └── app/
│       ├── main.py                    # FastAPI server, WebSocket + REST endpoints
│       ├── audio_pipeline.py          # VAD + STT engine selection + speaker filter
│       ├── transcriber.py             # WhisperTranscriber + NemotronTranscriber
│       ├── speaker_filter.py          # Voice embedding comparison (resemblyzer)
│       ├── conversation.py            # Wake word, MCP tool-calling, follow-up window
│       ├── memory.py                  # Shodh Memory client (remember/recall)
│       ├── mcp_client.py              # MCP client + MultiMCPClient (merge tools)
│       ├── local_tools.py             # In-process MCP server: theme/volume/mute/sun/speak
│       ├── ha_ws.py                   # HA WebSocket client (WebRTC offer signaling)
│       ├── go2rtc.py                  # go2rtc HTTP client (stream registration + WebRTC offer)
│       ├── mqtt_bridge.py             # HA Discovery + MQTT state/command bridge
│       └── tts.py                     # Wyoming protocol TTS client
│
├── rpi/
│   ├── Dockerfile                     # Python 3.11 slim + PulseAudio + ALSA
│   ├── audio_streamer/
│   │   ├── main.py                    # Mic capture, FIR resample, TTS playback,
│   │   │                              # CDP snapshot loop, music-state hook, HID buttons
│   │   └── cdp_snapshot.py            # Chrome DevTools Protocol screenshot client
│   ├── web/
│   │   ├── index.html                 # HAL 9000 UI (4 themes)
│   │   ├── style.css                  # Animations, theme variants
│   │   └── app.js                     # WebSocket client (orb / overlays / video)
│   └── sendspin/
│       ├── Dockerfile                 # Python 3.12 slim + sendspin daemon
│       ├── entrypoint.sh              # Daemon launcher with MA hooks
│       └── README.md                  # Pulse ducking + channel-mode docs
│
├── desktop/                           # Rust/GTK4 desktop command app
│   ├── Cargo.toml                     # GTK4, layer-shell, reqwest
│   ├── src/main.rs                    # Wayland overlay, sends to /api/command
│   ├── config/                        # Default config.toml + style.css
│   └── install.sh                     # Build + install to ~/.local/bin
│
├── openclaw-skill/                    # OpenClaw skill exposing HAL's REST API
│   └── hal/SKILL.md
│
└── tests/                             # 179 pytest tests (mocked, no GPU needed)
    ├── test_audio_manager.py
    ├── test_audio_pipeline.py
    ├── test_conversation.py
    ├── test_mcp_client.py
    ├── test_server_main.py
    ├── test_speaker_filter.py
    ├── test_transcriber.py
    └── test_tts.py
```

## Configuration Reference

### Network

| Variable | Default | Description |
|---|---|---|
| `AI_SERVER_HOST` | — | IP of the AI server (used by RPi to connect) |
| `RPI_HOST` | — | IP of the RPi (informational; not consumed by code) |
| `WEB_PORT` | `8080` | Port for the RPi web UI |
| `CHROMIUM_DEBUG_URL` | `http://127.0.0.1:9222` | Kiosk Chromium's DevTools endpoint (audio_streamer reaches it for screenshots; container runs on host networking) |
| `SNAPSHOT_INTERVAL_S` | `60` | Seconds between CDP screenshots posted to the AI server |

### Speech & LLM

| Variable | Default | Description |
|---|---|---|
| `WAKE_WORD` | `hey hal` | Phrase that activates command processing. Empty = always-on |
| `STT_ENGINE` | `whisper` | `whisper` or `nemotron` |
| `STT_MODEL` | (auto) | Defaults: whisper=`large-v3-turbo`, nemotron=`nvidia/parakeet-tdt-0.6b-v2` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.2` | LLM model name (must support tool calling) |
| `OLLAMA_NUM_CTX` | `32768` | LLM context window in tokens |
| `OLLAMA_NUM_PREDICT` | `512` | Max tokens per LLM response |
| `WYOMING_TTS_HOST` | `localhost` | Wyoming TTS host |
| `WYOMING_TTS_PORT` | `10200` | Wyoming TTS port |
| `WYOMING_TTS_VOICE` | (server default) | TTS voice; empty = service default |
| `SYSTEM_PROMPT_FILE` | `/app/system_prompt.txt` | LLM system prompt path inside the container |

### MCP & Memory

| Variable | Default | Description |
|---|---|---|
| `MCP_SERVERS_FILE` | `/app/mcp_servers.json` | Path to MCP server list inside the container |
| `MCP_SERVER_URL` | — | Single-server fallback if `mcp_servers.json` is absent |
| `MEMORY_URL` | `http://shodh-memory:3030` | Shodh Memory service URL |
| `MEMORY_USER_ID` | `hal-default` | User ID for memory isolation |
| `MEMORY_API_KEY` | — | Shodh API key (if your instance requires auth) |

### Home Assistant WebSocket (live camera streaming)

`stream_camera` opens a WebRTC peer connection against HA's built-in
`camera/webrtc/offer` subscription. The server holds the credentials
and proxies SDP/ICE between kiosk and HA — the kiosk never sees the
HA URL or token.

| Variable | Default | Description |
|---|---|---|
| `HA_URL` | — | HA base URL, e.g. `http://homeassistant.local:8123` (leave empty to disable streaming) |
| `HA_TOKEN` | — | HA Long-Lived Access Token (User profile → Long-Lived Access Tokens) |

### go2rtc sidecar (arbitrary RTSP streaming)

`stream_rtsp` registers an RTSP source in the bundled go2rtc service
and exchanges a WebRTC offer/answer through it. Compose ships a
go2rtc container on host networking (so its ICE candidates contain
the host's LAN IPs, reachable from the kiosk).

| Variable | Default | Description |
|---|---|---|
| `GO2RTC_URL` | `http://host.docker.internal:1984` | go2rtc HTTP/WS endpoint reachable from the AI server container |

### Audio (RPi)

| Variable | Default | Description |
|---|---|---|
| `AUDIO_DEVICE` | `default` | ALSA audio device (auto-detects USB speakerphones) |
| `SAMPLE_RATE` | `16000` | Target audio sample rate in Hz |
| `CHANNELS` | `1` | Target audio channels |
| `CHUNK_SIZE` | `4096` | Audio buffer size per WebSocket frame |

### Themes

| Variable | Default | Description |
|---|---|---|
| `AUTO_THEME` | `true` | Auto-switch themes at dusk/dawn via `sun.sun` |
| `THEME_DAY` | `birch` | Theme used when sun is above horizon |
| `THEME_NIGHT` | `dark` | Theme used when sun is below horizon |

### MQTT (HA auto-discovery)

| Variable | Default | Description |
|---|---|---|
| `MQTT_BROKER_HOST` | (empty = disabled) | MQTT broker hostname/IP |
| `MQTT_BROKER_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | — | Broker username (optional) |
| `MQTT_PASSWORD` | — | Broker password (optional) |
| `HAL_DEVICE_ID` | `hal-default` | MQTT object id (slug) |
| `HAL_DEVICE_NAME` | `HAL` | Display name in HA |

### Sendspin (multi-room audio)

| Variable | Default | Description |
|---|---|---|
| `SENDSPIN_PLAYER_ENTITY` | (empty = disabled) | MA `media_player.*` entity_id, enables button redirection / Shape C |
| `SENDSPIN_PAUSE_DURING_TTS` | `false` | Shape C: explicit pause/resume around TTS (only resumes if MA was playing) |
| `SENDSPIN_LOG_LEVEL` | `INFO` | Sendspin daemon log level |

## API Endpoints

### AI Server (`hal-ai-server:8765`)

| Endpoint | Protocol | Description |
|---|---|---|
| `/ws/audio` | WebSocket | Main audio channel (RPi). PCM in, transcription/TTS out, JSON control msgs |
| `/ws/ui` | WebSocket | UI channel for live transcription and state |
| `/api/command` | HTTP POST | Direct text command to LLM. Body `{"text": "..."}` |
| `/api/speak` | HTTP POST | Speak verbatim text via TTS. Body `{"text": "..."}` |
| `/api/volume` | HTTP POST | Set TTS volume. Body `{"level": 0.0..1.0}` |
| `/api/mute` | HTTP POST/GET | Toggle/get mic mute |
| `/api/snapshot` | HTTP POST | Receive a JPEG snapshot from the kiosk page |
| `/api/snapshot.jpg` | HTTP GET | Latest cached JPEG (also published on MQTT camera) |
| `/health` | HTTP GET | Health check: pipeline, MCP, TTS, memory |

### Audio Streamer (`hal-audio-streamer:8080`)

| Endpoint | Protocol | Description |
|---|---|---|
| `/` | HTTP GET | Web UI |
| `/ws` | WebSocket | UI client channel (state/transcription/theme) |
| `/api/snapshot` | HTTP POST | Snapshot proxy → forwards JPEG to AI server |
| `/api/music/state` | HTTP POST | Sendspin daemon hook → updates music-playing flag |

### WebSocket Message Types

**Server → RPi (`/ws/audio`):**
```jsonc
{"type": "transcription", "text": "...", "is_partial": false, "speaker": "human"}
{"type": "response", "text": "..."}          // LLM response text
{"type": "wake"}                             // Wake word detected (UI flash + chime)
{"type": "chime_start", "size": 13230}       // Wake chime audio incoming
{"type": "chime_end"}                        // Wake chime complete
{"type": "tts_start", "size": 48000}         // TTS audio incoming
{"type": "tts_end"}                          // TTS audio complete
{"type": "state", "state": "listening"}      // idle | listening | processing | speaking
{"type": "set_theme", "name": "birch"}       // Server-driven theme change
{"type": "volume_adjust", "step": 0.1}       // Adjust TTS volume
{"type": "mute_toggle"}                      // Toggle mic mute
{"type": "mute_query"}                       // Ask current mute state
{"type": "show_camera", "image": "<b64>", "mime": "image/jpeg",
 "duration_s": 150, "entity_id": "camera.x"}  // Paint snapshot inside the orb
{"type": "stream_start", "session_id": "...",
 "entity_id": "camera.x"}                    // Begin live WebRTC stream
{"type": "stream_stop", "session_id": "..."} // End live stream
{"type": "webrtc_signal", "session_id": "...",
 "kind": "answer"|"candidate", ...}           // SDP/ICE forwarded from HA
{"type": "play_video", "url": "https://...",
 "loop": false, "muted": false,
 "duration_s": 60}                            // Play HTTP video (MP4/WebM/HLS)
{"type": "video_stop"}                        // Stop any active HTTP video
```

**RPi → Server (`/ws/audio`):**
```jsonc
// Binary: raw PCM 16-bit LE audio chunks
{"type": "tts_finished"}                     // RPi finished playing audio
{"type": "ping"} / {"type": "pong"}          // Keepalive
{"type": "mute_sync", "muted": true}         // RPi reports mute state change
{"type": "volume_sync", "level": 0.7}        // RPi reports volume change
{"type": "ma_volume_adjust", "step": 0.1}    // Forward HID button to MA player
{"type": "webrtc_signal", "session_id": "...",
 "kind": "offer"|"candidate", ...}            // Kiosk-side SDP/ICE for stream
```

## Customizing the System Prompt

Edit `server/system_prompt.txt` to change HAL's personality. The file is mounted read-only into the container — no rebuild needed, just restart. Tool definitions are passed to Ollama separately via its native tool-calling API.

## Running Tests

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements-test.txt
pytest tests/ -v
```

All 251 tests run without GPU, ML models, or external services — dependencies (HA WS, MCP servers, TTS, audio devices) are fully mocked.

## License

MIT
