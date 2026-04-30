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

        self._client = None
        self._connected = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Cached state for republishing on connect
        self._cached_state: str = "idle"
        self._cached_volume: float = 0.7
        self._cached_muted: bool = False
        self._cached_theme: str = "dark"

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
                "options": ["dark", "birch", "odyssey", "japandi"],
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

                    # Subscribe to command topics
                    await client.subscribe(f"{self.base}/volume/set")
                    await client.subscribe(f"{self.base}/mute/set")
                    await client.subscribe(f"{self.base}/theme/set")
                    await client.subscribe(f"{self.base}/speak")
                    await client.subscribe(f"{self.base}/command")

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

    async def publish_snapshot(self, jpeg_bytes: bytes):
        """Publish a JPEG snapshot of the display."""
        await self._safe_publish(f"{self.base}/snapshot", jpeg_bytes)
