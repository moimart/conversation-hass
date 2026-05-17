# HAL — Architecture

This document describes how the pieces of HAL fit together: the two
nodes, the audio + LLM pipeline, the streaming surfaces, the
self-healing logic, and the file/module layout.

For the public-facing surface (REST + MQTT) see [`API.md`](./API.md)
and [`MQTT.md`](./MQTT.md). For theme authoring see
[`THEMES.md`](./THEMES.md).

---

## Table of contents

- [Two-node layout](#two-node-layout)
- [Pipeline walkthrough](#pipeline-walkthrough)
- [Speech-to-Text engines](#speech-to-text-engines)
- [Push-to-Talk](#push-to-talk)
- [Camera / video / image in the orb](#camera--video--image-in-the-orb)
- [Live runtime config](#live-runtime-config)
- [Sendspin (multi-room audio)](#sendspin-multi-room-audio)
- [Self-healing behaviour](#self-healing-behaviour)
- [Project structure](#project-structure)

---

## Two-node layout

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
│  │  │  (14 themes,           │  │  + wake flash│  │             │               │    │  │
│  │  │   auto day/night)      │  │              │  │  ┌──────────▼────────────┐  │    │  │
│  │  │  Live Transcription    │  │  + JPEG      │  │  │ Conversation Manager  │  │    │  │
│  │  │  Response Display      │  │   snapshots  │  │  │                       │  │    │  │
│  │  │  Calendar overlay      │  │              │  │  │  Wake word + chime    │  │    │  │
│  │  └────────────────────────┘  │              │  │  │  Push-to-Talk         │  │    │  │
│  │                              │              │  │  │  Follow-up window     │  │    │  │
│  │  ┌────────────────────────┐  │              │  │  └─┬────────────┬───┬────┘  │    │  │
│  │  │   Sendspin sidecar     │  │              │  │    │            │   │       │    │  │
│  │  │   (Music Assistant     │  │              │  │  ┌─▼─────┐ ┌───▼───▼────┐  │    │  │
│  │  │    multi-room player)  │  │              │  │  │Ollama │ │ Wyoming    │  │    │  │
│  │  │   + Pulse role-ducking │  │              │  │  │LLM    │ │ TTS        │  │    │  │
│  │  └────────────────────────┘  │              │  │  │(tools)│ └────────────┘  │    │  │
│  │                              │              │  │  └───┬───┘                 │    │  │
│  └──────────────────────────────┘              │  │      │ MCP + LocalTools    │    │  │
│                                                │  │  ┌───▼──────────────────┐  │    │  │
│                                                │  │  │ Multi-MCP Client     │──┼────┼──► HA MCP Server(s)
│                                                │  │  └──────────────────────┘  │    │  │
│                                                │  └──────────────────────────────┘   │  │
│                                                │                                      │  │
│                                                │  ┌──────────────────────────────┐    │  │
│                                                │  │  MQTT Bridge                 │    │  │
│                                                │  │  HA auto-discovery → state,  │────┼──► MQTT broker → HA
│                                                │  │  volume, mute, theme, cam,   │    │  │
│                                                │  │  speak text, PTT             │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                │  ┌──────────────────────────────┐    │  │
│                                                │  │  Shodh Memory (:3030)        │    │  │
│                                                │  │  Hebbian long-term memory    │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

The Raspberry Pi handles all I/O — microphone capture, speaker
playback, the kiosk UI. The AI server does everything else: speech
recognition, language understanding, tool calls, speech synthesis,
memory, MQTT.

The only persistent connection between them is a single WebSocket
(`/ws/audio`) carrying raw PCM upstream and TTS audio + JSON control
messages downstream.

---

## Pipeline walkthrough

1. **Continuous listening** — The Pi captures audio from the Anker
   PowerConf S330 USB speakerphone (48kHz stereo, FIR anti-alias
   filtered and downmixed to 16kHz mono) and streams raw PCM over
   WebSocket to the AI server.

2. **Always-on transcription** — The AI server runs Silero VAD
   (voice activity detection) and a configurable STT engine to
   transcribe everything said in the room. All transcriptions appear
   live on the web UI regardless of whether a command was issued.

3. **Speaker identification** — Resemblyzer voice embeddings
   distinguish human speakers from the AI's own TTS output, preventing
   the assistant from responding to itself.

4. **Wake word or always-on or push-to-talk** — Three modes:
   - **Wake word** (default): Transcribed text is monitored for the
     wake word. Only after detecting it does the system engage the
     LLM. A two-tone chime plays and the HAL eye flashes white as
     confirmation. A 10-second follow-up window allows natural
     back-and-forth without repeating the wake word.
   - **Always-on**: Set `WAKE_WORD=` empty to process every
     transcribed line through the LLM.
   - **Push-to-Talk**: External apps / hardware open a session via
     HTTP / MQTT / WebSocket; the session bypasses the wake word
     and runs the LLM on whatever was captured. See
     [Push-to-Talk](#push-to-talk) below.

5. **LLM with multi-MCP tool calling** — The user's command goes to
   Ollama (configurable context window, defaults to 32k) with tool
   definitions discovered at startup from one or more MCP servers
   (typically Home Assistant, plus a built-in `LocalTools` server
   that exposes `ui_set_theme`, `audio_set_volume`,
   `audio_toggle_mute`, `get_sun_times`, `speak_verbatim`,
   `show_camera`, `show_image`, `stream_camera`, `stream_rtsp`,
   `play_video`, `stop_streaming`, `show_calendar`, `hide_calendar`).
   The LLM searches for entities by friendly name, then calls HA
   services with the correct entity IDs. The system prompt lives in
   `server/system_prompt.txt` — edit it to customise HAL's
   personality.

6. **Long-term memory** — Before each LLM call, relevant memories
   are recalled from [Shodh Memory](https://www.shodh-memory.com/)
   and injected into the prompt as context. After each exchange, the
   conversation is stored as a new memory. Shodh uses Hebbian
   learning and natural decay, so frequently referenced facts
   strengthen over time while irrelevant ones fade — like biological
   memory.

7. **Local TTS** — The response is synthesised via a Wyoming-protocol
   TTS service and streamed back to the Raspberry Pi for playback
   through the speaker (resampled to the device's native rate).

8. **Web UI with themes** — A modern HAL 9000-inspired interface
   with metallic bezel ring, animated eye, live transcription, AI
   responses, and state indicators. Fourteen themes spanning the
   AI-pantheon (`dark`/`sal`/`glados`/`mother`/`joi`/`kitt`/
   `cyberpunk`/`matrix`) and ambient aesthetics
   (`birch`/`odyssey`/`japandi`/`forest`/`sunset`/`material_you`);
   optional auto day/night switching driven by HA's `sun.sun`
   entity. See [`THEMES.md`](./THEMES.md) for authoring docs.

9. **Home Assistant integration** — When MQTT is configured, the AI
   server publishes HA Discovery messages so HAL appears as a single
   device exposing state, volume, mute, theme, a camera (the latest
   UI snapshot), text inputs that speak/command, PTT buttons,
   calendar buttons, and live runtime-config selectors. The
   audio_streamer captures the kiosk via the Chrome DevTools Protocol
   against the running kiosk Chromium and forwards a JPEG to the
   server every minute (live video frames, custom fonts, animations,
   masks, and filters all included — exact pixels, no html2canvas
   approximations).

10. **Multi-room audio** (optional) — A Sendspin sidecar on the Pi
    registers an [Open Home Foundation](https://www.openhomefoundation.org/)
    multi-room player in Music Assistant. HAL TTS and Sendspin music
    share the same PulseAudio socket; HA announcements duck the
    music via `module-role-ducking` while HAL speaks. Hardware
    volume buttons on the Anker target MA when music is playing and
    HAL TTS otherwise. See [Sendspin](#sendspin-multi-room-audio).

11. **Camera in the orb** — Ask HAL to show or stream any HA camera
    and it appears inside the eye (filling the area up to the
    metallic rim, with the bezel and crystal highlights still on
    top). See [Camera / video / image in the orb](#camera--video--image-in-the-orb).

---

## Speech-to-Text engines

Two STT engines are available, selectable at runtime via the
`STT_ENGINE` environment variable:

| Engine               | Models                                                                | Speed       | Best for                                  |
|----------------------|-----------------------------------------------------------------------|-------------|-------------------------------------------|
| `whisper` (default)  | `large-v3-turbo`, `large-v3`, `medium`, `base`                        | Baseline    | Accuracy, multilingual, noisy environments|
| `nemotron`           | `nvidia/parakeet-tdt-0.6b-v2`, `nvidia/parakeet-ctc-1.1b`             | ~21x faster | Real-time streaming, low latency          |

```ini
# .env — switch to Nemotron
STT_ENGINE=nemotron
STT_MODEL=nvidia/parakeet-tdt-0.6b-v2
```

Both engines are included in the Docker image and share the same
interface. Models are cached in a Docker volume across restarts.

The pipeline also runs a **warm-up pass** at startup (one dummy
inference on 1 s of silence through STT + VAD + speaker filter) so
the first real user request doesn't pay the 1-2 s cold-start tax for
CUDA kernel JIT and cuDNN autotuning.

---

## Push-to-Talk

PTT opens a session that bypasses the wake-word check and runs the
LLM on the captured audio when the session ends. Three trigger
surfaces — all share one server function:

- **HTTP** `POST /api/ptt/{start,end,cancel}` — see [`API.md`](./API.md#push-to-talk)
- **WebSocket** `/ws/ptt` — JSON `{type:"start"|"end"|"cancel"}`
- **MQTT** `hal/<device>/ptt/{start,end,cancel}` — see [`MQTT.md`](./MQTT.md#push-to-talk)

The session is owned by the **first** trigger to send `start`; the
first `end` (or 20 s safety timeout) closes it. While a session is
open:

- If TTS is playing, it's cancelled mid-stream (PyAudio write loop
  honours a flag set by `tts_cancel`).
- If the mic was muted at rest, it's auto-unmuted (and restored on
  end).
- `conversation._wake_detected` is flipped so STT output flows into
  the command buffer.
- The kiosk receives `ptt_active=true` so the PTT chip + circular
  orb aura show up.

On end, `audio_pipeline.force_finalize()` flushes whatever the VAD
has been buffering through STT immediately (no 1.5 s silence wait),
then `conversation.on_silence()` runs the LLM exactly like a
wake-word turn. If `end` arrives less than 100 ms after `start` it's
treated as a debounce bounce — buffer is dropped, no LLM call.

Source: `server/app/ptt.py`.

---

## Camera / video / image in the orb

HAL can paint any image — HA camera snapshot, live WebRTC stream, or
arbitrary URL — inside the eye, filling the area up to the metallic
rim while keeping the bezel and crystal highlights on top. Five
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
  takes any RTSP URL (with optional inline credentials) and streams
  it via the bundled go2rtc sidecar. The server registers a
  temporary stream in go2rtc, then exchanges a non-trickle SDP
  offer/answer through go2rtc's HTTP API (kiosk waits for ICE
  gathering to complete before sending the offer; candidates are
  bundled in the SDP). Same `stop_streaming` ends it.

- **HTTP video / HLS** — `play_video(url, duration_s, loop, muted)`
  plays an MP4 / WebM / HLS playlist directly in the kiosk's
  `<video>` element. No server-side fetching, no transcoding — the
  browser handles playback. HLS is detected by `.m3u8` and routed
  through hls.js; everything else uses native `<video src>`. Audio
  plays by default and **auto-ducks while HAL is speaking**, then
  restores the user's muted preference when HAL goes back to idle.

- **Arbitrary image push** — `show_image(url, duration_s=60)` from
  the LLM, or publish to MQTT `hal/<id>/image/set` (URL, binary
  JPEG/PNG/GIF/WebP, or JSON wrapper), or write to the
  `text.<id>_show_image` HA entity. Server-side URL fetcher caps
  responses at 8 MB and 10 s, refuses non-image content types, and
  auto-attaches `HA_TOKEN` when the URL starts with `HA_URL`.

Streaming requires HA 2024.11+ (built-in go2rtc) and a Long-Lived
Access Token in `HA_TOKEN` plus `HA_URL`. See [`API.md`](./API.md)
and [`MQTT.md`](./MQTT.md) for the exact endpoints.

---

## Live runtime config

A handful of settings can be changed from HA without restarting the
server. They live in **`server/runtime/config.json`** (created on
first boot from the matching `.env` values). Once the file exists,
**the file wins over `.env`** — `.env` is only consulted to
bootstrap.

| Key                          | Type        | Bootstraps from         | HA control |
|------------------------------|-------------|-------------------------|------------|
| `theme_day`                  | string      | `THEME_DAY`             | select |
| `theme_night`                | string      | `THEME_NIGHT`           | select |
| `tts_voice`                  | string      | `WYOMING_TTS_VOICE`     | select (voices from Wyoming) |
| `wake_word`                  | string      | `WAKE_WORD`             | text |
| `ollama_model`               | string      | `OLLAMA_MODEL`          | select (models from Ollama `/api/tags`) |
| `auto_theme`                 | bool        | `AUTO_THEME`            | switch |
| `start_muted`                | bool        | `START_MUTED`           | switch |
| `calendar_default_source`    | string      | `CALENDAR_DEFAULT_SOURCE` | text |
| `calendar_dismiss_seconds`   | int (5-600) | `CALENDAR_DISMISS_SECONDS` (30) | number |

Changing any control from HA writes the file atomically, applies the
change live (theme reapplied if currently visible, TTS voice on the
next utterance, model on the next LLM round, etc.) and publishes the
new state back to MQTT so HA stays in sync.

To reset to env defaults: stop the server, delete the file, restart.

---

## Sendspin (multi-room audio)

A separate sidecar container on the Pi registers a
[Sendspin](https://www.music-assistant.io/player-support/sendspin/)
player in Music Assistant named `${HAL_DEVICE_NAME} Speaker`. Channel
mode is configured in MA's player settings — set `Mono` for the
Anker since it's effectively a mono device.

**One-time host setup** — append this to `~/.config/pulse/default.pa`
on the Pi:

```
load-module module-role-ducking trigger_roles=phone ducking_roles=music volume=-25dB
```

Then `systemctl --user restart pulseaudio`. HAL TTS streams (tagged
`media.role=phone`) trigger ducking; music streams (`media.role=music`)
duck by 25 dB while HAL speaks and resume automatically.

Set `SENDSPIN_PLAYER_ENTITY=media_player.hal_speaker` (or your actual
entity) on the *server* `.env` to enable hardware-volume-button
redirection (buttons drive MA when music is playing) and the optional
Shape C explicit pause/resume (`SENDSPIN_PAUSE_DURING_TTS=true`).
Shape C only resumes if MA was actually playing when HAL spoke —
manual user pauses are never overridden.

See `rpi/sendspin/README.md` for details.

---

## Self-healing behaviour

A few things keep the assistant resilient on real-world hardware:

- **STT/VAD self-reload** — If no successful transcription happens in
  `_vad_reset_interval` seconds (default 300), the pipeline reloads
  both models in an executor. Exponential backoff after the first
  reload (doubles, capped at 1 h). After a reload, the warm-up
  timestamp is updated so we don't immediately reload again.
- **VAD inference timeout** — Each VAD call has a 5 s hard ceiling.
  If it exceeds that we replace the executor (the old thread may be
  stuck forever) and reload the model.
- **STT transcribe timeout** — 15 s ceiling per call. After 3
  consecutive failures the executor is replaced and the model
  reloaded.
- **PortAudio device cache recovery** — On the RPi, if the Anker
  speaker drops out of PortAudio's stale device list during TTS,
  `play_tts_audio` reinits PyAudio (forcing a fresh enumeration),
  clears the cached output device/rate, and retries once.
- **Audio websocket keepalive** — Server pings the RPi every 15 s;
  3 missed pongs = 45 s timeout = disconnect.
- **MQTT retained state** — All `state` topics are retained, QoS 1.
  Bridge re-publishes every cached state on connect. LWT `offline`
  on unclean disconnect.

---

## Project structure

```
conversation-hass/
├── docker-compose.server.yml          # AI server (build locally)
├── docker-compose.server-ghcr.yml     # AI server (pre-built image)
├── docker-compose.rpi.yml             # RPi: audio_streamer + sendspin (build locally)
├── docker-compose.rpi-ghcr.yml        # RPi: audio_streamer + sendspin (pre-built)
├── .env.example                       # Configuration template
├── setup.sh                           # Interactive setup script
│
├── API.md, MQTT.md, THEMES.md, ARCHITECTURE.md, README.md
│
├── server/
│   ├── Dockerfile                     # NVIDIA CUDA + NeMo + Whisper
│   ├── requirements.txt               # faster-whisper, nemo_toolkit, silero-vad, mcp, aiomqtt
│   ├── system_prompt.txt              # LLM personality (mounted read-only)
│   ├── mcp_servers.json               # MCP server list (multi-server support)
│   ├── themes/                        # Plug-in theme folders (manifest+theme.css+optional effect.js)
│   └── app/
│       ├── main.py                    # FastAPI server, WebSocket + REST endpoints
│       ├── audio_pipeline.py          # VAD + STT engine selection + speaker filter
│       ├── transcriber.py             # WhisperTranscriber + NemotronTranscriber
│       ├── speaker_filter.py          # Voice embedding comparison (resemblyzer)
│       ├── conversation.py            # Wake word, MCP tool-calling, follow-up window
│       ├── ptt.py                     # Push-to-Talk session lifecycle
│       ├── calendar_ha.py             # HA REST calendar fetcher (60 s list cache)
│       ├── memory.py                  # Shodh Memory client (remember/recall)
│       ├── mcp_client.py              # MCP client + MultiMCPClient (merge tools)
│       ├── local_tools.py             # In-process MCP server: theme/volume/mute/sun/speak/etc.
│       ├── ha_ws.py                   # HA WebSocket client (WebRTC offer signaling)
│       ├── go2rtc.py                  # go2rtc HTTP client (stream registration + WebRTC offer)
│       ├── runtime_config.py          # File-backed live config (atomic JSON, env bootstrap)
│       ├── themes.py                  # Plug-in theme registry (scan + polling reload)
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
│   │   ├── index.html                 # HAL 9000 UI (cube wrapper for calendar overlay)
│   │   ├── style.css                  # Animations, theme variants, PTT chip + orb aura
│   │   ├── app.js                     # WebSocket client (orb / overlays / video / calendar)
│   │   ├── calendar.css               # Calendar overlay styling
│   │   └── calendar.js                # Month/week/day grid, WAAPI fade-swap
│   └── sendspin/
│       ├── Dockerfile                 # Python 3.12 slim + sendspin daemon
│       ├── entrypoint.sh              # Daemon launcher with MA hooks
│       └── README.md                  # Pulse ducking + channel-mode docs
│
├── desktop/                           # Rust/GTK4 desktop command app
│   ├── Cargo.toml                     # GTK4, layer-shell, reqwest
│   ├── src/main.rs                    # Wayland overlay; types commands; PTT hold-button
│   ├── config/                        # Default config.toml + style.css
│   └── install.sh                     # Build + install to ~/.local/bin
│
├── openclaw-skill/                    # OpenClaw skill exposing HAL's REST API
│   └── hal/SKILL.md
│
├── docs/themes/                       # README screenshot gallery (14 thumbnails)
│
└── tests/                             # pytest suite — mocked, no GPU needed
    ├── test_audio_manager.py
    ├── test_audio_pipeline.py
    ├── test_calendar_ha.py
    ├── test_conversation.py
    ├── test_mcp_client.py
    ├── test_ptt.py
    ├── test_runtime_config.py
    ├── test_server_main.py
    ├── test_speaker_filter.py
    ├── test_themes.py
    ├── test_transcriber.py
    └── test_tts.py
```

---

## See also

- [`API.md`](./API.md) — REST + WebSocket reference
- [`MQTT.md`](./MQTT.md) — MQTT topic + HA Discovery reference
- [`THEMES.md`](./THEMES.md) — Plug-in theme authoring
- [`README.md`](./README.md) — Setup + feature overview
