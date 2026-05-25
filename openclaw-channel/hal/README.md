# HAL Kiosk — OpenClaw Channel Plugin

Connects a [HAL voice assistant](../README.md) kiosk to OpenClaw as an
alternative conversation engine. Voice commands captured by HAL are routed
through an OpenClaw agent with full MCP tool access, and the agent's
responses are delivered back for TTS playback and orb display.

## How it works

```
Voice → HAL STT → text
          ↓ (if openclaw_enabled)
  POST /channels/hal/webhook on gateway
          ↓
  runtime.channel.turn.runAssembled()  ← full agent pipeline w/ mcporter
          ↓
  POST /api/openclaw/response on HAL   ← delivery callback
          ↓
  text → TTS → speaker
  media_urls → orb display / QR codes
```

1. HAL sends transcribed voice text via HTTP POST to the plugin's
   webhook endpoint on the OpenClaw gateway.
2. The plugin dispatches the message through the standard agent turn
   pipeline (`runAssembled()`) — same as Telegram/WhatsApp channels,
   with full tool access including mcporter.
3. The agent uses `mcporter` with two MCP servers:
   - **`hal`** — HAL's own MCP server at `/mcp` (SSE transport) for
     kiosk controls: volume, mute, display, photo frame, camera, etc.
   - **`berlinmcp`** (or your HA MCP) — for Home Assistant tasks:
     lights, climate, sensors, automations.
4. The agent's text response (and any media URLs) are delivered back
   to HAL via an HTTP callback to `/api/openclaw/response`.
5. HAL renders the text through TTS and displays any media on the orb.
   URLs without media are shown as QR codes.

Falls back to Ollama automatically on timeout or error.

## Installation

```bash
# On the OpenClaw gateway machine:
cd openclaw-channel/hal
npm install
npm run build
openclaw plugins install --link .

# Or from elsewhere:
openclaw plugins install /path/to/openclaw-channel/hal
```

## Configuration

### 1. Gateway — `~/.openclaw/openclaw.json`

```json5
{
  channels: {
    hal: {
      enabled: true,
      halBaseUrl: "http://<hal-server>:8765",
      dmPolicy: "open"          // LAN trust; use "allowlist" + allowFrom for stricter control
    }
  }
}
```

### 2. Workspace mcporter — `~/.openclaw/<workspace>/config/mcporter.json`

Register HAL's MCP server so the agent can control the kiosk via mcporter:

```json
{
  "mcpServers": {
    "hal": {
      "baseUrl": "http://<hal-server>:8765/mcp/sse"
    },
    "berlinmcp": {
      "baseUrl": "https://your-ha-mcp-server/endpoint"
    }
  }
}
```

### 3. SKILL.md

Install the skill in the workspace so the agent knows how to use HAL:

```bash
cp openclaw-skill/hal/SKILL.md ~/.openclaw/<workspace>/skills/hal/SKILL.md
```

### 4. HAL server — Docker environment

```bash
OPENCLAW_ENABLED=true
OPENCLAW_GATEWAY_URL=http://<gateway-host>:18789
OPENCLAW_WORKSPACE=<workspace-name>
```

These can also be toggled at runtime via Home Assistant entities
(switch + text inputs) exposed through the MQTT bridge.

## Home Assistant entities

The MQTT bridge auto-discovers these entities in HA:

| Entity | Type | Description |
|--------|------|-------------|
| `switch.openclaw` | Switch | Enable/disable OpenClaw engine |
| `text.openclaw_gateway_url` | Text | Gateway URL |
| `text.openclaw_workspace` | Text | Workspace name |
| `sensor.conversation_engine` | Sensor (diagnostic) | Shows "openclaw" or "ollama" |

## Webhook Endpoint

`POST /channels/hal/webhook`

```json
{
  "text": "turn on the kitchen lights",
  "request_id": "uuid-v4",
  "sender_id": "kiosk-user"
}
```

Response: `200 {"status": "accepted", "request_id": "..."}` (immediate).

The agent's response is delivered asynchronously to `{halBaseUrl}/api/openclaw/response`.

## HAL MCP Server

HAL exposes its local tools as an MCP server at `/mcp` (SSE transport).
Tools include: `set_volume`, `toggle_mute`, `set_theme`, `speak_verbatim`,
`show_camera`, `stream_camera`, `show_photo_frame`, `show_calendar`,
`set_display_power`, `show_qr_code`, and more.

The MCP server has DNS rebinding protection disabled to allow LAN access
from the OpenClaw gateway.
