---
name: hal
description: Control PAL — local voice assistant for Home Assistant. Send spoken commands, adjust speaker volume, toggle mic mute, switch the web UI theme, drive push-to-talk, open or auto-trigger the photo frame, toggle the hub display (DPMS), and pop up the calendar.
metadata.openclaw.requires.bins: ["curl"]
metadata.openclaw.requires.config: ["HAL_SERVER_URL"]
---

# PAL Voice Assistant Control

You can control a self-hosted PAL voice assistant via its REST API. PAL is a 2001-style voice assistant that listens through a Raspberry Pi, runs Whisper STT + a local LLM (Ollama) with Home Assistant MCP tools, and replies through a Wyoming-protocol TTS service. The Raspberry Pi also serves a glowing-eye web UI showing live transcription and the assistant's state.

The base URL of the AI server is exposed as `$HAL_SERVER_URL` (set in OpenClaw config). All requests use JSON.

## MANDATORY: How to control Home Assistant and PAL hardware

When the user asks you to control lights, climate, sensors, or any
Home Assistant device, you MUST use the `exec` tool to run `mcporter`.
Do NOT answer without calling exec first. Do NOT make up a response
about having done something — actually call exec.

If you see a tool called "mcporter" in your tool list, IGNORE IT —
it will fail in this channel. Use `exec` instead.

**Always use this exact pattern with exec:**
```
mcporter call <server>.<tool> key=value key2=value2
```

### Available MCP servers

- **`berlinmcp`** — Full Home Assistant access: `ha_call_service`,
  `ha_get_state`, `ha_search_entities`, `ha_get_history`, and all
  other HA tools. Use this for lights, climate, sensors, automations,
  scripts, service calls, etc.
- **`hal`** — PAL hub controls: volume, mute, display power, photo
  frame, calendar, cameras, speak, and theme.

### Examples

```sh
# Turn on a light
mcporter call berlinmcp.ha_call_service domain=light service=turn_on entity_id=light.office

# Get entity state
mcporter call berlinmcp.ha_get_state entity_id=sun.sun

# Search for entities
mcporter call berlinmcp.ha_search_entities query=light domain=light

# Show photo frame on PAL hub
mcporter call hal.show_photo_frame

# Speak text on PAL hub
mcporter call hal.speak_verbatim text="Hello Master"
```

### Important

- Do NOT route HA tasks through PAL's `/api/command`.
- There is NO tool called "mcporter" — always use `exec` to run the
  `mcporter` CLI binary. If you get "tool mcporter not found," you
  forgot to use exec.

## Loop prevention

When OpenClaw IS the conversation engine for PAL (via the `hal` channel
plugin), do **NOT** call `/api/command` — that routes through the
conversation pipeline, which in this mode IS OpenClaw, creating an
infinite loop. Use `/api/speak` for verbatim TTS output, and use the
direct REST/MQTT endpoints below for orb display, volume, mute, etc.

## When to use this skill

- The user asks to "tell PAL", "ask PAL", "command PAL", or anything addressed to PAL
- The user asks to control the PAL UI (theme, volume, mute) without speaking
- The user wants PAL to do something on their smart home (lights, climate, scenes) — PAL has the Home Assistant MCP tools wired in, so just send the natural-language command via `/api/command`
- The user wants something shown inside PAL's orb (a camera snapshot, a live camera stream, an arbitrary image URL, an RTSP URL, an HTTP MP4/HLS video) — describe it in natural language to `/api/command` and PAL's LLM will pick the right tool

## What PAL can do (via /api/command)

When you POST a natural-language command to `/api/command`, PAL's LLM has these tools available — describe the intent and the LLM dispatches to the right one. You don't have to name the tool; just ask plainly.

**Smart home (via Home Assistant MCP):**
- Control devices: lights, climate, scenes, media players, scripts, automations, helpers
- Query state: temperatures, sensor readings, device status, history
- Anything Home Assistant exposes through its MCP server

**PAL UI / hardware:**
- Switch the hub theme: "switch the theme to japandi" (registry is dynamic; see Theme control for how to list available themes)
- Set or adjust speaker volume: "turn the volume up", "set volume to 30%"
- Toggle mic mute
- Make PAL speak text exactly: "say out loud: dinner is ready"
- Turn the hub display (panel) on or off via real DPMS: "turn off the screen", "wake the screen" (`set_display_power`)
- Auto-blank the display after N idle seconds (set via voice or REST; see Display power)
- Auto-activate the photo frame after N idle minutes: "auto-show the photo frame after 30 minutes" (`set_photo_frame_idle_minutes`)

**Orb display (image / video / camera / photo frame / calendar):**
- Snapshot from a Home Assistant camera: "show me the front door camera" — paints a JPEG inside PAL's orb for ~2.5 minutes (`show_camera`)
- Live WebRTC stream from a HA camera: "watch the kitchen camera live", "stream the porch" — opens a low-latency feed for up to 5 minutes (`stream_camera`)
- Live RTSP URL (any IP cam, NVR, Frigate go2rtc, etc.): "stream the rtsp at rtsp://user:pass@host/path" — uses the bundled go2rtc sidecar (`stream_rtsp`)
- Arbitrary image URL: "show the picture at https://example.com/x.jpg" or "put X on screen for 30 seconds" — fetches and displays for 60 s by default (`show_image`)
- HTTP video / HLS playlist: "play the video at https://example.com/clip.mp4" or "play this looping silently: <url>" — auto-stops on end of file unless `loop=true`; auto-ducks audio when PAL speaks (`play_video`)
- Stop any active orb display: "stop streaming", "stop the video", "don't show the camera anymore" (`stop_streaming` clears webrtc + video)
- Show the photo frame (a configured HA `image.*` / `camera.*` entity, Ken-Burns + clock): "show the photo frame", "open the slideshow" (`show_photo_frame`); dismiss: "hide the photo frame", "stop the slideshow" (`hide_photo_frame`)
- Pop up the calendar overlay: "show my calendar for this week", "what's on the calendar tomorrow", "open the month view" (`show_calendar`); dismiss: "hide the calendar" (`hide_calendar`)

The orb shows one thing at a time — starting any new display replaces whatever's there. The photo frame and calendar each preempt other displays the same way.

## Send a spoken command to the LLM

Use this for ANY request that should run through PAL's LLM, including HA control commands like "turn on the kitchen lights". The text appears on PAL's web UI as a transcription, the LLM processes it (with all its MCP tools available), and PAL speaks the response through the Raspberry Pi.

```sh
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "<command>"}'
```

Examples:
- `{"text": "turn on the table lamp"}`
- `{"text": "what's the temperature in the bedroom?"}`
- `{"text": "play some jazz on the living room speaker"}`
- `{"text": "show me the front door camera"}`
- `{"text": "stream the kitchen camera live"}`
- `{"text": "stream the rtsp at rtsp://admin:pass@10.0.0.20:554/stream1"}`
- `{"text": "put the picture at https://example.com/cat.jpg on screen for 2 minutes"}`
- `{"text": "play https://example.com/clip.mp4 muted"}`
- `{"text": "stop streaming"}`
- `{"text": "show my calendar for this week"}`
- `{"text": "show the photo frame"}`
- `{"text": "turn off the screen"}`
- `{"text": "auto-show the photo frame after 30 minutes"}`

## Speak text out loud verbatim (bypass the LLM)

When you (the agent) want PAL to say a specific message exactly as written —
notifications, announcements, status reports — use `/api/speak`. This
bypasses the LLM entirely: the text is sent straight to TTS and played on the
Raspberry Pi speaker. No persona transformation, no paraphrasing.

```sh
curl -sS -X POST "$HAL_SERVER_URL/api/speak" \
  -H "Content-Type: application/json" \
  -d '{"text": "<exact words to speak>"}'
```

Examples:
- `{"text": "Master, your laundry cycle just finished."}`
- `{"text": "Heads up: the front door has been open for 5 minutes."}`
- `{"text": "Build complete. All tests passed."}`

Use this when the wording matters and you don't want PAL's witty butler
persona to rewrite it. Use `/api/command` instead when you want PAL to
process the request through its LLM (e.g., to control the home).

## Volume control

Adjust the Raspberry Pi speaker volume by ±10%:

```sh
# Volume up
curl -sS -X POST "$HAL_SERVER_URL/api/volume" \
  -H "Content-Type: application/json" \
  -d '{"direction": "up"}'

# Volume down
curl -sS -X POST "$HAL_SERVER_URL/api/volume" \
  -H "Content-Type: application/json" \
  -d '{"direction": "down"}'

# Custom step (default 0.1)
curl -sS -X POST "$HAL_SERVER_URL/api/volume" \
  -H "Content-Type: application/json" \
  -d '{"direction": "up", "step": 0.25}'
```

## Mic mute

Toggle the microphone mute on the Raspberry Pi:

```sh
# Toggle mute (returns {"status":"ok"})
curl -sS -X POST "$HAL_SERVER_URL/api/mute"

# Read current mute state
curl -sS "$HAL_SERVER_URL/api/mute"
# → {"muted": true|false}
```

## Push-to-talk (PTT)

Open a listening session without saying the wake word. STT begins immediately and runs until you end (or cancel) the session. The hub shows the PTT chip + orb glow; an active photo frame dismisses automatically.

```sh
# Start listening (idempotent; second start while open is a no-op)
curl -sS -X POST "$HAL_SERVER_URL/api/ptt/start"
# → {"status":"ok","session":true}

# End and process the captured audio through the LLM
curl -sS -X POST "$HAL_SERVER_URL/api/ptt/end"
# → {"status":"ok","session":false}

# Cancel without running the LLM (drops the captured audio)
curl -sS -X POST "$HAL_SERVER_URL/api/ptt/cancel"
# → {"status":"cancelled","session":false}
```

`{"status":"rpi_disconnected"}` means the Raspberry Pi audio client is not currently connected — STT cannot run, the press is refused. If a session is already open, a second `/start` returns `{"status":"already_active"}`. Releases that come back faster than ~250 ms are debounced and end as cancellations.

## Display power (DPMS)

Turn the hub's physical display on or off (real DPMS — the panel
actually powers off, not just a black overlay). Auto-wakes on any
incoming hub activity (wake word, PTT, takeover, TTS reply).

```sh
# Read current state + idle-blank timeout
curl -sS "$HAL_SERVER_URL/api/display"
# → {"state":"on","auto_off_seconds":0,"available":true}

# Manual on / off / toggle
curl -sS -X POST "$HAL_SERVER_URL/api/display" \
  -H "Content-Type: application/json" \
  -d '{"state":"off"}'

# Or via voice through the LLM:
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "turn off the screen"}'
```

`available: false` in the GET response means the hub host has no
supported DPMS backend (wlr-randr / xset / vcgencmd). In that state
the POST returns `{"status":"unavailable"}` and the voice tool replies
with the same.

## Show an image on the orb

Display an arbitrary image on the hub orb for a configurable duration.
No REST endpoint — use MQTT or the LLM voice path.

```sh
# Via MQTT — plain URL (default 60 s)
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/image/set" \
  -m "https://example.com/photo.jpg"

# Via MQTT — JSON with duration
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/image/set" \
  -m '{"url":"https://example.com/photo.jpg","duration_s":120}'

# Via LLM voice path
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "show the picture at https://example.com/photo.jpg for 2 minutes"}'
```

The MQTT topic also accepts raw `image/jpeg` bytes as the payload (for
binary pushes from automations).

## Play a video on the orb

Play an HTTP video (MP4, WebM) or HLS playlist on the hub orb.
Auto-stops at end of file unless `loop` is set. Audio ducks
automatically when PAL speaks.

```sh
# Via MQTT — plain URL
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/video/set" \
  -m "https://example.com/clip.mp4"

# Via MQTT — JSON with options
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/video/set" \
  -m '{"url":"https://example.com/clip.mp4","loop":true,"muted":true,"duration_s":300}'

# Via LLM voice path
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "play https://example.com/clip.mp4 looping and muted"}'
```

## Show a HA camera on the orb

Paint a snapshot from a Home Assistant `camera.*` or `image.*` entity
inside the orb. For `camera.*` entities, set `live: true` to open a
low-latency WebRTC stream instead of a static snapshot.

```sh
# Via MQTT — entity_id only (snapshot, default ~2.5 min)
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/camera/set" \
  -m "camera.front_door"

# Via MQTT — JSON with live streaming + custom duration
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/camera/set" \
  -m '{"entity_id":"camera.front_door","live":true,"duration_s":300}'

# Via LLM voice path (snapshot)
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "show me the front door camera"}'

# Via LLM voice path (live WebRTC)
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "stream the front door camera live"}'
```

`live: true` is ignored for `image.*` entities (they have no video
feed). The orb shows one thing at a time — starting a new display
replaces whatever's there.

## Stream an RTSP source on the orb

Open a WebRTC stream from any RTSP URL (IP cam, NVR, Frigate, go2rtc)
via the bundled go2rtc sidecar. Default duration 5 minutes.

```sh
# Via MQTT — plain URL
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/rtsp/set" \
  -m "rtsp://admin:pass@10.0.0.20:554/stream1"

# Via MQTT — JSON with custom duration
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/rtsp/set" \
  -m '{"rtsp_url":"rtsp://admin:pass@10.0.0.20:554/stream1","duration_s":600}'

# Via LLM voice path
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "stream the rtsp at rtsp://admin:pass@10.0.0.20:554/stream1"}'
```

## Show the calendar overlay

Pop up a calendar overlay on the hub (month / week / day view).
Merges all HA calendars by default; pass `calendar_name` to filter.
Auto-dismisses after `calendar_dismiss_seconds` (default 30, configurable
via MQTT / runtime config).

```sh
# Via MQTT — bare view name
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/calendar/show/set" \
  -m "week"

# Via MQTT — JSON with options
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/calendar/show/set" \
  -m '{"view":"month","calendar_name":"Family","anchor_date":"2026-06-01","duration_s":60}'

# Dismiss
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/calendar/hide/set" \
  -m ""

# Via LLM voice path
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "show my calendar for this week"}'
```

Views: `month` (default), `week`, `day`. If the bare payload is a
string that isn't a view name, it's treated as `calendar_name`.

## Photo frame (open / dismiss on demand)

Open the photo frame on the hub — full-screen image (HA `image.*` or `camera.*` entity) with white drop-shadow clock and Ken-Burns zoom. If `photo_frame_entity` is configured in runtime config, no body is needed; otherwise pass `entity_id` explicitly. Auto-crossfades when HA rotates the underlying image.

```sh
# REST — open using the configured default entity
curl -sS -X POST "$HAL_SERVER_URL/api/photo_frame/start"
# → {"status":"ok","session":true}

# REST — open a specific entity
curl -sS -X POST "$HAL_SERVER_URL/api/photo_frame/start" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "image.living_room_slideshow"}'

# REST — dismiss
curl -sS -X POST "$HAL_SERVER_URL/api/photo_frame/end"
# → {"status":"ok","session":false}

# MQTT — open (bare entity_id or JSON)
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/photo_frame/show/set" \
  -m "image.google_photos_rotator_next_photo"

# MQTT — dismiss
mosquitto_pub -h "$MQTT_HOST" \
  -t "hal/$HAL_DEVICE_ID/photo_frame/hide/set" \
  -m ""

# Via LLM voice path
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "show the photo frame"}'
```

Statuses returned by `/start`: `ok` (opened), `already_active` (same entity already showing — pushes a refresh image), `not_configured` (no entity given AND `photo_frame_entity` is empty), `invalid_entity` (must start with `image.` or `camera.`), `fetch_failed` (HA unreachable or non-image MIME). The photo frame is automatically dismissed when a command is received, wake word fires, or PTT starts.

## Photo-frame idle auto-activation

After `N` minutes of no activity the hub auto-activates the photo
frame (uses the configured `photo_frame_entity`). `0` disables. Wake
word, PTT, video/image/calendar takeover, PAL TTS, and a hub-side
dismissal all reset the timer.

```sh
# Read current threshold + whether a session is open
curl -sS "$HAL_SERVER_URL/api/photo_frame/idle"
# → {"minutes": 30, "active": false}

# Set the threshold (clamped to 0..720)
curl -sS -X POST "$HAL_SERVER_URL/api/photo_frame/idle" \
  -H "Content-Type: application/json" \
  -d '{"minutes": 30}'

# Disable
curl -sS -X POST "$HAL_SERVER_URL/api/photo_frame/idle" \
  -H "Content-Type: application/json" \
  -d '{"minutes": 0}'

# Or via voice through the LLM:
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "auto-show the photo frame after 30 minutes"}'
```

## Health check

Verify PAL is alive and which subsystems are connected:

```sh
curl -sS "$HAL_SERVER_URL/health"
# → {"status":"ok","pipeline_ready":true,"mcp_connected":true,"tts_available":true,"memory_available":true}
```

## Read what PAL last said

Useful when you want to confirm or react to PAL's most recent reply:

```sh
# Published on MQTT as a Home Assistant sensor entity:
#   sensor.<HAL_DEVICE_ID>_last_response
# State = truncated text (≤250 chars), attribute "full_text" = full reply.
# Read via HA's REST API or the homeassistant CLI if you have access.
```

## Grab a screenshot of the hub

For visual confirmation of what PAL is currently showing (orb state, active photo frame, calendar overlay, camera view, etc.), fetch the latest hub snapshot. The RPi audio_streamer posts a fresh JPEG to the server every `SNAPSHOT_INTERVAL_S` seconds (default 60).

```sh
curl -sS -o /tmp/hal.jpg "$HAL_SERVER_URL/api/snapshot.jpg"
```

## Theme control

PAL's web UI theme is a plug-in registry; the available set changes as themes are added/removed under `server/themes/`. List the current set with:

```sh
curl -sS "$HAL_SERVER_URL/api/themes"
# → {"themes":[{"name":"birch", ...},{"name":"dark", ...}, ...]}
```

The PAL server picks the theme automatically at dusk/dawn when auto-theme is enabled, but you can also drive it through the LLM:

```sh
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "switch the UI theme to japandi"}'
```

## Calling pattern

Use the `exec` tool to run the curl commands above. Always:

1. Read `$HAL_SERVER_URL` from configuration
2. Run the appropriate `curl` invocation
3. Confirm the response shows `{"status":"ok"}` (or the expected payload for GETs)
4. Reply to the user with what you did, e.g. "Sent to PAL: 'turn on the kitchen lights'"

If the response is `{"status":"error","message":"RPi not connected"}` it means the Raspberry Pi audio client is not currently connected to the AI server — tell the user.
