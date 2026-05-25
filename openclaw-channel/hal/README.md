# HAL Kiosk — OpenClaw Channel Plugin

Connects a [HAL voice assistant](../README.md) kiosk to OpenClaw as an
alternative conversation engine. Voice commands captured by HAL are routed
through an OpenClaw agent with full MCP tool access, and the agent's
responses are delivered back for TTS playback and orb display.

## How it works

1. HAL sends transcribed voice text via HTTP POST to the plugin's
   webhook endpoint on the OpenClaw gateway.
2. The plugin dispatches the message through the standard agent turn
   pipeline (session recording, routing, full tool access including MCP).
3. The agent processes the message — it can call HAL's REST/MQTT
   endpoints directly (documented in `SKILL.md`) for things like
   showing cameras, adjusting volume, controlling smart home devices.
4. The agent's text response (and any media URLs) are delivered back
   to HAL via an HTTP callback to `/api/openclaw/response`.
5. HAL renders the text through TTS and displays any media on the orb.

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

Add to `~/.openclaw/openclaw.json`:

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

Make sure the HAL SKILL.md is installed in the workspace:

```bash
cp openclaw-skill/hal/SKILL.md ~/.openclaw/<workspace>/skills/hal/SKILL.md
```

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
