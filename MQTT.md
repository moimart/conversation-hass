# PAL — MQTT Reference

PAL connects to your MQTT broker (`MQTT_BROKER_HOST` / `MQTT_BROKER_PORT`)
as a single device with `device_id = HAL_DEVICE_ID` (default
`hal-default`) and publishes Home Assistant Discovery payloads for
every entity so it appears in HA without manual configuration.

This document is the **reference** for that surface — every topic the
bridge subscribes to, every topic it publishes, the payload format for
each, and the resulting HA entities.

* **Broker**: any MQTT broker (Mosquitto, EMQX, the Mosquitto add-on
  in HA — anything Paho/aiomqtt can speak v3.1.1 to)
* **Topic prefix**: `hal/<device_id>` (default `hal/hal-default`)
  — referred to as `<base>` below.
* **Discovery prefix**: `homeassistant` (HA's default; not currently
  configurable)
* **Will**: `<base>/availability` is set to `offline` on disconnect
  (retained, QoS 1) and `online` on connect.
* **All published state topics**: QoS 1, retained.
* **Authentication**: optional via `MQTT_USERNAME` / `MQTT_PASSWORD`.

---

## Table of contents

- [Topic layout overview](#topic-layout-overview)
- [Subscribed topics (HA → PAL)](#subscribed-topics)
  - [Quick controls](#quick-controls)
  - [Conversation triggers](#conversation-triggers)
  - [Display payloads](#display-payloads)
  - [Push-to-Talk](#push-to-talk)
  - [Calendar overlay](#calendar-overlay)
  - [Conversation log](#conversation-log)
  - [Live runtime config](#live-runtime-config)
- [Published topics (PAL → HA)](#published-topics)
- [HA Discovery entities](#ha-discovery-entities)
- [HA automation snippets](#ha-automation-snippets)
- [Direct MQTT publishing (mosquitto_pub examples)](#direct-mqtt-publishing-mosquitto_pub-examples)
- [Notes on retained payloads & cold-starts](#notes-on-retained-payloads--cold-starts)

---

## Topic layout overview

```
hal/<device_id>/
├── availability                              online / offline   (will, retained)
├── state                                     idle | listening | processing | speaking
├── snapshot                                  binary JPEG (camera entity)
├── last_response                             text (≤250 chars, state)
├── last_response/attrs                       {full_text, ts}
├── task_metrics                              JSON (11 diagnostic sensor fields)
│
├── volume/{state,set}                        0-100   (number)
├── mute/{state,set}                          ON|OFF  (switch)
├── theme/{state,set}                         theme name (select)
│
├── speak                                     write-only text — speak verbatim
├── command                                   write-only text — run through LLM
├── image/set                                 URL / JSON wrapper / binary JPEG
├── rtsp/set                                  rtsp:// URL or JSON wrapper
├── video/set                                 http(s) video URL or JSON wrapper
├── camera/set                                camera.* / image.* entity_id or JSON
│
├── calendar/show/set                         JSON {view, calendar_name?, duration_s?}
├── calendar/hide/set                         bare trigger
│
├── conversation_log/show/set                 bare trigger
├── conversation_log/hide/set                 bare trigger
│
├── ptt/start                                 bare trigger
├── ptt/end                                   bare trigger
├── ptt/cancel                                bare trigger
│
└── config/                                   live runtime config (state + set per key)
    ├── theme_day/{state,set}
    ├── theme_night/{state,set}
    ├── tts_voice/{state,set}
    ├── ollama_model/{state,set}
    ├── wake_word/{state,set}
    ├── auto_theme/{state,set}                ON|OFF
    ├── start_muted/{state,set}               ON|OFF
    ├── calendar_default_source/{state,set}
    ├── calendar_dismiss_seconds/{state,set}  5-600
    ├── timer_name_template/{state,set}
    └── timer_announce_template/{state,set}
```

---

## Subscribed topics

HA (or any MQTT publisher on your LAN) writes to these. PAL reads.

### Quick controls

| Topic | Payload | Effect |
|---|---|---|
| `<base>/volume/set` | `0`-`100` (string) | Sets RPi TTS volume (mapped to `0.0`-`1.0`). |
| `<base>/mute/set`   | `ON` / `OFF`       | Mutes/unmutes the RPi mic. |
| `<base>/theme/set`  | theme name (e.g. `material_you`) | Switches active kiosk theme. |

### Conversation triggers

| Topic | Payload | Effect |
|---|---|---|
| `<base>/speak` | text | Speak the text **verbatim** through TTS — no LLM. Equivalent to [`POST /api/speak`](./API.md#post-apispeak). |
| `<base>/command` | text | Send the text through the LLM with tools, then TTS the response. Equivalent to [`POST /api/command`](./API.md#post-apicommand). |

### Display payloads

These all push something onto the orb / kiosk. Multiple shapes are
accepted to make HA automations easy.

| Topic | Payloads accepted | Effect |
|---|---|---|
| `<base>/image/set`   | • `http(s)://...` URL<br>• JSON `{"url": "...", "duration_s": N}`<br>• Raw `image/jpeg` bytes | Show the image on the orb for `duration_s` seconds (default 60 — see `show_image` tool). |
| `<base>/rtsp/set`    | • `rtsp://...` URL<br>• JSON `{"rtsp_url": "...", "duration_s": N}` | Start a WebRTC stream from any RTSP source (via go2rtc sidecar). |
| `<base>/video/set`   | • `http(s)://...` URL (mp4/webm/m3u8)<br>• JSON `{"url": "...", "loop": bool, "muted": bool, "duration_s": N}` | Play the video file in the orb. |
| `<base>/camera/set`  | • `camera.front_door` entity_id<br>• `image.weather_radar` entity_id<br>• JSON `{"entity_id": "...", "live": true, "duration_s": N}` | Snapshot (default) or live WebRTC stream (`live: true` for `camera.*`). |

### Push-to-Talk

All three are **bare triggers** — any payload (typically `"PRESS"` from
HA discovery buttons) works.

| Topic | Effect |
|---|---|
| `<base>/ptt/start`  | Open a PTT session (bypass wake word, auto-unmute, etc.) — see [`POST /api/ptt/start`](./API.md#post-apipttstart). |
| `<base>/ptt/end`    | Close the session, run the LLM on the captured audio. |
| `<base>/ptt/cancel` | Close the session and discard the captured audio. |

> **Heads-up on HA dashboard buttons**: HA discovery buttons are
> *press-only* (no built-in release event). For real hold-to-talk
> behaviour, use a custom Lovelace card with `tap_action` +
> `hold_action`, an automation tied to a Zigbee/Z-Wave button's press
> AND release events, the desktop app's PTT mouse button, or the
> [`/ws/ptt`](./API.md#wsptt) WebSocket. Pressing PTT Start alone will
> open a session that times out after 20 s if no End is published.

### Calendar overlay

| Topic | Payload | Effect |
|---|---|---|
| `<base>/calendar/show/set` | Several forms:<br>• `month` / `week` / `day` (bare string)<br>• Calendar name (bare string)<br>• JSON `{"view": "...", "calendar_name": "...", "anchor_date": "YYYY-MM-DD", "duration_s": N}` | Show the calendar overlay. Empty/no-match `calendar_name` = merge all HA calendars. Optional `anchor_date` (ISO `YYYY-MM-DD`) anchors a specific day/week/month — omit for today. |
| `<base>/calendar/hide/set` | (any) | Dismiss the overlay early. |

### Conversation log

Full-screen browsable history of every request, answer, and announcement
(PostgreSQL-backed — see the README's *Conversation log* section). Shown on
the kiosk + web mirrors; auto-dismisses after 30 s without interaction.

| Topic | Payload | Effect |
|---|---|---|
| `<base>/conversation_log/show/set` | (any) | Open the conversation log view, scrolled to the newest entries. |
| `<base>/conversation_log/hide/set` | (any) | Dismiss the view early. |

### Photo frame

Bare-trigger topics published by the two HA Discovery buttons. The
`show` topic also accepts an explicit `entity_id` to override the
configured default. Gated by [`photo_frame_entity`](#live-runtime-config)
— if neither the configured default nor the payload supplies one,
the request is a silent no-op.

| Topic | Payload | Effect |
|---|---|---|
| `<base>/photo_frame/show/set` | Several forms:<br>• `PRESS` (HA button default) — uses configured default<br>• Bare `image.weather_radar` / `camera.front_door`<br>• JSON `{"entity_id": "image.weather_radar"}` | Show the photo frame. Auto-dismisses on any kiosk activity. |
| `<base>/photo_frame/hide/set` | (any) | Dismiss early. |

### Display power (DPMS)

Real hardware power-down via the kiosk-host display tools (`wlr-randr`
/ `xset` / `vcgencmd`, auto-selected). Auto-wake fires on any incoming
kiosk activity (wake word, PTT, takeover push, TTS reply). The number
entity controls an optional idle-blank timeout; `0` = manual control
only.

| Topic | Payload | Effect |
|---|---|---|
| `<base>/display/set` | `ON` / `OFF` | Power the kiosk display on or off. |

### Live runtime config

Each key has a `<state>` topic the bridge publishes to (retained) and a
`<set>` topic PAL subscribes to. Changes persist atomically to
`server/runtime/config.json` and survive restarts.

| Key | State / Set topic | Payload | Constraints |
|---|---|---|---|
| `theme_day`              | `<base>/config/theme_day/{state,set}`              | theme name | must be a valid theme |
| `theme_night`            | `<base>/config/theme_night/{state,set}`            | theme name | must be a valid theme |
| `tts_voice`              | `<base>/config/tts_voice/{state,set}`              | voice name | must be one Wyoming advertised |
| `ollama_model`           | `<base>/config/ollama_model/{state,set}`           | model name | must be installed in Ollama |
| `wake_word`              | `<base>/config/wake_word/{state,set}`              | text       | lowercased on the server |
| `auto_theme`             | `<base>/config/auto_theme/{state,set}`             | `ON`/`OFF` | enables sun-based theme swap |
| `start_muted`            | `<base>/config/start_muted/{state,set}`            | `ON`/`OFF` | mic boots muted |
| `calendar_default_source`| `<base>/config/calendar_default_source/{state,set}`| text       | empty = merge all calendars |
| `calendar_dismiss_seconds`| `<base>/config/calendar_dismiss_seconds/{state,set}` | `5`-`600` (number) | default 30 |
| `photo_frame_entity`     | `<base>/config/photo_frame_entity/{state,set}`     | text       | HA `image.*` (or `camera.*`) entity_id; empty = feature disabled |
| `display_auto_off_seconds`| `<base>/config/display_auto_off_seconds/{state,set}` | `0`-`7200` (number) | Idle-blank timeout. `0` disables auto-off (manual control only). |
| `photo_frame_idle_minutes`| `<base>/config/photo_frame_idle_minutes/{state,set}` | `0`-`720` (number) | Auto-activate photo frame after this many idle minutes. `0` disables. |
| `cloud_llm_enabled`      | `<base>/config/cloud_llm_enabled/{state,set}`      | `ON`/`OFF` | Cloud Override: route every turn (tools included) to the selected cloud model, skipping the router + OpenClaw. **Never persisted — always boots OFF.** |
| `cloud_llm_model`        | `<base>/config/cloud_llm_model/{state,set}`        | `provider/model-id` | Options fetched live from each provider in `server/runtime/cloud_providers.json` (keys never appear on MQTT). Persists across restarts. |

---

## Published topics

PAL writes to these. HA (or anything else subscribed) reads.

| Topic | Payload | Notes |
|---|---|---|
| `<base>/availability`       | `online` / `offline` | Retained; LWT publishes `offline` on unclean disconnect. |
| `<base>/state`              | `idle` / `listening` / `processing` / `speaking` | The conversation state machine. |
| `<base>/volume/state`       | `0`-`100`            | Mirrors the RPi's TTS volume. |
| `<base>/mute/state`         | `ON` / `OFF`         | Mirrors the RPi's mic mute. |
| `<base>/theme/state`        | theme name           | Active theme. |
| `<base>/last_response`      | text (≤ 250 chars)   | Truncated for HA's 255-char sensor cap. |
| `<base>/last_response/attrs`| JSON `{"full_text": "...", "ts": "..."}` | Full text + ISO timestamp. |
| `<base>/snapshot`           | binary `image/jpeg`  | Latest kiosk snapshot (camera entity). |
| `<base>/task_metrics`       | JSON (11 fields, see below) | Per-task timing diagnostics. |
| `<base>/config/<key>/state` | per-key (see table above) | Mirrors persisted runtime config. |

### `task_metrics` payload

Published at the end of every command. Each field is exposed as its own
HA diagnostic sensor via `value_template`.

```json
{
  "task_total_s":     2.41,
  "llm_total_s":      1.83,
  "tools_total_s":    0.12,
  "memory_recall_s":  0.05,
  "memory_remember_s":0.08,
  "tts_s":            0.69,
  "rounds":           1,
  "gen_n":            72,
  "gen_tps":          41.5,
  "prompt_tps":       2103.2,
  "model":            "gemma4:e4b"
}
```

---

## HA Discovery entities

Every entity below appears under one HA device (default name `PAL`).
Discovery payloads are published retained on `homeassistant/<component>/<device_id>/<key>/config`. Republished on every PAL boot and any time
the theme catalog / voice list / model list changes.

### Sensors (read-only)

| Entity                        | Topic source             | Notes                                          |
|-------------------------------|--------------------------|------------------------------------------------|
| **State**                     | `state`                  | `idle/listening/processing/speaking`           |
| **Last Response**             | `last_response`          | text (≤ 250 chars); full text in attributes    |
| **Active Timers**             | `active_timers`          | count of running voice timers; per-timer `{name, remaining_s, ends_at}` in attributes |
| **Context Usage**             | `context_usage`          | % of `num_ctx` PAL's rolling context uses (est.); attributes: history_messages, est_tokens, summary_chars, compactions, last_compaction |
| **Last Task Duration**        | `task_metrics.task_total_s` | seconds, diagnostic                          |
| **Last LLM Duration**         | `task_metrics.llm_total_s`  | seconds, diagnostic                          |
| **Last Tools Duration**       | `task_metrics.tools_total_s` | seconds, diagnostic                         |
| **Last Memory Recall**        | `task_metrics.memory_recall_s` | seconds, diagnostic                       |
| **Last Memory Remember**      | `task_metrics.memory_remember_s` | seconds, diagnostic                     |
| **Last TTS Duration**         | `task_metrics.tts_s`     | seconds, diagnostic                            |
| **Last LLM Rounds**           | `task_metrics.rounds`    | integer, diagnostic                            |
| **Last Generated Tokens**     | `task_metrics.gen_n`     | integer, diagnostic                            |
| **Last Generation Speed**     | `task_metrics.gen_tps`   | tokens/s, diagnostic                           |
| **Last Prompt Eval Speed**    | `task_metrics.prompt_tps`| tokens/s, diagnostic                           |
| **Last Model**                | `task_metrics.model`     | string, diagnostic                             |

### Number

| Entity                          | Range  | Unit | Notes |
|---------------------------------|--------|------|-------|
| **Volume**                      | 0-100  | %    | TTS volume |
| **Calendar Dismiss Seconds**    | 5-600  | s    | Config; default 30 |

### Switch

| Entity                | Notes |
|-----------------------|-------|
| **Mute**              | RPi mic |
| **Auto Day/Night Theme** | Config; sun-based theme swap |
| **Start Muted**       | Config; mic boots muted |

### Select

| Entity         | Options |
|----------------|---------|
| **Theme**      | All installed themes |
| **Day Theme**  | Config; same options as Theme |
| **Night Theme**| Config; same options as Theme |
| **TTS Voice**  | Config; voices Wyoming announces |
| **Ollama Model** | Config; models Ollama has installed |

### Text

| Entity                       | Notes |
|------------------------------|-------|
| **Speak**                    | Write-only; speaks verbatim |
| **Command**                  | Write-only; runs through LLM |
| **Show Image**               | URL or short JSON wrapper |
| **Stream RTSP**              | RTSP URL or JSON wrapper |
| **Play Video**               | http(s) video URL or JSON wrapper |
| **Show Camera**              | `camera.*` / `image.*` entity_id |
| **Wake Word**                | Config |
| **Calendar Default Source**  | Config |
| **Timer Name Template**      | Config; `{n}` = timer number (any language) |
| **Timer Announce Template**  | Config; `{name}` = timer name |

### Button

| Entity                       | Topic / Payload                                |
|------------------------------|------------------------------------------------|
| **Show Calendar — Month**    | `calendar/show/set`  payload `{"view":"month"}` |
| **Show Calendar — Week**     | `calendar/show/set`  payload `{"view":"week"}`  |
| **Show Calendar — Day**      | `calendar/show/set`  payload `{"view":"day"}`   |
| **Hide Calendar**            | `calendar/hide/set`  payload empty             |
| **Show Conversation Log**    | `conversation_log/show/set` payload `PRESS`    |
| **Hide Conversation Log**    | `conversation_log/hide/set` payload `PRESS`    |
| **Clear Context**            | `context/clear/set` payload `PRESS` — drops PAL's rolling LLM context (history + summary); keeps long-term memory |
| **PTT Start**                | `ptt/start`          payload `PRESS`           |
| **PTT End**                  | `ptt/end`            payload `PRESS`           |
| **PTT Cancel**               | `ptt/cancel`         payload `PRESS`           |
| **Show Photo Frame**         | `photo_frame/show/set` payload `PRESS`         |
| **Hide Photo Frame**         | `photo_frame/hide/set` payload `PRESS`         |

### Camera

| Entity     | Topic       | Notes                            |
|------------|-------------|----------------------------------|
| **Display**| `snapshot`  | Latest kiosk JPEG (binary topic) |

---

## HA automation snippets

### Hold-to-talk via Zigbee/Z-Wave button

For real hold-to-talk, route a button device's separate press and
release events to PTT start/end. Example with a Zigbee2MQTT-published
remote that emits `action: "hold"` and `action: "release"`:

```yaml
- alias: PAL PTT hold
  triggers:
    - trigger: mqtt
      topic: zigbee2mqtt/desk_button/action
      payload: hold
  actions:
    - action: button.press
      target:
        entity_id: button.hal_ptt_start

- alias: PAL PTT release
  triggers:
    - trigger: mqtt
      topic: zigbee2mqtt/desk_button/action
      payload: release
  actions:
    - action: button.press
      target:
        entity_id: button.hal_ptt_end
```

### Make PAL announce things

```yaml
- alias: Announce when washing machine finishes
  triggers:
    - trigger: state
      entity_id: sensor.washing_machine_state
      to: idle
  actions:
    - action: mqtt.publish
      data:
        topic: hal/hal-default/speak
        payload: "Master, the washing machine is done."
```

### Show a camera on the orb when motion detected

```yaml
- alias: Front door motion -> show camera
  triggers:
    - trigger: state
      entity_id: binary_sensor.front_door_motion
      to: "on"
  actions:
    - action: mqtt.publish
      data:
        topic: hal/hal-default/camera/set
        payload: >-
          {"entity_id": "camera.front_door", "live": true, "duration_s": 90}
```

### Show the Family calendar in the morning

```yaml
- alias: Morning calendar
  triggers:
    - trigger: time
      at: "07:30:00"
  actions:
    - action: mqtt.publish
      data:
        topic: hal/hal-default/calendar/show/set
        payload: >-
          {"view": "day", "calendar_name": "Family", "duration_s": 60}
```

### Show tomorrow's calendar at bedtime

```yaml
- alias: Bedtime preview
  triggers:
    - trigger: time
      at: "22:30:00"
  actions:
    - action: mqtt.publish
      data:
        topic: hal/hal-default/calendar/show/set
        payload: >-
          {"view": "day", "calendar_name": "Family",
           "anchor_date": "{{ (now() + timedelta(days=1)) | as_timestamp | timestamp_custom('%Y-%m-%d') }}",
           "duration_s": 45}
```

---

## Direct MQTT publishing (mosquitto_pub examples)

For testing without going through HA. Replace `mqtt.broker.lan` and add
`-u user -P pass` if your broker needs auth.

```bash
# Toggle mute
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/mute/set -m ON

# Speak something
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/speak \
  -m "Dinner is ready"

# Run a command through the LLM
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/command \
  -m "turn off the kitchen lights"

# Show a URL image for 30 s
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/image/set \
  -m '{"url":"https://example.com/cat.jpg","duration_s":30}'

# PTT start + end (test the round trip)
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/ptt/start -m PRESS
sleep 2
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/ptt/end   -m PRESS

# Switch theme
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/theme/set -m material_you

# Adjust a runtime config key (auto-theme off)
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/config/auto_theme/set -m OFF

# Configure + show the photo frame (HA image.* entity)
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/config/photo_frame_entity/set -m image.weather_radar
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/photo_frame/show/set -m PRESS
# ...wait, then dismiss explicitly (or just touch the kiosk):
mosquitto_pub -h mqtt.broker.lan -t hal/hal-default/photo_frame/hide/set -m PRESS

# Watch every topic the device touches
mosquitto_sub -h mqtt.broker.lan -t 'hal/hal-default/#' -v
```

---

## Notes on retained payloads & cold-starts

* All `state` topics are published **retained, QoS 1**, so a fresh HA
  install picks up the current state immediately when it connects.
* The `availability` LWT means PAL going offline (network blip,
  container restart) flips every entity to `unavailable` in HA within
  a few seconds.
* On PAL startup, the bridge re-publishes every cached state in order
  (state → volume → mute → theme → last_response → task_metrics → all
  config keys), so HA never sees stale `unknown` values mid-restart.
* The `snapshot` topic is binary and may be > 100 KB per publish — the
  bridge throttles to whatever the RPi posts (typically every 60 s via
  the audio_streamer's `SNAPSHOT_INTERVAL_S` env var).

---

## See also

- [`API.md`](./API.md) — REST + WebSocket reference (the non-MQTT side
  of the same surface).
- [`THEMES.md`](./THEMES.md) — Plug-in theme authoring.
- [`README.md`](./README.md) — Top-level architecture and setup.
