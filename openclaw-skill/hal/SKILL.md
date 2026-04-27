---
name: hal
description: Control HAL — local voice assistant for Home Assistant. Send spoken commands, adjust speaker volume, toggle mic mute, and switch the web UI theme.
metadata.openclaw.requires.bins: ["curl"]
metadata.openclaw.requires.config: ["HAL_SERVER_URL"]
---

# HAL Voice Assistant Control

You can control a self-hosted HAL voice assistant via its REST API. HAL is a 2001-style voice assistant that listens through a Raspberry Pi, runs Whisper STT + a local LLM (Ollama) with Home Assistant MCP tools, and replies through a Wyoming-protocol TTS service. The Raspberry Pi also serves a HAL 9000-style web UI showing live transcription and the assistant's state.

The base URL of the AI server is exposed as `$HAL_SERVER_URL` (set in OpenClaw config). All requests use JSON.

## When to use this skill

- The user asks to "tell HAL", "ask HAL", "command HAL", or anything addressed to HAL
- The user asks to control the HAL UI (theme, volume, mute) without speaking
- The user wants HAL to do something on their smart home (lights, climate, scenes) — HAL has the Home Assistant MCP tools wired in, so just send the natural-language command via `/api/command`

## Send a spoken command to the LLM

Use this for ANY request that should run through HAL's LLM, including HA control commands like "turn on the kitchen lights". The text appears on HAL's web UI as a transcription, the LLM processes it (with all its MCP tools available), and HAL speaks the response through the Raspberry Pi.

```sh
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "<command>"}'
```

Examples:
- `{"text": "turn on the table lamp"}`
- `{"text": "what's the temperature in the bedroom?"}`
- `{"text": "play some jazz on the living room speaker"}`

## Speak text out loud verbatim (bypass the LLM)

When you (the agent) want HAL to say a specific message exactly as written —
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

Use this when the wording matters and you don't want HAL's witty butler
persona to rewrite it. Use `/api/command` instead when you want HAL to
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

## Health check

Verify HAL is alive and which subsystems are connected:

```sh
curl -sS "$HAL_SERVER_URL/health"
# → {"status":"ok","pipeline_ready":true,"mcp_connected":true,"tts_available":true,"memory_available":true}
```

## Theme control

HAL's web UI has four themes (`dark`, `birch`, `odyssey`, `japandi`). The HAL server picks the theme automatically at dusk/dawn, but you can also drive it through the LLM by issuing a natural-language command:

```sh
curl -sS -X POST "$HAL_SERVER_URL/api/command" \
  -H "Content-Type: application/json" \
  -d '{"text": "switch the UI theme to japandi"}'
```

## Calling pattern

Use the `exec` tool to run the curl commands above. Always:

1. Read `$HAL_SERVER_URL` from configuration (e.g. `http://10.20.30.185:8765`)
2. Run the appropriate `curl` invocation
3. Confirm the response shows `{"status":"ok"}` (or the expected payload for GETs)
4. Reply to the user with what you did, e.g. "Sent to HAL: 'turn on the kitchen lights'"

If the response is `{"status":"error","message":"RPi not connected"}` it means the Raspberry Pi audio client is not currently connected to the AI server — tell the user.
