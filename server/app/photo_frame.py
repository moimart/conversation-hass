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
import os
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


@dataclass
class PhotoFrameVideoSession:
    """A looping-video photo-frame session. Unlike the photo session this
    has no HA entity / subscription — the kiosk plays a single local file
    (`src`) on repeat. `video_hash` is the sha256 of the cached video so we
    can skip redundant re-shows when nothing changed."""
    video_hash: str
    src: str
    started_at: float = field(default_factory=time.monotonic)


# Kiosk-local URL of the looping video. The RPi serves the file it pulled
# from the server at this path (see rpi/audio_streamer add_static("/media/")).
VIDEO_SRC = "/media/loop.mp4"


def _show_clock(state: "AppState") -> bool:
    """Whether the kiosk should keep the clock/date overlay visible during
    photo mode (user setting, default on). Carried in every show/update
    payload so the kiosk applies the right state the moment a frame opens —
    the value rides with the frame it affects rather than relying on a
    connect-time push the (separately-connecting) browser might miss."""
    return bool(getattr(state, "photo_frame_show_clock", True))


def _video_available(state: "AppState") -> Optional[str]:
    """Return the cached video's sha256 if video mode is ON and a video has
    been downloaded (hash known), else None. `photo_frame_video_hash` is set
    by the server only after a successful download and cleared when the cache
    is dropped, so a non-empty hash implies the cache file exists."""
    try:
        mode = bool(state.runtime_config.get("photo_frame_video_mode", False))
    except Exception:
        mode = False
    if not mode:
        return None
    h = (getattr(state, "photo_frame_video_hash", "") or "").strip()
    return h or None


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
    force_photo: bool = False,
) -> dict:
    """Open a photo frame session.

    `entity_id` overrides `runtime_config["photo_frame_entity"]` when
    non-empty. If both are empty, returns `not_configured`.

    When video mode is ON and a video is cached, this loops the video
    instead of cycling photos. `force_photo=True` bypasses the video
    branch (used by the load-error fallback to drop back to photos).
    """
    # Showing the photo frame counts as kiosk activity → wake the
    # display first if it's currently blanked.
    try:
        from .main import _record_kiosk_activity  # avoid top-level cycle
        _record_kiosk_activity(state)
    except Exception:
        pass

    # Video branch: if the user enabled video mode and we have a cached
    # video, loop it (no HA fetch / subscription).
    if not force_photo:
        video_hash = _video_available(state)
        if video_hash is not None:
            result = await _start_video_frame(state, video_hash)
            if result is not None:
                return result
            # _start_video_frame returned None → couldn't reach the kiosk;
            # fall through to the photo path below.

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
            update_msg = {
                "type": "photo_frame_update", "image": b64, "mime": mime,
                "entity_id": entity, "show_clock": _show_clock(state),
            }
            await _push_to_rpi(state, update_msg)
            from .main import broadcast_to_ui
            await broadcast_to_ui(state, update_msg)
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

    msg = {"type": "show_photo_frame", "image": b64, "mime": mime,
           "entity_id": entity, "show_clock": _show_clock(state)}
    await _push_to_rpi(state, msg)
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)
    log.info(f"Photo frame opened: entity={entity} sub_id={sub_id}")
    return {"status": "ok", "session": True}


async def _start_video_frame(state: "AppState", video_hash: str) -> Optional[dict]:
    """Open (or refresh) a looping-video session.

    Returns a status dict on success, or None if the kiosk is unreachable
    (so the caller can fall back to the photo path).
    """
    existing: Optional[PhotoFrameVideoSession] = getattr(
        state, "photo_frame_video_session", None)
    already = existing is not None and existing.video_hash == video_hash

    # A photo session must never coexist with a video session.
    if state.photo_frame_session is not None:
        await stop_photo_frame(state, reason="video_switch")

    # Always (re)push, even when a session for the same hash is already
    # open: an explicit Show must re-assert the display, and the kiosk may
    # have reconnected and lost its state since the session opened. The
    # sync is cheap (the RPi skips the download on a hash match) and the
    # kiosk just re-plays the loop it already has. The sync message must
    # reach the RPi or it can't serve /media/loop.mp4 — if the audio WS is
    # gone, bail to the photo path.
    synced = await _push_to_rpi(state, {
        "type": "photo_frame_video_sync",
        "hash": video_hash,
        "url": "/api/photo_frame/video",
    })
    if not synced:
        return None

    show = {"type": "show_photo_frame_video", "src": VIDEO_SRC,
            "hash": video_hash, "show_clock": _show_clock(state)}
    await _push_to_rpi(state, show)
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, show)

    state.photo_frame_video_session = PhotoFrameVideoSession(
        video_hash=video_hash, src=VIDEO_SRC)
    log.info(f"Photo frame video {'re-shown' if already else 'opened'}: "
             f"hash={video_hash[:12]}")
    return {"status": "already_active" if already else "ok",
            "session": True, "mode": "video"}


async def handle_video_load_error(state: "AppState") -> dict:
    """The kiosk couldn't load/autoplay the looping video. Tear the video
    session down and fall back to cycling photos."""
    log.warning("Photo frame video failed on the kiosk — falling back to photos")
    await stop_photo_frame(state, reason="video_load_error")
    return await start_photo_frame(state, "", force_photo=True)


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
    msg = {"type": "photo_frame_update", "image": b64, "mime": mime,
           "entity_id": entity_id, "show_clock": _show_clock(state)}
    await _push_to_rpi(state, msg)
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)
    log.debug(f"Photo frame: pushed update for {entity_id}")


async def stop_photo_frame(state: "AppState", reason: str = "explicit") -> dict:
    """Close the active photo frame session (photo OR video). Idempotent —
    no-op when nothing is open."""
    session: Optional[PhotoFrameSession] = state.photo_frame_session
    video_session = getattr(state, "photo_frame_video_session", None)
    if session is None and video_session is None:
        return {"status": "not_active", "session": False}

    # Detach BOTH sessions BEFORE awaiting anything so a re-entrant start
    # sees a clean slate. A video session has no HA subscription, so the
    # only teardown it needs is the kiosk hide message below.
    state.photo_frame_session = None
    state.photo_frame_video_session = None

    # Tear down the HA subscription if we had a photo session with one.
    if session is not None and session.sub_id is not None:
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


# --- Photo-frame looping video: download + cache on the server -------------
# The user sets a source URL in HA; the server downloads it once, caches it
# under the writable /app/runtime mount, and serves it at
# /api/photo_frame/video. The RPi pulls that file over HTTP and plays it
# locally on the kiosk. A .sha256 sidecar lets a restart recover the hash
# without re-hashing the (large) file.
PHOTO_FRAME_VIDEO_MAX_BYTES = int(
    os.environ.get("PHOTO_FRAME_VIDEO_MAX_BYTES", str(150 * 1024 * 1024))
)
_VIDEO_FETCH_TIMEOUT = 60.0


def _photo_frame_video_cache_path() -> str:
    return os.environ.get(
        "PHOTO_FRAME_VIDEO_CACHE", "/app/runtime/photo_frame_video.mp4"
    )


def _photo_frame_video_sidecar_path() -> str:
    return _photo_frame_video_cache_path() + ".sha256"


def _load_photo_frame_video_hash(state: AppState) -> str:
    """Recover the cached video's hash from the sidecar at startup. Returns
    "" (and leaves state.photo_frame_video_hash empty) if no usable cache."""
    cache = _photo_frame_video_cache_path()
    sidecar = _photo_frame_video_sidecar_path()
    if not (os.path.isfile(cache) and os.path.isfile(sidecar)):
        state.photo_frame_video_hash = ""
        return ""
    try:
        with open(sidecar) as f:
            h = f.read().strip()
    except Exception:
        h = ""
    state.photo_frame_video_hash = h
    return h


def _clear_photo_frame_video_cache(state: AppState) -> None:
    """Drop the cached video + sidecar and forget its hash (best-effort)."""
    state.photo_frame_video_hash = ""
    for p in (_photo_frame_video_cache_path(), _photo_frame_video_sidecar_path()):
        try:
            os.unlink(p)
        except OSError:
            pass


async def _download_photo_frame_video(state: AppState, url: str) -> bool:
    """Stream-download a video URL to the cache (atomic) and set the hash.

    Validates a video/* content-type, enforces PHOTO_FRAME_VIDEO_MAX_BYTES
    incrementally while streaming, and writes to a temp file then os.replace
    so a partial/aborted download never leaves a truncated loop.mp4. On any
    failure the previous cache is left intact and False is returned.
    """
    import hashlib
    import tempfile
    import httpx

    url = (url or "").strip()
    if not url:
        return False
    headers: dict[str, str] = {}
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if ha_url and ha_token and url.startswith(ha_url):
        headers["Authorization"] = f"Bearer {ha_token}"

    cache = _photo_frame_video_cache_path()
    d = os.path.dirname(cache) or "."
    os.makedirs(d, exist_ok=True)
    tmp = None
    sha = hashlib.sha256()
    total = 0
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_VIDEO_FETCH_TIMEOUT, max_redirects=3
        ) as client:
            async with client.stream("GET", url, headers=headers) as r:
                r.raise_for_status()
                mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not mime.startswith("video/"):
                    log.warning(f"photo_frame video: refusing non-video content-type {mime!r} from {url}")
                    return False
                fd, tmp = tempfile.mkstemp(prefix=".photo_frame_video.", dir=d)
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        total += len(chunk)
                        if total > PHOTO_FRAME_VIDEO_MAX_BYTES:
                            log.warning(
                                f"photo_frame video at {url} exceeds cap "
                                f"{PHOTO_FRAME_VIDEO_MAX_BYTES} bytes — aborting"
                            )
                            raise ValueError("video too large")
                        sha.update(chunk)
                        f.write(chunk)
        digest = sha.hexdigest()
        os.replace(tmp, cache)
        tmp = None
        with open(_photo_frame_video_sidecar_path(), "w") as f:
            f.write(digest + "\n")
        state.photo_frame_video_hash = digest
        log.info(f"photo_frame video cached: {total} bytes, sha={digest[:12]}")
        return True
    except Exception as e:
        log.warning(f"photo_frame video download failed for {url}: {e}")
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
