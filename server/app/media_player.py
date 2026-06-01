"""Sendspin / Music-Assistant media-player control.

Thin wrappers that forward hardware button presses (volume, play/pause) to the
configured Sendspin media_player entity via the HA MCP client, serialized
through state._ma_lock. Extracted from main.py; callers reach these via the
re-export in main (and the lazy `from .main import` pattern elsewhere)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal")


async def _ma_call(state: AppState, service: str, extra: dict | None = None) -> None:
    """Invoke a media_player service on the configured Sendspin player.

    Serialized via state._ma_lock so concurrent button presses arrive
    at HA in the order they were issued, with a 5s timeout per call so
    a hung MCP tool can't pile up the queue indefinitely.
    """
    entity = state.sendspin_player_entity
    if not entity or not state.mcp_client:
        return
    args = {
        "domain": "media_player",
        "service": service,
        "target": {"entity_id": entity},
    }
    if extra:
        args["service_data"] = extra
    async with state._ma_lock:
        try:
            await asyncio.wait_for(
                state.mcp_client.call_tool("ha_call_service", args),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.warning(f"ha_call_service {service} timed out after 5s")
        except Exception as e:
            log.warning(f"ha_call_service {service} failed: {e}")


async def _ma_volume_step(state: AppState, step: float) -> None:
    """Forward a hardware vol button press to the Sendspin player in MA."""
    if not state.sendspin_player_entity:
        return
    service = "volume_up" if step > 0 else "volume_down"
    await _ma_call(state, service)


async def _ma_query_state(state: AppState) -> str | None:
    """Return the Sendspin player's current state ('playing'/'paused'/...).

    Returns None on any error or if not configured.
    """
    entity = state.sendspin_player_entity
    if not entity or not state.mcp_client:
        return None
    try:
        result = await asyncio.wait_for(
            state.mcp_client.call_tool("ha_get_state", {"entity_id": entity}),
            timeout=3.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        log.warning(f"ha_get_state for sendspin player failed: {e}")
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    # HA MCP wraps the entity in {"data": {...}, "metadata": {...}}
    entity_data = data.get("data", data) if isinstance(data, dict) else {}
    if not isinstance(entity_data, dict):
        return None
    return entity_data.get("state")


async def _ma_pause_if_playing(state: AppState) -> None:
    """Pause MA only if currently playing; remember so we can resume later."""
    ma_state = await _ma_query_state(state)
    if ma_state == "playing":
        state.sendspin_was_playing = True
        await _ma_call(state, "media_pause")
    else:
        # User paused/stopped manually — don't touch it on resume.
        state.sendspin_was_playing = False


async def _ma_resume_if_we_paused(state: AppState) -> None:
    """Resume MA only if we were the ones who paused it."""
    if state.sendspin_was_playing:
        state.sendspin_was_playing = False
        await _ma_call(state, "media_play")
