"""Weather under the clock.

Polls a configured Home Assistant `weather.*` entity (like the theme scheduler
polls `sun.sun`) and pushes the temperature + condition to every display — the
kiosk and the satellites — which render it under the clock. The temperature unit
follows HA (read off the entity's `temperature_unit` attribute).

Two settings drive it (runtime_config / MQTT, see mqtt_callbacks):
  - `weather_entity`  — the `weather.*` entity id; empty → nothing shown.
  - `weather_enabled` — show/hide toggle, on by default.

The parse + message builders are pure (no network) so they're unit-testable; the
only side-effecting parts are the HA read and the broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

log = logging.getLogger("hal.weather")

POLL_MINUTES = max(1, int(os.environ.get("WEATHER_POLL_MINUTES", "10") or 10))

_HIDE_MSG = {"type": "weather_update", "show": False}


def _hide_msg() -> dict:
    """The message that tells displays to hide the weather readout."""
    return dict(_HIDE_MSG)


def build_weather_msg(condition: str, temp, unit: str) -> dict:
    """Pure: the wire message for a visible weather readout. The display rounds
    the temperature and maps `condition` (a raw HA condition string) to an icon +
    label, so presentation stays in the display layer."""
    return {
        "type": "weather_update",
        "show": True,
        "temp": temp,
        "unit": unit or "°",
        "condition": condition or "",
    }


def parse_weather_response(raw: str) -> dict | None:
    """Pure: parse an `ha_get_state` result for a weather entity into a
    `weather_update` message, or None if it can't be read.

    HA wraps the entity as `{"data": {"state": <condition>, "attributes":
    {"temperature": <num>, "temperature_unit": "°C", ...}}}` (sometimes inside
    markdown — mirror `_query_sun_state`'s tolerant parse)."""
    data = None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if m:
            try:
                data = json.loads(m.group(0))
            except (TypeError, ValueError):
                pass
    if not isinstance(data, dict):
        return None
    entity = data.get("data", data)
    if not isinstance(entity, dict):
        return None
    condition = entity.get("state")
    attrs = entity.get("attributes") or {}
    temp = attrs.get("temperature")
    if not condition or temp is None:
        return None
    unit = str(attrs.get("temperature_unit") or "°")
    return build_weather_msg(str(condition), temp, unit)


def _is_enabled(state) -> bool:
    rc = state.runtime_config
    if rc is None:
        return False
    if not bool(rc.get("weather_enabled", True)):
        return False
    return bool(str(rc.get("weather_entity", "") or "").strip())


async def _read_weather(state) -> dict | None:
    """Read the configured weather entity from HA → a `weather_update` message,
    or None (→ hide) when disabled, unconfigured, or unavailable. Never raises."""
    if not _is_enabled(state):
        return None
    entity_id = str(state.runtime_config.get("weather_entity", "") or "").strip()
    if not state.mcp_client or "ha_get_state" not in state.mcp_client.tool_names:
        return None
    try:
        result = await state.mcp_client.call_tool("ha_get_state", {"entity_id": entity_id})
    except Exception as e:
        log.debug(f"weather: ha_get_state({entity_id}) failed: {e}")
        return None
    return parse_weather_response(result)


async def refresh_now(state) -> None:
    """Re-read the weather and push it to every display IF it changed. Pushes a
    hide message when disabled/unconfigured. Cached on `state.current_weather`
    so a (re)connecting client gets it replayed (see routes_ws.ui_endpoint)."""
    from .main import broadcast_force_action

    msg = await _read_weather(state) or _hide_msg()
    if msg == getattr(state, "current_weather", None):
        return
    state.current_weather = msg
    try:
        await broadcast_force_action(state, msg)
    except Exception as e:
        log.debug(f"weather: broadcast failed: {e}")


async def weather_poller(state) -> None:
    """Background task: refresh the weather every POLL_MINUTES. Cheap and robust
    — it just keeps the display in sync with HA's current weather."""
    log.info(f"Weather poller started (every {POLL_MINUTES} min)")
    while True:
        try:
            await refresh_now(state)
            await asyncio.sleep(POLL_MINUTES * 60)
        except asyncio.CancelledError:
            log.info("Weather poller cancelled")
            return
        except Exception as e:
            log.error(f"Weather poller error: {e}")
            await asyncio.sleep(POLL_MINUTES * 60)
