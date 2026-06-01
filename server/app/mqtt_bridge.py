"""MQTT bridge — exposes HAL as a Home Assistant device.

Publishes state/volume/mute/theme/snapshot, subscribes to set/speak/theme topics.
Uses HA MQTT Discovery so entities appear automatically.

Topic layout (with HAL_DEVICE_ID = "hal-default"):
    hal/hal-default/state                    sensor (idle|listening|processing|speaking)
    hal/hal-default/volume/state             number 0-100
    hal/hal-default/volume/set               <- HA writes
    hal/hal-default/mute/state               switch ON|OFF
    hal/hal-default/mute/set                 <- HA writes
    hal/hal-default/theme/state              text
    hal/hal-default/theme/set                <- HA writes
    hal/hal-default/speak                    <- HA writes (notify -> /api/speak)
    hal/hal-default/command                  <- HA writes (text -> conversation pipeline)
    hal/hal-default/image/set                <- HA writes (URL / JSON / binary JPEG)
    hal/hal-default/rtsp/set                 <- HA writes (RTSP URL / JSON)
    hal/hal-default/video/set                <- HA writes (video URL / JSON)
    hal/hal-default/camera/set               <- HA writes (camera.* entity_id / JSON)
    hal/hal-default/last_response            sensor (truncated text of last HAL utterance)
    hal/hal-default/last_response/attrs      JSON attributes (full_text, ts)
    hal/hal-default/task_metrics             JSON (timing breakdown of last command)
    hal/hal-default/snapshot                 binary JPEG (published from RPi)
    hal/hal-default/availability             "online" / "offline"
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("hal.mqtt")

DISCOVERY_PREFIX = "homeassistant"

# Sentinel returned by a ConfigEntity.parse to signal "ignore this payload"
# (e.g. an out-of-range orientation value). The dispatcher skips the callback.
_SKIP = object()

# Sentinel option string used by the fallback/router selects to represent
# "disabled" in the HA UI; maps to "" on the wire.
_NONE_SENTINEL = "(none — disabled)"


def _switch_parse(p: str) -> bool:
    return p.strip().upper() == "ON"


def _switch_serialize(v: Any) -> str:
    return "ON" if v else "OFF"


def _none_sentinel_parse(p: str) -> str:
    raw = p.strip()
    return "" if raw.startswith("(none") else raw


def _none_sentinel_serialize(v: Any) -> str:
    v = (v or "")
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    return v if v else _NONE_SENTINEL


def _clamped_int_parse(lo: int, hi: int, default: int) -> Callable[[str], int]:
    def _parse(p: str) -> int:
        try:
            n = int(float(p.strip()))
        except ValueError:
            n = default
        return max(lo, min(hi, n))
    return _parse


def _clamped_int_serialize(lo: int, hi: int, default: int) -> Callable[[Any], str]:
    def _serialize(v: Any) -> str:
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = default
        return str(max(lo, min(hi, n)))
    return _serialize


def _validated_lower_parse(allowed: set[str]) -> Callable[[str], Any]:
    def _parse(p: str) -> Any:
        val = p.strip().lower()
        return val if val in allowed else _SKIP
    return _parse


def _str_or_empty(v: Any) -> str:
    return str(v) if v is not None else ""


@dataclass
class ConfigEntity:
    """Data-driven spec for one HA "config" entity.

    Collapses the per-entity boilerplate (callback attr, cache slot,
    discovery payload, subscribe, dispatch branch, publisher) into a single
    declarative record. See CONFIG_ENTITIES below.
    """

    key: str
    platform: str                  # "text"|"number"|"switch"|"select"
    name: str
    icon: str = ""
    category: str | None = "config"
    extra: dict = field(default_factory=dict)        # static discovery fields
    parse: Callable[[str], Any] = lambda p: p.strip()
    serialize: Callable[[Any], str] = _str_or_empty
    default: Any = ""
    options_attr: str | None = None  # for selects: bridge attr with options list
    options_placeholder: str = ""    # placeholder when the options list is empty
    options_prefix_none: bool = False  # prepend the "(none)" sentinel option
    max_attr: str | None = None      # for num_ctx-style dynamic max


CONFIG_ENTITIES: list[ConfigEntity] = [
    ConfigEntity(
        key="theme_day", platform="select", name="Day Theme",
        icon="mdi:weather-sunny", options_attr="theme_options",
    ),
    ConfigEntity(
        key="theme_night", platform="select", name="Night Theme",
        icon="mdi:weather-night", options_attr="theme_options",
    ),
    ConfigEntity(
        key="tts_voice", platform="select", name="TTS Voice",
        icon="mdi:account-voice", options_attr="voice_options",
        options_placeholder="(no voices found)",
        serialize=lambda v: v or "",
    ),
    ConfigEntity(
        key="wake_word", platform="text", name="Wake Word",
        icon="mdi:microphone-message", extra={"mode": "text"},
    ),
    ConfigEntity(
        key="ollama_model", platform="select", name="Ollama Model",
        icon="mdi:chip", options_attr="model_options",
        options_placeholder="(no models found)",
    ),
    ConfigEntity(
        key="fallback_ollama_model", platform="select", name="LLM Fallback Model",
        icon="mdi:robot-confused-outline", options_attr="model_options",
        options_prefix_none=True,
        parse=_none_sentinel_parse, serialize=_none_sentinel_serialize,
    ),
    ConfigEntity(
        key="num_ctx", platform="number", name="LLM Context Size",
        icon="mdi:format-letter-case", default=32768, max_attr="num_ctx_max",
        extra={
            "min": 2048, "step": 1024,
            "unit_of_measurement": "tok", "mode": "box",
        },
        serialize=lambda v: str(v),  # already clamped by publisher
    ),
    ConfigEntity(
        key="auto_theme", platform="switch", name="Auto Day/Night Theme",
        icon="mdi:theme-light-dark", default=True,
        extra={"payload_on": "ON", "payload_off": "OFF"},
        parse=_switch_parse, serialize=_switch_serialize,
    ),
    ConfigEntity(
        key="router_enabled", platform="switch", name="Router Model Enabled",
        icon="mdi:call-split", default=False,
        extra={"payload_on": "ON", "payload_off": "OFF"},
        parse=_switch_parse, serialize=_switch_serialize,
    ),
    ConfigEntity(
        key="router_model", platform="select", name="Router Model",
        icon="mdi:directions-fork", options_attr="model_options",
        options_prefix_none=True,
        parse=_none_sentinel_parse, serialize=_none_sentinel_serialize,
    ),
    ConfigEntity(
        key="calendar_default_source", platform="text",
        name="Calendar Default Source", icon="mdi:calendar-text",
        extra={"mode": "text"}, serialize=lambda v: v or "",
    ),
    ConfigEntity(
        key="calendar_dismiss_seconds", platform="number",
        name="Calendar Dismiss Seconds", icon="mdi:timer-sand", default=30,
        extra={"min": 5, "max": 600, "step": 5, "unit_of_measurement": "s"},
        parse=_clamped_int_parse(5, 600, 30),
        serialize=_clamped_int_serialize(5, 600, 30),
    ),
    ConfigEntity(
        key="start_muted", platform="switch", name="Start Muted",
        icon="mdi:microphone-off", default=False,
        extra={"payload_on": "ON", "payload_off": "OFF"},
        parse=_switch_parse, serialize=_switch_serialize,
    ),
    ConfigEntity(
        key="photo_frame_entity", platform="text", name="Photo Frame Entity",
        icon="mdi:image-multiple-outline", extra={"mode": "text"},
        serialize=lambda v: v or "",
    ),
    ConfigEntity(
        key="photo_frame_video_url", platform="text",
        name="Photo Frame Video URL", icon="mdi:video-outline",
        extra={"mode": "text"}, serialize=lambda v: v or "",
    ),
    ConfigEntity(
        key="photo_frame_video_mode", platform="switch",
        name="Photo Frame Video Mode", icon="mdi:video-box", default=False,
        category=None,
        extra={"payload_on": "ON", "payload_off": "OFF"},
        parse=_switch_parse, serialize=_switch_serialize,
    ),
    ConfigEntity(
        key="display_auto_off_seconds", platform="number",
        name="Display Auto-off Seconds", icon="mdi:monitor-off", default=0,
        extra={"min": 0, "max": 7200, "step": 30, "unit_of_measurement": "s"},
        parse=_clamped_int_parse(0, 7200, 0),
        serialize=_clamped_int_serialize(0, 7200, 0),
    ),
    ConfigEntity(
        key="photo_frame_idle_minutes", platform="number",
        name="Photo Frame Idle Minutes", icon="mdi:image-multiple-outline",
        default=0,
        extra={"min": 0, "max": 720, "step": 1, "unit_of_measurement": "min"},
        parse=_clamped_int_parse(0, 720, 0),
        serialize=_clamped_int_serialize(0, 720, 0),
    ),
    ConfigEntity(
        key="openclaw_enabled", platform="switch", name="OpenClaw",
        icon="mdi:brain", default=False,
        extra={"payload_on": "ON", "payload_off": "OFF"},
        # NOTE: the callback receives the raw stripped string (it coerces
        # to bool itself), matching the original _handle_message branch.
        parse=lambda p: p.strip(), serialize=_switch_serialize,
    ),
    ConfigEntity(
        key="openclaw_gateway_url", platform="text",
        name="OpenClaw Gateway URL", icon="mdi:web",
        serialize=lambda v: str(v or ""),
    ),
    ConfigEntity(
        key="openclaw_workspace", platform="text", name="OpenClaw Workspace",
        icon="mdi:folder-account",
        serialize=lambda v: str(v or ""),
    ),
    ConfigEntity(
        key="display_orientation", platform="select",
        name="Display Orientation", icon="mdi:phone-rotate-landscape",
        default="portrait", extra={"options": ["portrait", "landscape"]},
        parse=_validated_lower_parse({"portrait", "landscape"}),
    ),
    ConfigEntity(
        key="orb_side", platform="select", name="Orb Side",
        icon="mdi:swap-horizontal", default="left",
        extra={"options": ["left", "right"]},
        parse=_validated_lower_parse({"left", "right"}),
    ),
]

_CONFIG_BY_KEY: dict[str, ConfigEntity] = {e.key: e for e in CONFIG_ENTITIES}


class MQTTBridge:
    """MQTT bridge with HA Discovery."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        device_id: str = "hal-default",
        device_name: str = "HAL",
    ):
        self.host = host
        self.port = port
        self.username = username or None
        self.password = password or None
        self.device_id = device_id
        self.device_name = device_name
        self.base = f"hal/{device_id}"
        self.availability_topic = f"{self.base}/availability"

        # Callbacks invoked when HA writes commands
        self.on_volume_set: Callable[[float], Awaitable[None]] | None = None
        self.on_mute_set: Callable[[bool], Awaitable[None]] | None = None
        self.on_theme_set: Callable[[str], Awaitable[None]] | None = None
        self.on_speak: Callable[[str], Awaitable[None]] | None = None
        self.on_command: Callable[[str], Awaitable[None]] | None = None
        # Image-set: payload may be raw image bytes, a URL string, or a JSON
        # wrapper. The caller decides what to do with it.
        self.on_image_set: Callable[[bytes | str], Awaitable[None]] | None = None
        # RTSP-set: text payload (RTSP URL or JSON {url, duration_s}).
        self.on_rtsp_set: Callable[[str], Awaitable[None]] | None = None
        # Video-set: text payload (HTTP video URL or JSON wrapper).
        self.on_video_set: Callable[[str], Awaitable[None]] | None = None
        # Camera-set: text payload (HA camera.* entity_id, or JSON
        # {entity_id, live?, duration_s?}). Routes to show_camera
        # (snapshot) or stream_camera (live WebRTC).
        self.on_camera_set: Callable[[str], Awaitable[None]] | None = None
        # Live runtime-config callbacks, registered per-key via
        # set_config_callback("<key>", cb) from mqtt_callbacks.wire().
        # Replaces the ~23 individual on_config_* attrs; the dispatcher in
        # _handle_message resolves the callback by entity key.
        self._config_callbacks: dict[str, Callable] = {}
        # Calendar overlay callbacks. on_calendar_show receives a dict like
        # {"view": "month"|"week"|"day", "calendar_name"?: str, "duration_s"?: int}.
        self.on_calendar_show: Callable[[dict], Awaitable[None]] | None = None
        self.on_calendar_hide: Callable[[], Awaitable[None]] | None = None
        # Push-to-Talk callbacks. Bare-trigger topics (payload ignored):
        # `start` opens a PTT session, `end` closes it (runs the LLM),
        # `cancel` closes it AND discards the captured audio.
        self.on_ptt_start: Callable[[], Awaitable[None]] | None = None
        self.on_ptt_end: Callable[[], Awaitable[None]] | None = None
        self.on_ptt_cancel: Callable[[], Awaitable[None]] | None = None
        # Photo-frame callbacks. show accepts optional JSON
        # {"entity_id":"image.xyz"} to override the configured default.
        # hide is a bare trigger.
        self.on_photo_frame_show: Callable[[dict], Awaitable[None]] | None = None
        self.on_photo_frame_hide: Callable[[], Awaitable[None]] | None = None
        # Display power callback. `on_display_set` is the imperative
        # switch payload (ON/OFF); the idle-blank timeout config lives in
        # the data-driven config registry (display_auto_off_seconds).
        self.on_display_set: Callable[[bool], Awaitable[None]] | None = None
        # Lists populated by main.py at startup so the select entities
        # advertise the right options. Empty lists publish a "none found"
        # placeholder so the entity still appears in HA.
        self.voice_options: list[str] = []
        self.model_options: list[str] = []
        self.theme_options: list[str] = [
            "dark", "sal", "glados", "matrix", "mother", "joi", "kitt",
            "birch", "odyssey", "japandi", "forest", "sunset",
        ]

        self._client = None
        self._connected = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Cached state for republishing on connect
        self._cached_state: str = "idle"
        self._cached_volume: float = 0.7
        self._cached_muted: bool = False
        self._cached_theme: str = "dark"
        self._cached_last_response: str = ""
        self._cached_task_metrics: dict | None = None
        # Live config caches for republish-on-reconnect — one dict keyed by
        # ConfigEntity.key, seeded from each entity's declared default.
        # (Callers seed real values via self._cached_config["<key>"] = ...)
        self._cached_config: dict[str, Any] = {
            e.key: e.default for e in CONFIG_ENTITIES
        }
        # Dynamic upper bound for the LLM context size — main.py refreshes
        # this from Ollama's /api/show whenever the model changes, so the
        # HA Number entity exposes exactly what the current model supports.
        self.num_ctx_max: int = 131072
        # Display power: imperative state ("on"|"off"). The idle-blank
        # timeout lives in the config registry (display_auto_off_seconds).
        self._cached_display_state: str = "on"
        self._cached_conversation_engine: str = "ollama"

    @property
    def connected(self) -> bool:
        return self._connected

    def _config_discovery(self, entity: ConfigEntity) -> tuple[str, dict]:
        """Build the (config_topic, payload) for one data-driven config entity.

        Reproduces the exact discovery payload the per-entity hand-written
        blocks used to emit. Key order is irrelevant (the golden test
        compares parsed dicts), but every key/value is preserved.
        """
        device = self._device_block()
        avail = [{"topic": self.availability_topic}]
        topic = (
            f"{DISCOVERY_PREFIX}/{entity.platform}/{self.device_id}/"
            f"config_{entity.key}/config"
        )
        payload: dict = {
            "name": entity.name,
            "unique_id": f"{self.device_id}_config_{entity.key}",
            "state_topic": f"{self.base}/config/{entity.key}/state",
            "command_topic": f"{self.base}/config/{entity.key}/set",
        }
        # Selects pull their option list dynamically at discovery time.
        if entity.platform == "select" and entity.options_attr:
            opts = list(getattr(self, entity.options_attr) or [])
            if entity.options_prefix_none:
                payload["options"] = [_NONE_SENTINEL] + opts
            elif opts:
                payload["options"] = opts
            else:
                payload["options"] = [entity.options_placeholder]
        # Static discovery fields (min/step/unit/mode, payload_on/off,
        # explicit options for fixed-option selects, etc.).
        payload.update(entity.extra)
        # Dynamic upper bound for num_ctx-style numbers.
        if entity.max_attr:
            payload["max"] = max(2048, int(getattr(self, entity.max_attr)))
        payload["icon"] = entity.icon
        payload["availability"] = avail
        payload["device"] = device
        if entity.category is not None:
            payload["entity_category"] = entity.category
        return topic, payload

    def _device_block(self) -> dict:
        return {
            "identifiers": [self.device_id],
            "name": self.device_name,
            "manufacturer": "HAL Voice Assistant",
            "model": "Voice Server",
        }

    def _discovery_payloads(self) -> list[tuple[str, dict]]:
        """Return [(config_topic, payload), ...] for all entities."""
        device = self._device_block()
        avail = [{"topic": self.availability_topic}]
        configs: list[tuple[str, dict]] = []

        # State sensor (enum)
        configs.append((
            f"{DISCOVERY_PREFIX}/sensor/{self.device_id}/state/config",
            {
                "name": "State",
                "unique_id": f"{self.device_id}_state",
                "state_topic": f"{self.base}/state",
                "icon": "mdi:robot",
                "availability": avail,
                "device": device,
            },
        ))

        # Last response — read-only sensor exposing the last thing HAL said.
        # HA caps sensor state at 255 chars; longer responses are
        # truncated. The full text is stored in the json_attribute.
        configs.append((
            f"{DISCOVERY_PREFIX}/sensor/{self.device_id}/last_response/config",
            {
                "name": "Last Response",
                "unique_id": f"{self.device_id}_last_response",
                "state_topic": f"{self.base}/last_response",
                "json_attributes_topic": f"{self.base}/last_response/attrs",
                "icon": "mdi:message-text",
                "availability": avail,
                "device": device,
            },
        ))

        # Conversation engine diagnostic sensor
        configs.append((
            f"{DISCOVERY_PREFIX}/sensor/{self.device_id}/conversation_engine/config",
            {
                "name": "Conversation Engine",
                "unique_id": f"{self.device_id}_conversation_engine",
                "state_topic": f"{self.base}/conversation_engine",
                "json_attributes_topic": f"{self.base}/conversation_engine/attrs",
                "icon": "mdi:brain",
                "availability": avail,
                "device": device,
                "entity_category": "diagnostic",
            },
        ))

        # Volume number
        configs.append((
            f"{DISCOVERY_PREFIX}/number/{self.device_id}/volume/config",
            {
                "name": "Volume",
                "unique_id": f"{self.device_id}_volume",
                "state_topic": f"{self.base}/volume/state",
                "command_topic": f"{self.base}/volume/set",
                "min": 0,
                "max": 100,
                "step": 1,
                "unit_of_measurement": "%",
                "icon": "mdi:volume-high",
                "availability": avail,
                "device": device,
            },
        ))

        # Mute switch
        configs.append((
            f"{DISCOVERY_PREFIX}/switch/{self.device_id}/mute/config",
            {
                "name": "Mute",
                "unique_id": f"{self.device_id}_mute",
                "state_topic": f"{self.base}/mute/state",
                "command_topic": f"{self.base}/mute/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "icon": "mdi:microphone-off",
                "availability": avail,
                "device": device,
            },
        ))

        # Theme select
        configs.append((
            f"{DISCOVERY_PREFIX}/select/{self.device_id}/theme/config",
            {
                "name": "Theme",
                "unique_id": f"{self.device_id}_theme",
                "state_topic": f"{self.base}/theme/state",
                "command_topic": f"{self.base}/theme/set",
                "options": list(self.theme_options),
                "icon": "mdi:palette",
                "availability": avail,
                "device": device,
            },
        ))

        # Camera (snapshot)
        configs.append((
            f"{DISCOVERY_PREFIX}/camera/{self.device_id}/snapshot/config",
            {
                "name": "Display",
                "unique_id": f"{self.device_id}_snapshot",
                "topic": f"{self.base}/snapshot",
                "icon": "mdi:monitor",
                "availability": avail,
                "device": device,
            },
        ))

        # TTS announce — text input that HA writes to. Trigger TTS via a
        # button + service call pattern: the simpler approach is a "text"
        # entity HA can push to.
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/speak/config",
            {
                "name": "Speak",
                "unique_id": f"{self.device_id}_speak",
                "command_topic": f"{self.base}/speak",
                "icon": "mdi:bullhorn",
                "availability": avail,
                "device": device,
                # No state_topic — write-only
                "mode": "text",
            },
        ))

        # Command — text input that runs through the conversation pipeline
        # (LLM with tools), as if the user had spoken it.
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/command/config",
            {
                "name": "Command",
                "unique_id": f"{self.device_id}_command",
                "command_topic": f"{self.base}/command",
                "icon": "mdi:console",
                "availability": avail,
                "device": device,
                "mode": "text",
            },
        ))

        # Show Image — URL (or short JSON) -> orb. Same backing topic as
        # the binary image/set channel; this entity is the URL-friendly UI
        # for HA automations and openclaw skills. HA caps text-entity `max`
        # at 255, so we use the default — fits a typical URL and a small
        # JSON wrapper. For longer payloads use the MQTT topic directly.
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/show_image/config",
            {
                "name": "Show Image",
                "unique_id": f"{self.device_id}_show_image",
                "command_topic": f"{self.base}/image/set",
                "icon": "mdi:image",
                "availability": avail,
                "device": device,
                "mode": "text",
            },
        ))

        # Stream RTSP — paste an RTSP URL to start a WebRTC live stream
        # in the orb (via the go2rtc sidecar). Default duration 5 min.
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/stream_rtsp/config",
            {
                "name": "Stream RTSP",
                "unique_id": f"{self.device_id}_stream_rtsp",
                "command_topic": f"{self.base}/rtsp/set",
                "icon": "mdi:cctv",
                "availability": avail,
                "device": device,
                "mode": "text",
            },
        ))

        # Play Video — paste an HTTP video URL (MP4 / WebM / HLS) to play
        # in the orb. JSON wrapper supports loop/muted/duration_s.
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/play_video/config",
            {
                "name": "Play Video",
                "unique_id": f"{self.device_id}_play_video",
                "command_topic": f"{self.base}/video/set",
                "icon": "mdi:play-circle",
                "availability": avail,
                "device": device,
                "mode": "text",
            },
        ))

        # Show Camera — paste a HA camera.* OR image.* entity_id to show
        # it in the orb (default 150 s). JSON wrapper supports
        # `{"entity_id":"...", "live": true, "duration_s": N}` to open a
        # live WebRTC stream instead (camera.* only; live=true on
        # image.* entities is ignored — they're snapshots by nature).
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/show_camera/config",
            {
                "name": "Show Camera",
                "unique_id": f"{self.device_id}_show_camera",
                "command_topic": f"{self.base}/camera/set",
                "icon": "mdi:cctv",
                "availability": avail,
                "device": device,
                "mode": "text",
            },
        ))

        # Per-task timing diagnostics. All sensors read from one JSON
        # topic via value_template — fewer publishes, easier to extend.
        metrics_topic = f"{self.base}/task_metrics"
        diag_sensors = [
            ("task_total_s",      "Last Task Duration",     "mdi:timer-outline",      "s",       "duration"),
            ("llm_total_s",       "Last LLM Duration",      "mdi:brain",              "s",       "duration"),
            ("tools_total_s",     "Last Tools Duration",    "mdi:tools",              "s",       "duration"),
            ("memory_recall_s",   "Last Memory Recall",     "mdi:book-search",        "s",       "duration"),
            ("memory_remember_s", "Last Memory Remember",   "mdi:bookmark-plus",      "s",       "duration"),
            ("tts_s",             "Last TTS Duration",      "mdi:account-voice",      "s",       "duration"),
            ("rounds",            "Last LLM Rounds",        "mdi:rotate-3d-variant",  None,      None),
            ("gen_n",             "Last Generated Tokens",  "mdi:counter",            "tokens",  None),
            ("gen_tps",           "Last Generation Speed",  "mdi:speedometer",        "t/s",     None),
            ("prompt_tps",        "Last Prompt Eval Speed", "mdi:speedometer",        "t/s",     None),
            ("model",             "Last Model",             "mdi:chip",               None,      None),
        ]
        for key, name, icon, unit, device_class in diag_sensors:
            payload = {
                "name": name,
                "unique_id": f"{self.device_id}_{key}",
                "state_topic": metrics_topic,
                "value_template": "{{ value_json." + key + " }}",
                "icon": icon,
                "availability": avail,
                "device": device,
                "entity_category": "diagnostic",
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            configs.append((
                f"{DISCOVERY_PREFIX}/sensor/{self.device_id}/{key}/config",
                payload,
            ))

        # Live runtime-config controls: data-driven config entities,
        # emitted from the CONFIG_ENTITIES registry. Each lands under
        # entity_category="config" (except where category=None) so HA
        # groups them under the device's Configuration section.
        for _entity in CONFIG_ENTITIES:
            configs.append(self._config_discovery(_entity))


        # Calendar overlay — three buttons (month/week/day) + a "Hide
        # Calendar" button, plus a text entity for the default calendar
        # source and a number for the auto-dismiss timeout. The buttons
        # all publish to a single command topic; the view is encoded in
        # the payload via `command_template` so HA's button entity (which
        # has no payload field by itself) can carry it.
        for view, label, icon in (
            ("month", "Show Calendar — Month", "mdi:calendar-month"),
            ("week",  "Show Calendar — Week",  "mdi:calendar-week"),
            ("day",   "Show Calendar — Day",   "mdi:calendar-today"),
        ):
            configs.append((
                f"{DISCOVERY_PREFIX}/button/{self.device_id}/show_calendar_{view}/config",
                {
                    "name": label,
                    "unique_id": f"{self.device_id}_show_calendar_{view}",
                    "command_topic": f"{self.base}/calendar/show/set",
                    "payload_press": json.dumps({"view": view}),
                    "icon": icon,
                    "availability": avail,
                    "device": device,
                },
            ))
        configs.append((
            f"{DISCOVERY_PREFIX}/button/{self.device_id}/hide_calendar/config",
            {
                "name": "Hide Calendar",
                "unique_id": f"{self.device_id}_hide_calendar",
                "command_topic": f"{self.base}/calendar/hide/set",
                "payload_press": "",
                "icon": "mdi:calendar-remove",
                "availability": avail,
                "device": device,
            },
        ))
        # Display power: a top-level switch (not in Configuration) so it's
        # one tap from the device card, plus a Configuration-section
        # number for the idle-blank timeout.
        configs.append((
            f"{DISCOVERY_PREFIX}/switch/{self.device_id}/display/config",
            {
                "name": "Display",
                "unique_id": f"{self.device_id}_display",
                "state_topic":   f"{self.base}/display/state",
                "command_topic": f"{self.base}/display/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:monitor",
                "availability": avail,
                "device": device,
            },
        ))



        # Push-to-Talk: three button entities under the device's
        # Configuration section. HA dashboards, automations, and
        # Zigbee/Z-Wave remotes routed through HA can press them.
        # Triggers are bare — payload is ignored on the server side.
        for action, label, icon in (
            ("start",  "PTT Start",  "mdi:microphone"),
            ("end",    "PTT End",    "mdi:microphone-off"),
            ("cancel", "PTT Cancel", "mdi:microphone-question"),
        ):
            configs.append((
                f"{DISCOVERY_PREFIX}/button/{self.device_id}/ptt_{action}/config",
                {
                    "name": label,
                    "unique_id": f"{self.device_id}_ptt_{action}",
                    "command_topic": f"{self.base}/ptt/{action}",
                    "payload_press": "PRESS",
                    "icon": icon,
                    "availability": avail,
                    "device": device,
                    "entity_category": "config",
                },
            ))

        # Photo frame: two bare-trigger buttons + one text entity for the
        # configured image entity_id. Pattern mirrors the calendar
        # buttons + calendar_default_source.
        # User-facing buttons live under the device's Controls section
        # (no entity_category) so they're surfaced next to Volume / Mute /
        # Theme, not buried under Configuration.
        for action, label, icon in (
            ("show", "Show Photo Frame", "mdi:image-frame"),
            ("hide", "Hide Photo Frame", "mdi:image-off-outline"),
        ):
            configs.append((
                f"{DISCOVERY_PREFIX}/button/{self.device_id}/photo_frame_{action}/config",
                {
                    "name": label,
                    "unique_id": f"{self.device_id}_photo_frame_{action}",
                    "command_topic": f"{self.base}/photo_frame/{action}/set",
                    "payload_press": "PRESS",
                    "icon": icon,
                    "availability": avail,
                    "device": device,
                },
            ))

        return configs

    async def start(self):
        """Start the MQTT client task."""
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self):
        """Main loop: connect, publish discovery, subscribe, reconnect on failure."""
        try:
            import aiomqtt
        except ImportError:
            log.error("aiomqtt not installed; MQTT bridge disabled")
            return

        retry_delay = 5
        while not self._stop.is_set():
            try:
                log.info(f"Connecting to MQTT broker {self.host}:{self.port}...")
                async with aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    will=aiomqtt.Will(
                        topic=self.availability_topic,
                        payload="offline",
                        qos=1,
                        retain=True,
                    ),
                ) as client:
                    self._client = client
                    self._connected = True
                    retry_delay = 5
                    log.info("MQTT connected")

                    # Publish HA discovery messages
                    await self.publish_discovery()

                    # Mark online
                    await client.publish(self.availability_topic, "online", qos=1, retain=True)

                    # Republish cached state
                    await self.publish_state(self._cached_state)
                    await self.publish_volume(self._cached_volume)
                    await self.publish_mute(self._cached_muted)
                    await self.publish_theme(self._cached_theme)
                    # Always publish a state for last_response so the sensor
                    # appears in HA even before HAL has spoken. Empty string
                    # is fine — HA shows it as blank rather than "unknown".
                    await self.publish_last_response(self._cached_last_response)
                    if self._cached_task_metrics:
                        await self.publish_task_metrics(self._cached_task_metrics)
                    # Live runtime-config — publish the cached values so
                    # the HA selects/text/switch start with their real
                    # state, not "unknown". Data-driven: one pass over the
                    # registry. The imperative display switch isn't a config
                    # entity, so it's published separately.
                    for _entity in CONFIG_ENTITIES:
                        await self.publish_config(
                            _entity.key, self._cached_config[_entity.key]
                        )
                    await self.publish_display_state(self._cached_display_state)
                    await self.publish_conversation_engine(
                        self._cached_conversation_engine
                    )

                    # Subscribe to command topics
                    await client.subscribe(f"{self.base}/volume/set")
                    await client.subscribe(f"{self.base}/mute/set")
                    await client.subscribe(f"{self.base}/theme/set")
                    await client.subscribe(f"{self.base}/speak")
                    await client.subscribe(f"{self.base}/command")
                    await client.subscribe(f"{self.base}/image/set")
                    await client.subscribe(f"{self.base}/rtsp/set")
                    await client.subscribe(f"{self.base}/video/set")
                    await client.subscribe(f"{self.base}/camera/set")
                    # Data-driven config-entity set topics.
                    for _entity in CONFIG_ENTITIES:
                        await client.subscribe(
                            f"{self.base}/config/{_entity.key}/set"
                        )
                    await client.subscribe(f"{self.base}/calendar/show/set")
                    await client.subscribe(f"{self.base}/calendar/hide/set")
                    await client.subscribe(f"{self.base}/ptt/start")
                    await client.subscribe(f"{self.base}/ptt/end")
                    await client.subscribe(f"{self.base}/ptt/cancel")
                    await client.subscribe(f"{self.base}/photo_frame/show/set")
                    await client.subscribe(f"{self.base}/photo_frame/hide/set")
                    await client.subscribe(f"{self.base}/display/set")

                    # Listen for messages
                    async for msg in client.messages:
                        await self._handle_message(str(msg.topic), msg.payload)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"MQTT error: {e}; reconnecting in {retry_delay}s")
                self._connected = False
                self._client = None
                try:
                    await asyncio.sleep(retry_delay)
                except asyncio.CancelledError:
                    break
                retry_delay = min(retry_delay * 2, 60)

        # Mark offline on shutdown
        self._connected = False

    async def _handle_message(self, topic: str, raw_payload):
        # image/set carries arbitrary bytes (binary JPEG) OR text (URL or
        # JSON). Pass it through raw so the handler can decide.
        if topic == f"{self.base}/image/set":
            if self.on_image_set:
                payload: bytes | str
                if isinstance(raw_payload, (bytes, bytearray)):
                    payload = bytes(raw_payload)
                else:
                    payload = str(raw_payload)
                try:
                    await self.on_image_set(payload)
                except Exception as e:
                    log.error(f"Error handling MQTT {topic}: {e}")
            return

        try:
            payload = raw_payload.decode("utf-8") if isinstance(raw_payload, (bytes, bytearray)) else str(raw_payload)
        except Exception:
            payload = ""

        log.debug(f"MQTT recv {topic}: {payload[:80]}")

        try:
            # Data-driven config-entity dispatch: any hal/<id>/config/<key>/set
            # topic resolves to its ConfigEntity, parses the payload per its
            # spec, and invokes the registered callback. _SKIP (e.g. an
            # invalid orientation) silently drops the message.
            cfg_prefix = f"{self.base}/config/"
            if topic.startswith(cfg_prefix) and topic.endswith("/set"):
                key = topic[len(cfg_prefix):-len("/set")]
                entity = _CONFIG_BY_KEY.get(key)
                if entity is not None:
                    cb = self._config_callbacks.get(key)
                    if cb is not None:
                        if entity.max_attr:
                            # num_ctx-style: clamp against the dynamic ceiling.
                            try:
                                n = int(float(payload.strip()))
                            except ValueError:
                                n = int(entity.default)
                            lo = int(entity.extra.get("min", 0))
                            value = max(lo, min(int(getattr(self, entity.max_attr)), n))
                        else:
                            value = entity.parse(payload)
                        if value is not _SKIP:
                            await cb(value)
                    return

            if topic == f"{self.base}/volume/set":
                level = max(0.0, min(1.0, float(payload) / 100.0))
                if self.on_volume_set:
                    await self.on_volume_set(level)

            elif topic == f"{self.base}/mute/set":
                muted = payload.strip().upper() == "ON"
                if self.on_mute_set:
                    await self.on_mute_set(muted)

            elif topic == f"{self.base}/theme/set":
                theme = payload.strip()
                if self.on_theme_set:
                    await self.on_theme_set(theme)

            elif topic == f"{self.base}/speak":
                if payload and self.on_speak:
                    await self.on_speak(payload)

            elif topic == f"{self.base}/command":
                if payload and self.on_command:
                    await self.on_command(payload)

            elif topic == f"{self.base}/rtsp/set":
                if payload and self.on_rtsp_set:
                    await self.on_rtsp_set(payload)

            elif topic == f"{self.base}/video/set":
                if payload and self.on_video_set:
                    await self.on_video_set(payload)

            elif topic == f"{self.base}/camera/set":
                if payload and self.on_camera_set:
                    await self.on_camera_set(payload)









            elif topic == f"{self.base}/calendar/show/set":
                args: dict = {}
                stripped = payload.strip()
                if stripped:
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            args = parsed
                        elif isinstance(parsed, str):
                            args = {"view": parsed}
                    except json.JSONDecodeError:
                        # Bare string payload like "month" or "Family"
                        if stripped.lower() in ("month", "week", "day"):
                            args = {"view": stripped.lower()}
                        else:
                            args = {"calendar_name": stripped}
                if self.on_calendar_show:
                    await self.on_calendar_show(args)

            elif topic == f"{self.base}/calendar/hide/set":
                if self.on_calendar_hide:
                    await self.on_calendar_hide()




            elif topic == f"{self.base}/display/set":
                if self.on_display_set:
                    await self.on_display_set(payload.strip().upper() == "ON")










            elif topic == f"{self.base}/ptt/start":
                if self.on_ptt_start:
                    await self.on_ptt_start()

            elif topic == f"{self.base}/ptt/end":
                if self.on_ptt_end:
                    await self.on_ptt_end()

            elif topic == f"{self.base}/ptt/cancel":
                if self.on_ptt_cancel:
                    await self.on_ptt_cancel()

            elif topic == f"{self.base}/photo_frame/show/set":
                args: dict = {}
                stripped = payload.strip()
                if stripped:
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            args = parsed
                        elif isinstance(parsed, str) and parsed.startswith(("image.", "camera.")):
                            args = {"entity_id": parsed}
                    except json.JSONDecodeError:
                        # bare entity_id like "image.weather_radar"
                        if stripped.startswith(("image.", "camera.")):
                            args = {"entity_id": stripped}
                        # else: ignore (e.g. "PRESS" from a button entity)
                if self.on_photo_frame_show:
                    await self.on_photo_frame_show(args)

            elif topic == f"{self.base}/photo_frame/hide/set":
                if self.on_photo_frame_hide:
                    await self.on_photo_frame_hide()




        except Exception as e:
            log.error(f"Error handling MQTT {topic}: {e}")

    async def publish_discovery(self):
        """Re-emit every HA Discovery config payload (retained).

        Called on connect and any time the catalog of theme/voice/model
        options changes — HA re-reads the retained configs and
        refreshes the entity options.
        """
        payloads = self._discovery_payloads()
        for topic, payload in payloads:
            await self._safe_publish(topic, json.dumps(payload))
        log.info(f"Published {len(payloads)} HA discovery messages")

    async def _safe_publish(self, topic: str, payload, retain: bool = True):
        """Publish to MQTT, swallow errors if disconnected."""
        if not self._client or not self._connected:
            return
        try:
            await self._client.publish(topic, payload, qos=1, retain=retain)
        except Exception as e:
            log.debug(f"MQTT publish failed for {topic}: {e}")

    # Generic data-driven config API ---------------------------------------

    def set_config_callback(self, key: str, cb: Callable) -> None:
        """Register the handler invoked when HA writes config/<key>/set."""
        self._config_callbacks[key] = cb

    async def publish_config(self, key: str, value) -> None:
        """Publish a config entity's state (cached for republish-on-connect).

        Serialization is per-entity. num_ctx is clamped to the dynamic
        ceiling here (it depends on self.num_ctx_max).
        """
        entity = _CONFIG_BY_KEY.get(key)
        if entity is None:
            log.warning(f"publish_config: unknown config key {key!r}")
            return
        if entity.max_attr:
            # num_ctx-style number: clamp to [min, dynamic-max] before publish.
            lo = int(entity.extra.get("min", 0))
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = int(entity.default)
            value = max(lo, min(int(getattr(self, entity.max_attr)), n))
        self._cached_config[key] = value
        await self._safe_publish(
            f"{self.base}/config/{key}/state", entity.serialize(value)
        )

    # Public publishers (called from server when state changes)

    async def publish_state(self, state: str):
        self._cached_state = state
        await self._safe_publish(f"{self.base}/state", state)

    async def publish_volume(self, level: float):
        """level is 0.0..1.0"""
        self._cached_volume = level
        await self._safe_publish(f"{self.base}/volume/state", str(int(round(level * 100))))

    async def publish_mute(self, muted: bool):
        self._cached_muted = muted
        await self._safe_publish(f"{self.base}/mute/state", "ON" if muted else "OFF")

    async def publish_theme(self, theme: str):
        self._cached_theme = theme
        await self._safe_publish(f"{self.base}/theme/state", theme)

    async def publish_task_metrics(self, metrics: dict):
        """Publish the per-task timing breakdown for the diagnostic sensors.

        Single JSON message; the discovery sensors fan out via
        value_template. Cached for republish on reconnect.
        """
        self._cached_task_metrics = metrics
        await self._safe_publish(f"{self.base}/task_metrics", json.dumps(metrics))

    async def publish_last_response(self, text: str):
        """Publish HAL's most recent utterance.

        State topic carries the truncated string (HA's 255-char limit on
        sensor state); attributes topic carries the full text plus an
        ISO timestamp for richer template access.
        """
        import datetime as _dt
        self._cached_last_response = text
        truncated = text if len(text) <= 250 else text[:247] + "..."
        await self._safe_publish(f"{self.base}/last_response", truncated)
        attrs = json.dumps({
            "full_text": text,
            "ts": _dt.datetime.utcnow().isoformat() + "Z",
        })
        await self._safe_publish(f"{self.base}/last_response/attrs", attrs)

    async def publish_snapshot(self, jpeg_bytes: bytes):
        """Publish a JPEG snapshot of the display."""
        await self._safe_publish(f"{self.base}/snapshot", jpeg_bytes)

    # Live runtime-config publishers: collapsed into the generic
    # publish_config(key, value) above. Only the imperative display-state
    # publisher and the num_ctx-max updater remain (they aren't plain
    # config entities).

    async def update_num_ctx_max(self, value: int):
        """Set the dynamic upper bound and republish discovery so the HA
        Number entity reflects the current model's true ceiling."""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return
        if n < 2048:
            return
        if n == self.num_ctx_max:
            return
        self.num_ctx_max = n
        try:
            await self.publish_discovery()
        except Exception as e:
            log.warning(f"num_ctx_max republish_discovery failed: {e}")
        # If the current value exceeds the new ceiling, clamp + republish.
        if int(self._cached_config["num_ctx"]) > n:
            await self.publish_config("num_ctx", n)

    async def publish_display_state(self, value: str):
        v = (value or "").strip().lower()
        if v not in ("on", "off"):
            v = "on"
        self._cached_display_state = v
        await self._safe_publish(
            f"{self.base}/display/state",
            "ON" if v == "on" else "OFF",
        )

    async def publish_conversation_engine(
        self, engine: str, *, duration_s: float = 0.0, model: str = ""
    ):
        self._cached_conversation_engine = engine
        await self._safe_publish(
            f"{self.base}/conversation_engine",
            engine,
        )
        import json as _json
        await self._safe_publish(
            f"{self.base}/conversation_engine/attrs",
            _json.dumps({
                "engine": engine,
                "model": model,
                "duration_s": round(duration_s, 2),
            }),
        )

