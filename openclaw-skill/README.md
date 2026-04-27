# OpenClaw skill — HAL

A skill that lets an OpenClaw agent control your HAL voice assistant via its
REST API. It teaches the agent how to send commands to HAL, adjust the
speaker volume, toggle the mic mute, switch the UI theme, and check health.

## Install

Copy the `hal/` directory into your OpenClaw workspace skills folder:

```sh
mkdir -p ~/.openclaw/workspace/skills
cp -r hal ~/.openclaw/workspace/skills/
```

## Configure

Set the HAL server URL in your OpenClaw config so the skill can read
`$HAL_SERVER_URL`:

```yaml
# Example: ~/.openclaw/config.yaml (path may differ — check your OpenClaw setup)
config:
  HAL_SERVER_URL: "http://10.20.30.185:8765"
```

The skill also requires `curl` on PATH (it uses the `exec` tool).

## What the skill exposes

Once installed, the agent can:

- Send any natural-language command to HAL's LLM via `/api/command`
  (covers Home Assistant control, conversational replies, etc.)
- Adjust the Raspberry Pi speaker volume up or down (`/api/volume`)
- Toggle the microphone mute (`/api/mute`)
- Query mute state (`GET /api/mute`)
- Check HAL health (`/health`)
- Change the web UI theme by phrasing it as a command to HAL

## Endpoints reference

| Endpoint | Method | Body | Purpose |
|---|---|---|---|
| `/api/command` | POST | `{"text": "..."}` | Send to LLM — drives all HA control + conversational replies |
| `/api/volume` | POST | `{"direction": "up\|down", "step": 0.1}` | Adjust speaker volume |
| `/api/mute` | POST | — | Toggle mic mute |
| `/api/mute` | GET | — | Read mute state |
| `/health` | GET | — | Service health |

See the [HAL repository](https://github.com/moimart/conversation-hass) for
more detail on the server.
