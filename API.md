# PAL — REST + WebSocket API Reference

The AI server (default port **`8765`**) exposes a small REST surface and
three WebSocket endpoints. The Raspberry Pi audio_streamer (port
**`8080`** on the kiosk host) re-publishes a couple of these and serves
the kiosk UI.

**Two reachability tiers** (see [Reachability](#reachability-lan-vs-gateway)):

- **LAN (`:8765`)** — the full surface, **no authentication** (LAN trust).
  Keep it behind your firewall / VPN; never port-forward `:8765`.
- **Gateway (`:8766`)** — the optional internet-facing `hal-gateway` proxy
  exposes only a **token-gated allowlist** (the satellite subset). The
  home-control surface (speak, display, volume, PTT, pairing mint, MCP,
  MQTT) is **never** reachable through it.

* **Default base URL**: `http://<ai-server-host>:8765`
* **Content type for JSON bodies**: `application/json`
* **All responses**: `application/json` unless noted (binary endpoints
  return `image/jpeg`, etc.)

---

## Table of contents

- [Conventions](#conventions)
- [Reachability (LAN vs gateway)](#reachability-lan-vs-gateway)
  - [Token scopes](#token-scopes)
- [Health](#health)
- [Push-to-Talk](#push-to-talk)
  - [`POST /api/ptt/start`](#post-apipttstart)
  - [`POST /api/ptt/end`](#post-apipttend)
  - [`POST /api/ptt/cancel`](#post-apipttcancel)
- [Conversation](#conversation)
  - [`POST /api/command`](#post-apicommand)
  - [`POST /api/speak`](#post-apispeak)
  - [`GET /api/conversation/log`](#get-apiconversationlog)
- [Cloud LLM override](#cloud-llm-override)
  - [`GET /api/cloud_llm`](#get-apicloud_llm)
  - [`POST /api/cloud_llm`](#post-apicloud_llm)
- [Audio control](#audio-control)
  - [`POST /api/mute`](#post-apimute)
  - [`GET /api/mute`](#get-apimute)
  - [`POST /api/volume`](#post-apivolume)
- [Snapshots (kiosk → server)](#snapshots)
  - [`POST /api/snapshot`](#post-apisnapshot)
  - [`GET /api/snapshot.jpg`](#get-apisnapshotjpg)
- [Photo frame](#photo-frame)
  - [`POST /api/photo_frame/start`](#post-apiphoto_framestart)
  - [`POST /api/photo_frame/end`](#post-apiphoto_frameend)
- [Themes](#themes)
  - [`GET /api/themes`](#get-apithemes)
  - [`GET /themes/{name}/{filename}`](#get-themesnamefilename)
- [WebSocket endpoints](#websocket-endpoints)
  - [`/ws/ptt`](#wsptt)
  - [`/ws/ui`](#wsui)
  - [`/ws/audio`](#wsaudio)
- [RPi audio_streamer (port 8080)](#rpi-audio_streamer-port-8080)
- [Errors and status codes](#errors-and-status-codes)

---

## Conventions

All JSON-returning endpoints follow this shape on **success**:

```json
{ "status": "ok", ... }
```

…and this shape on **failure** (HTTP still 200 — see [Errors](#errors-and-status-codes)):

```json
{ "status": "error", "message": "<human-readable reason>" }
```

Push-to-Talk and a few others return richer status strings — see each
endpoint.

---

## Reachability (LAN vs gateway)

The **LAN** surface (`:8765`) has **no auth** — the whole API below is
reachable on the local network. The optional **gateway** (`hal-gateway`,
`:8766`) is the only thing you expose to the internet, and it serves a
**default-deny, token-gated allowlist**: only the rows marked ✓ below reach
the server through it; everything else is `404` at the edge. So a route is
internet-reachable **only** if its "Gateway" cell is ✓.

| Endpoint | LAN `:8765` | Gateway `:8766` | Auth |
|---|:---:|:---:|---|
| `GET /health` | ✓ | ✓ | none (public) |
| `GET /api/themes`, `GET /themes/{name}/{file}` | ✓ | ✓ | none (public) |
| `POST /api/command` | ✓ | ✓ | token at gateway edge |
| `GET /api/conversation/log` (+ `/image`) | ✓ | ✓ | token |
| `GET`/`POST /api/cloud_llm` | ✓ | ✓ | token |
| `GET /api/pair/status` | ✓ | ✓ | token (also the edge validator) |
| `POST /api/pair/push-register` | ✓ | ✓ | token |
| `GET /api/satellite/tts` | ✓ | ✓ | token |
| `GET /api/satellite/stream.mjpeg` | ✓ | ✓ | token (`?token=`) |
| `POST /api/satellite/photo_frame/{start,stop}` | ✓ | ✓ | token |
| `GET /api/push/image/{id}.jpg` | ✓ | ✓ | **signed URL** (HMAC, no token) |
| `WS /ws/ui` | ✓ (mirror, tokenless) | ✓ | **token REQUIRED** on the gateway |
| `POST /api/speak` | ✓ | — | LAN-only |
| `POST /api/mute`, `GET /api/mute`, `POST /api/volume` | ✓ | — | LAN-only |
| `GET`/`POST /api/display`, `…/photo_frame/idle` | ✓ | — | LAN-only |
| `POST /api/snapshot`, `GET /api/snapshot.jpg` | ✓ | — | LAN-only |
| `POST /api/photo_frame/{start,end}` (kiosk) | ✓ | — | LAN-only |
| `POST /api/ptt/{start,end,cancel}`, `WS /ws/ptt` | ✓ | — | LAN-only |
| `POST /api/pair/request`, `/redeem` | ✓ | — | LAN-only — **pairing only happens at home** |
| `POST /api/pair/derive` | ✓ | — | LAN-only — scoped-token mint (full-token Bearer) |
| `GET /api/pair/devices`, `POST /api/pair/revoke` | ✓ | — | LAN-only — device admin stays home-side |
| `/mcp`, `WS /ws/audio`, all MQTT | ✓ | — | LAN-only |

Full gateway config + rationale: [Satellite gateway](#satellite-gateway-port-8766).

### Token scopes

Pairing tokens carry a **scope** that bounds what they may do *server-side*
(the gateway edge only checks validity, so scope is enforced per-route by the
server). `full` — phones, and every token issued before scopes existed —
is unrestricted. `watch` — the Apple Watch companion — may use **only**
`POST /api/command`, `GET /api/pair/status`, and `POST /api/pair/push-register`;
anything else returns `403` (or close code `4403` on `/ws/ui` — a scoped token
is never downgraded to a tokenless mirror). Unknown scopes deny everything.

A scoped token is minted with **`POST /api/pair/derive`** (LAN-only),
authorized by an existing **full** token in the `Authorization: Bearer`
header — the phone vouches for the watch, no code typing on the new device:

```json
{ "scope": "watch", "device_name": "Apple Watch" }
```

→ `{ "token": "...", "scope": "watch", "server_name": "...", "gateway_url": "..." }`
(mirrors `/api/pair/redeem`, so the enrollee learns the gateway base for
away-from-home use). Scoped tokens can't derive further tokens, `full` is not
derivable, and each derived token revokes independently via
`POST /api/pair/revoke`. `GET /api/pair/devices` shows each device's scope.

---

## Health

### `GET /health`

Cheap readiness probe. Returns the loaded-status of every major
subsystem.

**Response** `200`

```json
{
  "status": "ok",
  "pipeline_ready": true,
  "mcp_connected": true,
  "tts_available": true,
  "memory_available": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `pipeline_ready` | bool | Audio pipeline (VAD + STT + speaker filter) loaded |
| `mcp_connected` | bool | At least one MCP tool registered |
| `tts_available` | bool | Wyoming TTS endpoint configured |
| `memory_available` | bool | Shodh long-term-memory backend reachable |

```bash
curl http://hal:8765/health
```

---

## Push-to-Talk

Three bare-trigger endpoints. All three are idempotent — POSTing twice
is safe. See [`PTT_INTERNALS`](#wsptt) below for the WebSocket variant.

### `POST /api/ptt/start`

Open a Push-to-Talk session: bypass the wake word, capture audio
through STT until [`/api/ptt/end`](#post-apipttend) (or
[`/api/ptt/cancel`](#post-apipttcancel), or the 20 s safety timeout).

**Side effects on the server**:
- If TTS is playing, cancel it immediately on the RPi.
- If the mic is muted, auto-unmute for the duration (snapshot the prior
  mute state, restore on end).
- Set `conversation._wake_detected = True` so STT output flows into the
  command buffer.
- Push `state=listening` and `ptt_active=true` to the kiosk.
- Schedule a 20 s safety timeout — if `end` never arrives, finalise
  anyway.

**Request body**: empty

**Response** `200`

| Status string | When | Session opened? |
|---|---|---|
| `"ok"` | New session opened | yes |
| `"already_active"` | A PTT session is already open (idempotent) | yes |
| `"rpi_disconnected"` | The RPi audio_streamer isn't connected — no audio path | **no** |
| `"not_ready"` | Pipeline or conversation manager not initialised yet | no |

```json
{ "status": "ok", "session": true }
```

```bash
curl -XPOST http://hal:8765/api/ptt/start
```

### `POST /api/ptt/end`

Close the active PTT session, force-finalise whatever audio the VAD has
been buffering, and run the LLM on the transcript.

**Behaviour**:
- If no session is open: no-op, returns `"not_active"`.
- If the session has been open for **< 100 ms** (likely button bounce):
  treated as cancel — buffer is dropped, no LLM call.
- Otherwise: transcribes the captured audio with a 15 s STT timeout,
  feeds the transcript into the conversation manager exactly like a
  wake-word turn, restores the prior mute state.

**Request body**: empty

**Response** `200`

| Status string | Meaning |
|---|---|
| `"ok"` | Session closed normally, LLM running (or queued) |
| `"cancelled"` | Closed but no LLM run (debounce, no audio captured, or explicit cancel) |
| `"not_active"` | No session was open |

```json
{ "status": "ok", "session": false }
```

```bash
curl -XPOST http://hal:8765/api/ptt/end
```

### `POST /api/ptt/cancel`

Close the active session and **discard** the captured audio — no
transcript, no LLM call. Use when the trigger party knows the press was
an accident.

**Request body**: empty

**Response** `200`

```json
{ "status": "cancelled", "session": false }
```

```bash
curl -XPOST http://hal:8765/api/ptt/cancel
```

---

## Conversation

### `POST /api/command`

Inject a text command as if the user had spoken it. Bypasses STT and
the wake word; runs the full LLM round (with tool calling), produces a
TTS response that plays on the RPi.

**Request body**

```json
{ "text": "turn off the kitchen lights" }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | ✓ | The user's utterance. Empty/whitespace returns `"error"`. |
| `wait_reply` | bool | — | Default `false`. `true` = run the turn **synchronously** and return PAL's reply in this response. |

**Response** `200` (default, fire-and-forget)

```json
{ "status": "ok", "message": "Command received" }
```

The command runs asynchronously — the response confirms receipt, not
completion. Watch `/ws/ui` for state transitions or `state.last_response`
on MQTT for the eventual reply.

**Response** `200` (`wait_reply: true`)

```json
{ "status": "ok", "reply": "The kitchen lights are off, Master." }
```

The request blocks until the turn completes (capped at 90 s; on timeout
`reply` is `""` with a `message`). This is the reply channel for clients
without a `/ws/ui` connection — the Apple Watch app (whose token scope
excludes the WebSocket) uses it for every command. The gateway's proxy
timeout (100 s) accommodates it.

```bash
curl -XPOST http://hal:8765/api/command \
     -H 'Content-Type: application/json' \
     -d '{"text":"what time is it"}'
```

### `POST /api/speak`

Speak text **verbatim** through the RPi speaker. **Does not** run the
LLM — this is for announcements ("the package arrived"), notifications,
or anything where you want the exact wording vocalised in PAL's voice.

**Request body**

```json
{ "text": "Dinner is ready." }
```

**Response** `200`

```json
{ "status": "ok", "spoke": "Dinner is ready." }
```

Possible error statuses:
- `"Empty text"` — body is missing or whitespace
- `"RPi not connected"` — no audio_websocket
- `"TTS engine not available"` — Wyoming TTS unreachable
- `"TTS synthesis failed: <reason>"` — TTS server returned an error
- `"TTS produced no audio"` — TTS returned zero bytes

```bash
curl -XPOST http://hal:8765/api/speak \
     -H 'Content-Type: application/json' \
     -d '{"text":"Front door is open."}'
```

### `GET /api/conversation/log`

One page of the **persistent conversation log** (PostgreSQL-backed; see the
README's *Conversation log* section). Every user request, assistant answer,
and announcement is stored with a timestamp and an origin label. Rows come
back **oldest → newest** within the page.

**Query parameters**

| Param | Default | Notes |
|---|---|---|
| `limit` | `100` | Rows per page, clamped to 1–500 |
| `before_id` | (none) | Keyset pagination: return rows with `id` **below** this — pass the oldest `id` you have to page back through history |

**Response** `200`

```json
{
  "rows": [
    {"id": 41, "ts": "2026-06-05T07:50:32.072125+00:00", "kind": "user",
     "text": "what time is it", "origin": "Moi's Pixel", "meta": null},
    {"id": 42, "ts": "2026-06-05T07:50:35.901482+00:00", "kind": "assistant",
     "text": "It is ten to eight, Master.", "origin": null, "meta": null}
  ],
  "has_more": true
}
```

| Field | Notes |
|---|---|
| `kind` | `user` \| `assistant` \| `announcement` \| `image` (a static image shown on the orb; `origin` = its entity/source label) |
| `origin` | `null` for kiosk voice turns; the paired device's name for satellite turns; the source channel (`api`, `mqtt`, `openclaw`, `voice-tool`) for announcements. Never the pairing token. |
| `has_more` | `true` while older rows exist (keep paging with `before_id`) |
| `has_image` | `true` on `kind: "image"` rows (orb images) — fetch the thumbnail lazily via `GET /api/conversation/log/image?id=<row id>` (returns the stored JPEG; page payloads never carry bytes) |

Degraded shapes: `{"rows": [], "has_more": false, "disabled": true}` when no
DSN is configured, and `503` `{"error": "log_unavailable", "rows": []}` while
postgres is unreachable (the server reconnects lazily).

```bash
curl 'http://hal:8765/api/conversation/log?limit=50&before_id=1200'
```

---

## Cloud LLM override

Routes every turn (tool calls included) to a cloud OpenAI-compatible provider,
skipping the router model and OpenClaw. Providers + API keys are configured
server-side only (`server/runtime/cloud_providers.json`, hot-reloaded) — keys
are **never accepted or returned** by these endpoints. The enabled switch
always boots OFF after a server restart; the model choice persists.

### `GET /api/cloud_llm`

**Response** `200`

```json
{ "enabled": false, "model": "openai/gpt-5.5", "options": ["openai/gpt-4o", "openai/gpt-5.5", "..."], "available": true }
```

`options` is the merged `provider/model-id` list fetched live from each
configured provider's `/models` API, filtered to chat-completions-capable
models. `available` is `true` when at least one cloud provider is configured
(file or env) — the companion app's settings sheet shows its **Cloud LLM**
toggle only then. The response never includes keys or endpoints.

### `POST /api/cloud_llm`

All fields optional. Dispatches through the same callbacks as the HA MQTT
config entities, so HA stays in sync.

**Request body**

```json
{ "enabled": true, "model": "openai/gpt-5.5", "refresh": true }
```

- `enabled` — flip the override on/off
- `model` — pick a `provider/model-id` (must be in `options`)
- `refresh` — re-fetch the model list from all providers

**Response** `200` — same shape as `GET`.

```bash
curl -XPOST http://hal:8765/api/cloud_llm \
     -H 'Content-Type: application/json' \
     -d '{"enabled": true, "model": "openai/gpt-5.5"}'
```

---

## Audio control

### `POST /api/mute`

Toggle the RPi mic mute. Sends a `mute_toggle` message over `/ws/audio`;
the RPi flips its `mic_muted` state and echoes a `mute_sync` back.

**Request body**: empty

**Response** `200`

```json
{ "status": "ok" }
```

`{"status": "error", "message": "RPi not connected"}` if the
audio_websocket is down.

```bash
curl -XPOST http://hal:8765/api/mute
```

### `GET /api/mute`

Return the cached mute state (the server mirrors the RPi's
`mute_sync` echoes).

**Response** `200`

```json
{ "muted": false }
```

```bash
curl http://hal:8765/api/mute
```

### `POST /api/volume`

Bump the RPi TTS volume up or down by a relative step.

**Request body**

```json
{ "direction": "up", "step": 0.1 }
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `direction` | `"up"` \| `"down"` | required | Sign of the delta |
| `step` | float | `0.1` | Absolute size; `0.1` = 10% |

**Response** `200`

```json
{ "status": "ok" }
```

```bash
curl -XPOST http://hal:8765/api/volume \
     -H 'Content-Type: application/json' \
     -d '{"direction":"up","step":0.05}'
```

---

## Display power (DPMS)

Real hardware DPMS — the panel actually powers off, not just a black
overlay. The RPi-side container picks the first available backend at
startup: `wlr-randr` (Wayland kiosks, including labwc on the RPi),
`xset` (X11 kiosks), or `vcgencmd` (Pi-firmware fallback). If none of
the three is available, all control routes return
`{"status":"unavailable"}` and the HA switch is greyed out.

### `GET /api/display`

Return the current display power state and the idle-blank timeout.

**Response** `200`

```json
{ "state": "on", "auto_off_seconds": 300, "available": true }
```

| Field | Type | Notes |
|---|---|---|
| `state` | `"on"` \| `"off"` | The server's view of the panel state. |
| `auto_off_seconds` | int | How long with no kiosk activity before auto-blank. `0` = disabled. |
| `available` | bool | False = no DPMS backend found on the kiosk host. |

```bash
curl http://hal:8765/api/display
```

### `POST /api/display`

Turn the kiosk display on or off.

**Request body**

```json
{ "state": "off" }
```

| Field | Type | Notes |
|---|---|---|
| `state` | `"on"` \| `"off"` \| `"toggle"` | required |

**Response** `200`

```json
{ "status": "ok", "state": "off" }
```

`{"status":"rpi_disconnected","state":"off"}` if the audio_websocket is
down (the change is still stored server-side and will apply on
reconnect). `{"status":"unavailable",...}` if the kiosk container has
no working DPMS backend.

Any incoming kiosk activity — wake-word fire, PTT, calendar / photo
frame / camera / image / video takeover, PAL TTS playback — auto-wakes
the display before the activity proceeds.

```bash
curl -XPOST http://hal:8765/api/display \
     -H 'Content-Type: application/json' \
     -d '{"state":"off"}'
```

---

## Photo-frame idle auto-activation

The kiosk can auto-fall-back to the photo frame after a configurable
idle period. `0` minutes disables the feature. Range 0–720 (12 h).

Activity that resets the timer: wake word, PTT, video / image /
calendar / camera takeover, PAL TTS playback, and a
`photo_frame_dismissed` event from the kiosk. The photo frame itself
opening does **not** reset the timer — that would re-arm it forever and
prevent re-trigger after a manual dismiss.

### `GET /api/photo_frame/idle`

**Response** `200`

```json
{ "minutes": 30, "active": false }
```

| Field | Type | Notes |
|---|---|---|
| `minutes` | int | Idle threshold in minutes; `0` = disabled. |
| `active` | bool | Whether a photo-frame session is currently open. |

### `POST /api/photo_frame/idle`

**Request body**

```json
{ "minutes": 30 }
```

| Field | Type | Notes |
|---|---|---|
| `minutes` | int | required; clamped to `0..720`. `0` disables. |

```bash
curl -XPOST http://hal:8765/api/photo_frame/idle \
     -H 'Content-Type: application/json' \
     -d '{"minutes": 30}'
```

Also exposed via MQTT (`hal/<id>/config/photo_frame_idle_minutes/{state,set}`)
and the HA Number entity `Photo Frame Idle Minutes` (Configuration
category), both auto-discovered.

---

## Snapshots

### `POST /api/snapshot`

The RPi audio_streamer posts a JPEG of the kiosk view here every
`SNAPSHOT_INTERVAL_S` seconds. The server caches it for
[`GET /api/snapshot.jpg`](#get-apisnapshotjpg) and forwards it to MQTT
(`<base>/snapshot`) so HA sees it as a `camera` entity.

You don't normally call this yourself — but you can post any JPEG to
have it appear on the HA camera entity.

**Request body**: raw `image/jpeg` bytes (max 8 MB)

**Response** `200`

```json
{ "status": "ok", "size": 184320 }
```

### `GET /api/snapshot.jpg`

Return the most recent JPEG. `404` if no snapshot has been posted yet.

```bash
curl -o latest.jpg http://hal:8765/api/snapshot.jpg
```

---

## Photo frame

Ambient full-screen image from a configurable HA `image.*` entity,
with the kiosk clock overlaid in white and a slow Ken-Burns zoom. The
photo frame auto-dismisses on **any** kiosk activity (state change,
volume/mute interaction, PTT trigger, pointer tap, another overlay).

The feature is gated by `runtime_config["photo_frame_entity"]` (see
[`MQTT.md`](./MQTT.md#live-runtime-config)). If neither the config nor
the request body provides an entity, `start` is a silent no-op
(`status: "not_configured"`).

### `POST /api/photo_frame/start`

Open a photo frame session. Optional body overrides the configured
default entity.

**Request body** (optional)

```json
{ "entity_id": "image.weather_radar" }
```

**Response** `200`

| Status string       | Meaning                                                       |
|---------------------|---------------------------------------------------------------|
| `"ok"`              | New session opened; image is being shown on the kiosk.        |
| `"already_active"`  | A session was already open for the same entity; the kiosk got a fresh `photo_frame_update` (covers re-fetch when the entity rotated). |
| `"not_configured"`  | No entity given and `photo_frame_entity` runtime config is empty. Silent no-op — surface this in the UI text rather than as an error. |
| `"invalid_entity"`  | The supplied `entity_id` isn't an `image.*` or `camera.*`.    |
| `"fetch_failed"`    | HA returned a non-image content type, 404, or the request capped out. |

```json
{ "status": "ok", "session": true }
```

```bash
curl -XPOST http://hal:8765/api/photo_frame/start \
     -H 'Content-Type: application/json' \
     -d '{"entity_id":"image.weather_radar"}'

# Or with the configured default:
curl -XPOST http://hal:8765/api/photo_frame/start
```

### `POST /api/photo_frame/end`

Dismiss the active photo frame. No-op when nothing is open.

**Request body**: empty

**Response** `200`

| Status string  | Meaning                                                    |
|----------------|------------------------------------------------------------|
| `"ok"`         | Session closed; kiosk is fading out; HA subscription torn down. |
| `"not_active"` | No session was open.                                       |

```bash
curl -XPOST http://hal:8765/api/photo_frame/end
```

---

## Themes

### `GET /api/themes`

List the installed plug-in themes. The kiosk uses this on first load
and on every `themes_changed` WebSocket event.

**Response** `200`

```json
{
  "themes": [
    {
      "name": "birch",
      "display_name": "Birch — Light",
      "description": "Warm beige Scandinavian wood tones — light-room friendly.",
      "kind": "light",
      "version": "1.0.0",
      "has_effect": false
    },
    {
      "name": "material_you",
      "display_name": "Material You — Sunlit Birch",
      "description": "Material You light theme tuned for birch wood…",
      "kind": "light",
      "version": "1.0.0",
      "has_effect": true
    }
  ]
}
```

`has_effect` indicates the theme ships an `effect.js` (animated
background) at `/themes/<name>/effect.js`.

### `GET /themes/{name}/{filename}`

Serve a theme's static asset (`theme.css`, `effect.js`, fonts, etc.).
Responses are sent with `Cache-Control: no-cache, no-store,
must-revalidate` so the kiosk always picks up the latest after a
hot-reload.

`404` if the theme or file doesn't exist. Path traversal is rejected.

```bash
curl http://hal:8765/themes/material_you/theme.css
```

---

## WebSocket endpoints

All three live on the AI server.

### `/ws/ptt`

Persistent low-latency Push-to-Talk channel. Apps that press the button
repeatedly should hold one of these open instead of making fresh HTTP
requests per press.

**Connect** to `ws://<ai-server>:8765/ws/ptt`. Send JSON text frames:

| Send                          | Server action                                 |
|-------------------------------|-----------------------------------------------|
| `{"type": "start"}`           | Calls `start_ptt(state)` — same as `POST /api/ptt/start` |
| `{"type": "end"}`             | Calls `end_ptt(state)` — same as `POST /api/ptt/end` |
| `{"type": "cancel"}`          | Calls `end_ptt(state, cancel=True)` |
| Anything else                 | Returns `{"status": "unknown_type", "type": "..."}` |

**Receive**: every command echoes the same status dict that the
equivalent HTTP route would return (see [PTT](#push-to-talk)).

```python
# Python example with `websockets`
import asyncio, json, websockets

async def hold(duration: float = 2.0):
    async with websockets.connect("ws://hal:8765/ws/ptt") as ws:
        await ws.send(json.dumps({"type": "start"}))
        print(await ws.recv())                      # {"status": "ok", "session": true}
        await asyncio.sleep(duration)
        await ws.send(json.dumps({"type": "end"}))
        print(await ws.recv())                      # {"status": "ok", "session": false}

asyncio.run(hold())
```

### `/ws/ui`

Read-only stream of UI events for any web client that wants to mirror
the kiosk. Connect and you'll receive every state change, transcription,
and LLM response. Volume / mute / theme-picker UI clients can use this.

**Server → client** message types (all JSON text frames):

| `type`            | Payload                                                                 |
|-------------------|-------------------------------------------------------------------------|
| `state`           | `{"state": "idle"\|"listening"\|"processing"\|"speaking", "wake_word": "..."}` (initial only) |
| `transcription`   | `{"text": "...", "is_partial": bool, "speaker": "human"\|"ai"\|"unknown"}` |
| `response`        | `{"text": "..."}` — what PAL said back |
| `wake`            | `{}` — wake word detected (also when chime fires) |
| `set_theme`       | `{"name": "<theme>"}` — active theme changed |
| `themes_changed`  | `{}` — kiosk should re-fetch `/api/themes` |
| `mute_sync`       | `{"muted": bool}` — mic mute state echo |
| `volume_sync`     | `{"level": 0.0–1.0}` — TTS volume |
| `show_camera`     | `{"image_b64": "...", "mime": "...", "duration_s": N, "entity_id": "..."}` |
| `stream_start`    | `{"session_id": "...", "rtsp_url": "...", "mode": "non-trickle"}` |
| `stream_stop`     | `{}` |
| `webrtc_signal`   | `{"kind": "answer"\|"candidate", "session_id": "...", ...}` |
| `play_video`      | `{"url": "...", "loop": bool, "muted": bool, "duration_s": N?}` |
| `video_stop`      | `{}` |
| `show_calendar`   | See [Calendar overlay](#calendar-overlay-payload) below |
| `hide_calendar`   | `{}` |
| `show_conversation_log` | `{"duration_s": N?}` — open the full-screen conversation log view (the client fetches rows itself via [`GET /api/conversation/log`](#get-apiconversationlog)) |
| `hide_conversation_log` | `{}` |
| `timer_countdown` | `{"timer_id": "...", "name": "Timer 1", "ends_at_epoch_ms": N, "remaining_s": N}` — show the last-10s countdown inside the orb (sent ONLY to the device that created the timer; the client ticks locally from `ends_at_epoch_ms`) |
| `timer_countdown_cancel` | `{"timer_id": "..."}` — tear the countdown down early (timer cancelled) |
| `timer_countdown_dismiss` | `{"timer_id": "..."}` — safety dismiss at fire (the client also self-dismisses at 0) |
| `ptt_active`      | `{"active": bool}` — PTT chip / orb glow on the kiosk |

**Client → server**:
- `{"type": "ping"}` → server replies `{"type": "pong"}`

#### Calendar overlay payload

```json
{
  "type": "show_calendar",
  "view": "month",
  "title": "May 2026",
  "source_label": "Family",
  "range": { "start": "2026-05-01T00:00:00+00:00", "end": "2026-06-01T00:00:00+00:00" },
  "events": [
    {
      "summary": "Standup",
      "start": "2026-05-16T09:00:00+00:00",
      "end":   "2026-05-16T09:30:00+00:00",
      "all_day": false,
      "calendar_entity": "calendar.work",
      "calendar_friendly_name": "Work",
      "color_idx": 2
    }
  ],
  "duration_s": 30
}
```

### `/ws/audio`

**Used by the RPi audio_streamer only.** Do not connect from third
parties — it's a stateful pipeline assuming exactly one peer.

* **Client → server (binary)**: raw 16-bit LE PCM audio chunks. Sample
  rate must match the server's `SAMPLE_RATE` (default 48000).
* **Client → server (JSON)**: `tts_finished`, `pong`, `mute_sync`,
  `volume_sync`, `ma_volume_adjust`, `webrtc_signal`, `snapshot`,
  `chime_*` (control), etc.
* **Server → client (JSON)**: every message type listed under
  [`/ws/ui`](#wsui) above, plus `mute_set`, `mute_toggle`,
  `mute_query`, `volume`, `volume_adjust`, `tts_start`, `tts_end`,
  `tts_cancel`, `chime_start`, `chime_end`, `ping`.
* **Server → client (binary)**: WAV bytes for TTS (between `tts_start`
  and `tts_end`) or wake chime (between `chime_start`/`chime_end`).

Documented here for completeness — the protocol evolves with the audio
pipeline and the audio_streamer.

---

## RPi audio_streamer (port 8080)

The kiosk-host service exposes its own small HTTP surface on the RPi.
It serves the kiosk UI assets and proxies a handful of AI-server
endpoints so the browser only has to talk to one origin.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/` | Kiosk `index.html` |
| `GET`  | `/style.css`, `/app.js`, `/calendar.css`, `/calendar.js`, `/fonts/...` | Kiosk static assets (image-baked) |
| `GET`  | `/api/themes` | Proxy to AI server `/api/themes` |
| `GET`  | `/api/conversation/log` | Proxy to AI server [`/api/conversation/log`](#get-apiconversationlog) (query string forwarded) |
| `GET`  | `/api/conversation/log/image` | Proxy to the log's thumbnail route (`?id=N`) |
| `GET`  | `/themes/{name}/{filename}` | Proxy to AI server theme assets |
| `GET`  | `/ws` | Kiosk WebSocket (see message table below) |
| `POST` | `/api/snapshot` | Receives JPEG from a kiosk client, forwards to AI server `/api/snapshot` |
| `POST` | `/api/music/state` | Sendspin daemon hook: tells the audio_streamer to route HW volume buttons to the media player instead of PAL TTS while a stream is active |

### Kiosk `/ws` message types

The kiosk's browser-side WebSocket. **Server (audio_streamer) → kiosk**
relays AI-server messages plus its own local sync:

| `type` | Origin | Payload |
|---|---|---|
| `state`, `transcription`, `response`, `wake`, `set_theme`, `themes_changed`, `show_camera`, `stream_*`, `webrtc_signal`, `play_video`, `video_stop`, `show_calendar`, `hide_calendar`, `show_conversation_log`, `hide_conversation_log`, `timer_countdown`, `timer_countdown_cancel`, `timer_countdown_dismiss`, `ptt_active` | relayed from AI server | as in [`/ws/ui`](#wsui) |
| `mute_sync` | local | `{"muted": bool}` |
| `volume_sync` | local | `{"level": 0.0–1.0}` |

**Kiosk → server (audio_streamer)**:

| `type` | Effect |
|---|---|
| `{"type": "mute", "muted": bool}` | Sets RPi mic mute, forwards `mute_sync` to AI server |
| `{"type": "volume", "level": float}` | Sets RPi TTS volume, forwards `volume_sync` |
| `{"type": "volume_adjust", "step": float}` | Bumps RPi volume by `step`, forwards `volume_sync` |
| `{"type": "ma_volume_adjust", "step": float}` | Forwards upstream — adjusts the Sendspin media-player volume in HA |
| `{"type": "webrtc_signal", ...}` | Forwards upstream as-is |

---

## Satellite gateway (port 8766)

`hal-gateway` is an optional internet-exposable reverse proxy that lets paired
phones reach PAL without a VPN, while the AI server stays LAN-only. It serves a
**default-deny allowlist** — only the routes below exist; everything else is
`404`. Token-gated routes are validated at the edge against the server's own
`GET /api/pair/status` (30s positive cache). `?token=` (WS + MJPEG) is accepted
in addition to the `Authorization: Bearer` header and is redacted from logs.

| Method | Path | Token | Notes |
|---|---|---|---|
| `GET`  | `/health` | — | public |
| `GET`  | `/api/themes` | — | public display catalog |
| `GET`  | `/themes/{name}/{file}` | — | public theme assets |
| `GET`  | `/api/pair/status` | ✓ | also the edge auth validator |
| `POST` | `/api/command` | ✓ | talk to PAL |
| `GET`  | `/api/satellite/tts` | ✓ | server-voice audio |
| `GET`  | `/api/satellite/stream.mjpeg` | ✓ (`?token=`) | remote camera fallback |
| `POST` | `/api/satellite/photo_frame/{start,stop}` | ✓ | phone screensaver |
| `GET`  | `/api/conversation/log` (+ `/image`) | ✓ | history view |
| `GET` `POST` | `/api/cloud_llm` | ✓ | settings (toggle allowed) |
| `POST` | `/api/pair/push-register` | ✓ | register APNs/FCM push token |
| `GET`  | `/api/push/image/{id}.jpg` | — (signed) | inline push-image thumbnail; **server**-validated HMAC, no token |
| `WS`   | `/ws/ui` | ✓ (`?token=`) | **valid token required** — no public mirror mode |

Explicitly NOT proxied (LAN-only): `/api/pair/request`, `/api/pair/redeem`
(pairing is local-only), `/api/pair/devices`, `/api/pair/revoke` (device admin
stays home-side), `/api/speak`, `/api/display`, `/api/volume`, `/api/mute`,
`/api/ptt/*`, `/api/snapshot*`, `/api/photo_frame/*` (kiosk), `/mcp`, all MQTT.
Config: `AI_SERVER_URL`, `GATEWAY_PORT` (8766),
`AUTH_CACHE_TTL` (30s), `RATE_LIMIT_RPM` (240), `TRUST_CF_IP`. Expose via
Cloudflare Tunnel / reverse proxy / Tailscale Funnel; `HAL_GATEWAY_URL` on the
AI server is handed to the app at pairing for away-from-home failover.

---

## Errors and status codes

PAL deliberately keeps HTTP status codes simple — almost everything is
`200` and the *application-level* status sits in the body's `"status"`
field. This makes shell scripting and HA `rest_command` integration
straightforward (you check the JSON, not the HTTP code).

Exceptions:
- `404`  — `GET /api/snapshot.jpg` before any snapshot is posted; theme
           file not found
- `405`  — wrong HTTP method on a route
- `422`  — Pydantic validation failure on a JSON body (e.g. wrong type)

If you need stricter HTTP semantics for a route, that's a fair feature
request — the current shape is what calling code (the desktop app, the
HA `rest_command` definitions, ad-hoc curl) was easiest to write
against.

---

## See also

- [`MQTT.md`](./MQTT.md) — every MQTT topic the bridge subscribes to
  or publishes, plus the HA Discovery entity table.
- [`THEMES.md`](./THEMES.md) — theme-author guide (CSS variable
  reference, `effect.js` API, manifest schema).
- [`README.md`](./README.md) — top-level architecture and setup.
