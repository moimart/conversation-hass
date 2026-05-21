"""Photo frame session management.

A "photo frame" session pins a Home Assistant `image.*` entity to the
kiosk: the server pushes the current image to the kiosk (where it
fades in and gets a slow Ken-Burns), opens an HA WebSocket subscription
for the entity's `state_changed` events, and on each rotation re-fetches
the image and crossfades the kiosk to the new content. The session
ends when:

  * The LLM / MQTT / HTTP fire `stop_photo_frame`.
  * The kiosk reports a user-initiated dismissal back over
    `audio_websocket` (state change, volume tap, pointer, etc.).
  * A safety timer fires (the audio_websocket has been gone too long).

The whole feature is gated by `runtime_config["photo_frame_entity"]`.
If that's empty and the caller doesn't supply an explicit entity_id,
the start function returns a `not_configured` status with no log
noise — the LLM tool surfaces it as a short string, the MQTT button
no-ops, the HTTP route returns the same status. This is deliberate:
the feature should be silently invisible until someone configures it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal.photo_frame")

# Server keeps the HA subscription open this long after the kiosk goes
# silent (no `photo_frame_dismissed` ack, no further state messages)
# before tearing it down on its own.
SAFETY_TIMEOUT_S = 30.0


@dataclass
class PhotoFrameSession:
    entity_id: str
    sub_id: Optional[int]              # HA WS subscription id; None if HA wasn't reachable
    last_hash: str                     # sha256 of the last image bytes we pushed
    started_at: float = field(default_factory=time.monotonic)


async def _push_to_rpi(state: "AppState", msg: dict) -> bool:
    """Local mirror of main._push_to_rpi to dodge a circular import."""
    ws = state.audio_websocket
    if not ws:
        return False
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        log.warning(f"Could not send {msg.get('type')} to RPi: {e}")
        return False


async def _fetch_and_b64(entity_id: str) -> Optional[tuple[str, str, str]]:
    """Fetch an HA image.* entity and return (b64, mime, sha256).

    Returns None if the fetch fails or HA creds aren't configured.
    Wraps the existing `_fetch_ha_image_entity` helper in main.py which
    already handles caps + bearer-token attachment.
    """
    from .main import _fetch_ha_image_entity
    result = await _fetch_ha_image_entity(entity_id)
    if result is None:
        return None
    raw, mime = result
    b64 = base64.b64encode(raw).decode("ascii")
    sha = hashlib.sha256(raw).hexdigest()
    return b64, mime, sha


async def start_photo_frame(
    state: "AppState",
    entity_id: str = "",
) -> dict:
    """Open a photo frame session.

    `entity_id` overrides `runtime_config["photo_frame_entity"]` when
    non-empty. If both are empty, returns `not_configured`.
    """
    # Showing the photo frame counts as kiosk activity → wake the
    # display first if it's currently blanked.
    try:
        from .main import _record_kiosk_activity  # avoid top-level cycle
        _record_kiosk_activity(state)
    except Exception:
        pass
    # Resolve entity: explicit arg wins, otherwise the live runtime config.
    entity = (entity_id or "").strip()
    if not entity:
        entity = str(state.runtime_config.get("photo_frame_entity", "") or "").strip()
    if not entity:
        log.debug("Photo frame: no entity configured, ignoring")
        return {"status": "not_configured", "session": False}
    if not (entity.startswith("image.") or entity.startswith("camera.")):
        log.warning(f"Photo frame: rejecting non-image entity {entity!r}")
        return {"status": "invalid_entity", "session": False}

    # Fetch first image. If this fails (HA unreachable / 404 / non-image
    # MIME), don't bother opening a subscription.
    fetched = await _fetch_and_b64(entity)
    if fetched is None:
        return {"status": "fetch_failed", "session": False}
    b64, mime, sha = fetched

    # Idempotent: if we're already showing the photo frame for the same
    # entity, just push the freshly-fetched image as an update so the
    # kiosk crossfades to it (the entity may have rotated since the
    # previous session started).
    existing: Optional[PhotoFrameSession] = state.photo_frame_session
    if existing is not None:
        if existing.entity_id == entity:
            existing.last_hash = sha
            await _push_to_rpi(state, {
                "type": "photo_frame_update", "image": b64, "mime": mime, "entity_id": entity,
            })
            from .main import broadcast_to_ui
            await broadcast_to_ui(state, {
                "type": "photo_frame_update", "image": b64, "mime": mime, "entity_id": entity,
            })
            return {"status": "already_active", "session": True}
        # Different entity → close the old session before opening the new one.
        log.info(f"Photo frame: switching entity {existing.entity_id!r} → {entity!r}")
        await stop_photo_frame(state, reason="entity_switch")

    # Open a state_changed subscription for the entity. HA WS doesn't
    # support per-entity filtering at the subscribe layer, so the
    # handler filters on event.data.entity_id.
    sub_id: Optional[int] = None
    ha_client = await _ensure_ha_ws(state)
    if ha_client is not None:
        async def _on_event(event: dict) -> None:
            # Subscription confirmation comes back as a `result` —
            # ignore. Real events have a `data.entity_id`.
            data = (event.get("data") or {}) if isinstance(event, dict) else {}
            if data.get("entity_id") != entity:
                return
            await _on_state_changed(state, entity)
        try:
            sub_id = await ha_client.subscribe(
                {"type": "subscribe_events", "event_type": "state_changed"},
                _on_event,
            )
        except Exception as e:
            log.warning(f"Photo frame: HA subscribe failed (degraded mode): {e}")
            sub_id = None

    state.photo_frame_session = PhotoFrameSession(
        entity_id=entity, sub_id=sub_id, last_hash=sha,
    )

    msg = {"type": "show_photo_frame", "image": b64, "mime": mime, "entity_id": entity}
    await _push_to_rpi(state, msg)
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)
    log.info(f"Photo frame opened: entity={entity} sub_id={sub_id}")
    return {"status": "ok", "session": True}


async def _ensure_ha_ws(state: "AppState"):
    """Late-bound HA WS access. Returns the connected client or None."""
    from .main import _ensure_ha_ws as bootstrap
    try:
        return await bootstrap(state)
    except Exception as e:
        log.warning(f"Photo frame: HA WS unavailable: {e}")
        return None


async def _on_state_changed(state: "AppState", entity_id: str) -> None:
    """HA notified that our entity state changed — re-fetch and push if
    the bytes actually changed."""
    session: Optional[PhotoFrameSession] = state.photo_frame_session
    if session is None or session.entity_id != entity_id:
        return  # session ended between event and handler
    fetched = await _fetch_and_b64(entity_id)
    if fetched is None:
        log.debug(f"Photo frame update: fetch failed for {entity_id}")
        return
    b64, mime, sha = fetched
    if sha == session.last_hash:
        # State changed but bytes are identical (e.g. attribute-only update).
        return
    session.last_hash = sha
    msg = {"type": "photo_frame_update", "image": b64, "mime": mime, "entity_id": entity_id}
    await _push_to_rpi(state, msg)
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)
    log.debug(f"Photo frame: pushed update for {entity_id}")


async def stop_photo_frame(state: "AppState", reason: str = "explicit") -> dict:
    """Close the active photo frame session. Idempotent — no-op when
    nothing is open."""
    session: Optional[PhotoFrameSession] = state.photo_frame_session
    if session is None:
        return {"status": "not_active", "session": False}

    # Detach the session BEFORE awaiting anything so a re-entrant
    # start sees a clean slate.
    state.photo_frame_session = None

    # Tear down the HA subscription if we had one.
    if session.sub_id is not None:
        from .main import _ensure_ha_ws as _ha
        try:
            client = await _ha(state)
            if client is not None:
                await client.unsubscribe(session.sub_id)
        except Exception as e:
            log.warning(f"Photo frame: HA unsubscribe failed (ignoring): {e}")

    # Tell the kiosk to fade out (and any direct UI clients).
    await _push_to_rpi(state, {"type": "hide_photo_frame", "reason": reason})
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, {"type": "hide_photo_frame", "reason": reason})
    log.info(f"Photo frame closed (reason={reason})")
    return {"status": "ok", "session": False}
