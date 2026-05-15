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
from typing import Any, Awaitable, Callable

log = logging.getLogger("hal.mqtt")

DISCOVERY_PREFIX = "homeassistant"


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
        # Live runtime-config callbacks (theme_day/night, voice, model,
        # wake_word, auto_theme) — wire from main.py.
        self.on_config_theme_day: Callable[[str], Awaitable[None]] | None = None
        self.on_config_theme_night: Callable[[str], Awaitable[None]] | None = None
        self.on_config_tts_voice: Callable[[str], Awaitable[None]] | None = None
        self.on_config_wake_word: Callable[[str], Awaitable[None]] | None = None
        self.on_config_ollama_model: Callable[[str], Awaitable[None]] | None = None
        self.on_config_auto_theme: Callable[[bool], Awaitable[None]] | None = None
        # Lists populated by main.py at startup so the select entities
        # advertise the right options. Empty lists publish a "none found"
        # placeholder so the entity still appears in HA.
        self.voice_options: list[str] = []
        self.model_options: list[str] = []
        self.theme_options: list[str] = ["dark", "sal", "glados", "birch", "odyssey", "japandi"]

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
        # Live config caches for republish-on-reconnect.
        self._cached_config_theme_day: str = ""
        self._cached_config_theme_night: str = ""
        self._cached_config_tts_voice: str = ""
        self._cached_config_wake_word: str = ""
        self._cached_config_ollama_model: str = ""
        self._cached_config_auto_theme: bool = True

    @property
    def connected(self) -> bool:
        return self._connected

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

        # Live runtime-config controls: 6 entities under entity_category=
        # "config" so HA groups them under the device's "Configuration"
        # section. Selects for theme_day/night, tts_voice, ollama_model;
        # text for wake_word; switch for auto_theme.

        def _select_options(opts: list[str], placeholder: str) -> list[str]:
            return list(opts) if opts else [placeholder]

        configs.append((
            f"{DISCOVERY_PREFIX}/select/{self.device_id}/config_theme_day/config",
            {
                "name": "Day Theme",
                "unique_id": f"{self.device_id}_config_theme_day",
                "state_topic":   f"{self.base}/config/theme_day/state",
                "command_topic": f"{self.base}/config/theme_day/set",
                "options": list(self.theme_options),
                "icon": "mdi:weather-sunny",
                "availability": avail,
                "device": device,
                "entity_category": "config",
            },
        ))
        configs.append((
            f"{DISCOVERY_PREFIX}/select/{self.device_id}/config_theme_night/config",
            {
                "name": "Night Theme",
                "unique_id": f"{self.device_id}_config_theme_night",
                "state_topic":   f"{self.base}/config/theme_night/state",
                "command_topic": f"{self.base}/config/theme_night/set",
                "options": list(self.theme_options),
                "icon": "mdi:weather-night",
                "availability": avail,
                "device": device,
                "entity_category": "config",
            },
        ))
        configs.append((
            f"{DISCOVERY_PREFIX}/select/{self.device_id}/config_tts_voice/config",
            {
                "name": "TTS Voice",
                "unique_id": f"{self.device_id}_config_tts_voice",
                "state_topic":   f"{self.base}/config/tts_voice/state",
                "command_topic": f"{self.base}/config/tts_voice/set",
                "options": _select_options(self.voice_options, "(no voices found)"),
                "icon": "mdi:account-voice",
                "availability": avail,
                "device": device,
                "entity_category": "config",
            },
        ))
        configs.append((
            f"{DISCOVERY_PREFIX}/select/{self.device_id}/config_ollama_model/config",
            {
                "name": "Ollama Model",
                "unique_id": f"{self.device_id}_config_ollama_model",
                "state_topic":   f"{self.base}/config/ollama_model/state",
                "command_topic": f"{self.base}/config/ollama_model/set",
                "options": _select_options(self.model_options, "(no models found)"),
                "icon": "mdi:chip",
                "availability": avail,
                "device": device,
                "entity_category": "config",
            },
        ))
        configs.append((
            f"{DISCOVERY_PREFIX}/text/{self.device_id}/config_wake_word/config",
            {
                "name": "Wake Word",
                "unique_id": f"{self.device_id}_config_wake_word",
                "state_topic":   f"{self.base}/config/wake_word/state",
                "command_topic": f"{self.base}/config/wake_word/set",
                "icon": "mdi:microphone-message",
                "availability": avail,
                "device": device,
                "mode": "text",
                "entity_category": "config",
            },
        ))
        configs.append((
            f"{DISCOVERY_PREFIX}/switch/{self.device_id}/config_auto_theme/config",
            {
                "name": "Auto Day/Night Theme",
                "unique_id": f"{self.device_id}_config_auto_theme",
                "state_topic":   f"{self.base}/config/auto_theme/state",
                "command_topic": f"{self.base}/config/auto_theme/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:theme-light-dark",
                "availability": avail,
                "device": device,
                "entity_category": "config",
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
                    for topic, payload in self._discovery_payloads():
                        await client.publish(topic, json.dumps(payload), qos=1, retain=True)
                    log.info(f"Published {len(self._discovery_payloads())} HA discovery messages")

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
                    # state, not "unknown".
                    if self._cached_config_theme_day:
                        await self.publish_config_theme_day(self._cached_config_theme_day)
                    if self._cached_config_theme_night:
                        await self.publish_config_theme_night(self._cached_config_theme_night)
                    await self.publish_config_tts_voice(self._cached_config_tts_voice)
                    if self._cached_config_wake_word:
                        await self.publish_config_wake_word(self._cached_config_wake_word)
                    if self._cached_config_ollama_model:
                        await self.publish_config_ollama_model(self._cached_config_ollama_model)
                    await self.publish_config_auto_theme(self._cached_config_auto_theme)

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
                    await client.subscribe(f"{self.base}/config/theme_day/set")
                    await client.subscribe(f"{self.base}/config/theme_night/set")
                    await client.subscribe(f"{self.base}/config/tts_voice/set")
                    await client.subscribe(f"{self.base}/config/ollama_model/set")
                    await client.subscribe(f"{self.base}/config/wake_word/set")
                    await client.subscribe(f"{self.base}/config/auto_theme/set")

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

            elif topic == f"{self.base}/config/theme_day/set":
                if self.on_config_theme_day:
                    await self.on_config_theme_day(payload.strip())

            elif topic == f"{self.base}/config/theme_night/set":
                if self.on_config_theme_night:
                    await self.on_config_theme_night(payload.strip())

            elif topic == f"{self.base}/config/tts_voice/set":
                if self.on_config_tts_voice:
                    await self.on_config_tts_voice(payload.strip())

            elif topic == f"{self.base}/config/ollama_model/set":
                if self.on_config_ollama_model:
                    await self.on_config_ollama_model(payload.strip())

            elif topic == f"{self.base}/config/wake_word/set":
                if self.on_config_wake_word:
                    await self.on_config_wake_word(payload.strip())

            elif topic == f"{self.base}/config/auto_theme/set":
                if self.on_config_auto_theme:
                    await self.on_config_auto_theme(payload.strip().upper() == "ON")

        except Exception as e:
            log.error(f"Error handling MQTT {topic}: {e}")

    async def _safe_publish(self, topic: str, payload, retain: bool = True):
        """Publish to MQTT, swallow errors if disconnected."""
        if not self._client or not self._connected:
            return
        try:
            await self._client.publish(topic, payload, qos=1, retain=retain)
        except Exception as e:
            log.debug(f"MQTT publish failed for {topic}: {e}")

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

    # Live runtime-config publishers ---------------------------------------

    async def publish_config_theme_day(self, value: str):
        self._cached_config_theme_day = value
        await self._safe_publish(f"{self.base}/config/theme_day/state", value)

    async def publish_config_theme_night(self, value: str):
        self._cached_config_theme_night = value
        await self._safe_publish(f"{self.base}/config/theme_night/state", value)

    async def publish_config_tts_voice(self, value: str):
        self._cached_config_tts_voice = value
        await self._safe_publish(f"{self.base}/config/tts_voice/state", value or "")

    async def publish_config_wake_word(self, value: str):
        self._cached_config_wake_word = value
        await self._safe_publish(f"{self.base}/config/wake_word/state", value)

    async def publish_config_ollama_model(self, value: str):
        self._cached_config_ollama_model = value
        await self._safe_publish(f"{self.base}/config/ollama_model/state", value)

    async def publish_config_auto_theme(self, value: bool):
        self._cached_config_auto_theme = bool(value)
        await self._safe_publish(
            f"{self.base}/config/auto_theme/state",
            "ON" if value else "OFF",
        )
