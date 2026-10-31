"""HTTP routes + selection plumbing for custom orbs (see orbs.py).

All /api/orbs* routes need a valid pairing token whose scope grants `orbs`
(full only — watch tokens are refused). Media is served from /orbs/<id>/<file>:
LAN-open like /themes (the hub fetches it through its RPi proxy); the gateway
requires a token for it (?token=, since <video src> can't send headers).
"""
from __future__ import annotations

import logging
import mimetypes
import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .orbs import DEFAULT_ID, MAX_UPLOAD_BYTES, STATES, OrbError, OrbStore, orb_message

log = logging.getLogger("hal.orbs")
router = APIRouter()


def orb_store(state) -> OrbStore:
    store = getattr(state, "orbs", None)
    if store is None:
        store = OrbStore()
        state.orbs = store
    return store


async def apply_orb(state, orb_id: str, *, persist: bool = True) -> dict:
    """Make `orb_id` the global orb: persist, push to the hub + every display,
    and mirror it to the HA select. Unknown / media-less ids fall back to the
    theme's orb ("default")."""
    from .main import _push_to_rpi, broadcast_force_action
    msg = orb_message(orb_store(state), orb_id or DEFAULT_ID)
    state.current_orb = msg["id"]
    if persist and state.runtime_config is not None:
        state.runtime_config.set("orb", msg["id"])
    await _push_to_rpi(state, msg)
    await broadcast_force_action(state, msg)
    bridge = getattr(state, "mqtt_bridge", None)
    if bridge is not None:
        try:
            await bridge.publish_config("orb", msg["name"])
        except Exception as e:
            log.debug(f"orb publish failed: {e}")
    return msg


async def refresh_orb_options(state) -> None:
    """Re-advertise the HA select's options after an orb was added/renamed/
    deleted/got its first media."""
    bridge = getattr(state, "mqtt_bridge", None)
    if bridge is None:
        return
    bridge.orb_options = orb_store(state).names()
    try:
        await bridge.publish_discovery()
    except Exception as e:
        log.debug(f"orb discovery republish failed: {e}")


# --- admin import (PAL's LAN-only /mcp: create_orb & co.) -------------------------
# Media comes from an http(s) URL or a file dropped into the orb inbox
# (runtime/orbs_inbox/ on the host), so big videos never travel as MCP args.

def _inbox() -> str:
    return os.environ.get("ORBS_INBOX", "/app/runtime/orbs_inbox")


async def _read_source(src: str) -> tuple[bytes, str, str | None]:
    """(data, content_type, consumed inbox path or None) for a URL or inbox name."""
    src = (src or "").strip()
    if src.lower().startswith(("http://", "https://")):
        import httpx
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
            async with http.stream("GET", src) as r:
                if r.status_code != 200:
                    raise OrbError(f"download failed ({r.status_code}): {src}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_UPLOAD_BYTES:
                        raise OrbError("file too large (max 100 MB)", 413)
                ctype = r.headers.get("content-type", "").split(";")[0]
        return bytes(buf), ctype, None
    name = os.path.basename(src)
    path = os.path.join(_inbox(), name)
    if not name or name.startswith(".") or not os.path.isfile(path):
        raise OrbError(f"not a URL and not in the orb inbox: {src}", 404)
    if os.path.getsize(path) > MAX_UPLOAD_BYTES:
        raise OrbError("file too large (max 100 MB)", 413)
    with open(path, "rb") as f:
        data = f.read()
    return data, mimetypes.guess_type(name)[0] or "", path


async def import_orb(state, name: str, sources: dict[str, str], *, select: bool = False) -> dict:
    """Create the orb `name` (or update its media if it exists) from
    {state: url-or-inbox-file}. A new orb must bring `idle`; one whose idle
    import fails is removed again. Consumed inbox files are deleted."""
    store = orb_store(state)
    sources = {k: v for k, v in sources.items() if v}
    if not sources:
        raise OrbError("give at least the idle media")
    bad = set(sources) - set(STATES)
    if bad:
        raise OrbError(f"unknown state(s): {', '.join(sorted(bad))}")
    orb_id = store.id_for_name(name)
    if orb_id == DEFAULT_ID:
        raise OrbError('"default" is the theme\'s orb and can\'t be replaced')
    created = orb_id is None
    if created:
        if "idle" not in sources:
            raise OrbError("a new orb needs the idle media")
        orb_id = store.create(name)["id"]
    had_idle = bool((store.get(orb_id) or {}).get("media", {}).get("idle"))
    consumed = []
    try:
        for st in [s for s in STATES if s in sources]:
            data, ctype, path = await _read_source(sources[st])
            await store.set_media(orb_id, st, data, ctype)
            if path:
                consumed.append(path)
    except OrbError:
        if created and not (store.get(orb_id) or {}).get("media", {}).get("idle"):
            store.delete(orb_id)
        raise
    for path in consumed:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not had_idle:
        await refresh_orb_options(state)
    if select or getattr(state, "current_orb", None) == orb_id:
        await apply_orb(state, orb_id)
    return _public({"id": orb_id, **store.get(orb_id)})


def _err(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


def _authorize(request: Request):
    from .main import _get_state
    from .pairing import extract_bearer
    state = _get_state(request.app)
    token = extract_bearer(request)
    pairing = state.pairing
    if not (token and pairing and pairing.is_valid_token(token)):
        return _err(401, "unauthorized")
    if not pairing.scope_allows(token, "orbs"):
        return _err(403, "forbidden_scope")
    return state


async def _guard(request: Request, fn):
    state = _authorize(request)
    if isinstance(state, Response):
        return state
    try:
        return await fn(state)
    except OrbError as e:
        return _err(e.status, str(e))


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise OrbError("invalid JSON")
    if not isinstance(data, dict):
        raise OrbError("body must be an object")
    return data


def _public(orb: dict) -> dict:
    return {"id": orb["id"], "name": orb["name"],
            "states": sorted((orb.get("media") or {}).keys())}


@router.get("/api/orbs")
async def list_orbs(request: Request):
    async def run(state):
        return {"orbs": [_public(o) for o in orb_store(state).list()],
                "selected": getattr(state, "current_orb", DEFAULT_ID) or DEFAULT_ID}
    return await _guard(request, run)


@router.get("/api/orbs/{orb_id}/message")
async def orb_display_message(request: Request, orb_id: str):
    """The set_orb payload for any orb — a satellite that doesn't follow the
    hub uses this to show its own choice."""
    async def run(state):
        return orb_message(orb_store(state), orb_id)
    return await _guard(request, run)


@router.post("/api/orbs")
async def create_orb(request: Request):
    async def run(state):
        orb = orb_store(state).create((await _json(request)).get("name"))
        return _public(orb)
    return await _guard(request, run)


@router.patch("/api/orbs/{orb_id}")
async def rename_orb(request: Request, orb_id: str):
    async def run(state):
        orb = orb_store(state).rename(orb_id, (await _json(request)).get("name"))
        await refresh_orb_options(state)
        if state.current_orb == orb_id:
            await apply_orb(state, orb_id)
        return _public(orb)
    return await _guard(request, run)


@router.put("/api/orbs/{orb_id}/media/{orb_state}")
async def upload_orb_media(request: Request, orb_id: str, orb_state: str):
    async def run(state):
        length = int(request.headers.get("content-length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise OrbError("file too large (max 100 MB)", 413)
        body = await request.body()
        store = orb_store(state)
        had_idle = bool((store.get(orb_id) or {}).get("media", {}).get("idle"))
        orb = await store.set_media(orb_id, orb_state, body,
                                    request.headers.get("content-type", ""))
        if not had_idle:
            await refresh_orb_options(state)   # first media makes it selectable
        if state.current_orb == orb_id:
            await apply_orb(state, orb_id)     # live update of what's showing
        return _public(orb)
    return await _guard(request, run)


@router.delete("/api/orbs/{orb_id}/media/{orb_state}")
async def delete_orb_media(request: Request, orb_id: str, orb_state: str):
    async def run(state):
        orb = orb_store(state).remove_media(orb_id, orb_state)
        if state.current_orb == orb_id:
            await apply_orb(state, orb_id)
        return _public(orb)
    return await _guard(request, run)


@router.delete("/api/orbs/{orb_id}")
async def delete_orb(request: Request, orb_id: str):
    async def run(state):
        if not orb_store(state).delete(orb_id):
            raise OrbError("no such orb", 404)
        await refresh_orb_options(state)
        if state.current_orb == orb_id:
            await apply_orb(state, DEFAULT_ID)
        return {"ok": True}
    return await _guard(request, run)


@router.post("/api/orbs/select")
async def select_orb(request: Request):
    async def run(state):
        orb_id = str((await _json(request)).get("id") or DEFAULT_ID)
        if orb_id != DEFAULT_ID and orb_store(state).get(orb_id) is None:
            raise OrbError("no such orb", 404)
        msg = await apply_orb(state, orb_id)
        return {"selected": msg["id"]}
    return await _guard(request, run)


@router.get("/orbs/{orb_id}/{filename}")
async def get_orb_media(request: Request, orb_id: str, filename: str):
    from .main import _get_state
    path = orb_store(_get_state(request.app)).media_path(orb_id, filename)
    if not path:
        return Response(status_code=404)
    media_type = "video/mp4" if filename.endswith(".mp4") else "image/jpeg"
    # Immutable per ?v= (the message carries the update time).
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
