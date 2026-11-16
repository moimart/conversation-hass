"""Routes for native satellites (PAL Round, firmware/pal-round). See
device_api.py for what each one pre-digests. All need a paired token whose
scope grants `device` (full tokens; watch → 403).

    GET /api/device/info                 colours, time zone, orb, firmware
    GET /api/device/orb?orb=&size=&fps=  orb manifest (renders .palm clips)
    GET /api/device/orb/{key}.palm       a rendered clip
    GET /api/device/pages?dashboard=     an HA dashboard as device pages
    GET /api/device/firmware/{model}.bin OTA image
    POST /api/satellite/ble_pair         a phone asks online PAL Rounds to open
                                         Bluetooth pairing (see BLE_LINK.md)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import device_api

log = logging.getLogger("hal.device")
router = APIRouter()

_KEY_RE = re.compile(r"^[0-9a-f]{20}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_MAX_CACHE_FILES = 120
_render_locks: dict[str, asyncio.Lock] = {}


def _cache_dir() -> str:
    d = os.environ.get("DEVICE_ORB_CACHE", "/app/runtime/device_orbs")
    os.makedirs(d, exist_ok=True)
    return d


def _firmware_dir() -> str:
    return os.environ.get("DEVICE_FIRMWARE_DIR", "/app/runtime/firmware")


def _auth(request: Request):
    from .routes_ha import _authorize
    return _authorize(request, "device")


def firmware_info(model: str) -> dict | None:
    """{version, size, sha256} of runtime/firmware/<model>/firmware.bin (with
    its `version` file), or None."""
    if not _MODEL_RE.match(model or ""):
        return None
    d = os.path.join(_firmware_dir(), model)
    binp, verp = os.path.join(d, "firmware.bin"), os.path.join(d, "version")
    if not (os.path.isfile(binp) and os.path.isfile(verp)):
        return None
    with open(verp) as f:
        version = f.read().strip()[:32]
    h = hashlib.sha256()
    with open(binp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"version": version, "size": os.path.getsize(binp), "sha256": h.hexdigest(),
            "url": f"/api/device/firmware/{model}.bin"}


def _theme(state):
    reg = getattr(state, "themes", None)
    return reg.get(state.current_theme) if reg is not None and hasattr(reg, "get") else None


@router.get("/api/device/info")
async def device_info(request: Request):
    auth = _auth(request)
    if isinstance(auth, Response):
        return auth
    state, token = auth
    theme = _theme(state)
    colors = {}
    if theme is not None:
        try:
            with open(os.path.join(theme.dir_path, "theme.css")) as f:
                colors = device_api.theme_colors(f.read())
        except OSError:
            pass
    tz = ""
    bridge = getattr(state, "ha_bridge", None)
    if bridge is not None:
        try:
            boot = await asyncio.wait_for(bridge.bootstrap(), 5)
            tz = (boot.get("config") or {}).get("time_zone") or ""
        except Exception:
            pass
    tz = tz or os.environ.get("TZ", "") or "Europe/Berlin"
    model = request.query_params.get("model") or "pal-round"
    return {
        "server_name": os.environ.get("HAL_DEVICE_NAME", "PAL"),
        "device_name": state.pairing.device_name(token) if state.pairing else None,
        "tz": tz,
        "tz_posix": device_api.posix_tz(tz),
        "theme": {"name": getattr(state, "current_theme", ""),
                  "kind": getattr(theme, "kind", "dark"), "colors": colors},
        "orb": getattr(state, "current_orb", "default"),
        "firmware": firmware_info(model),
    }


async def _render_cached(src: str, kind: str, size: int, fps: int) -> str:
    key = device_api.source_key(src, size, fps, 10.0)
    path = os.path.join(_cache_dir(), f"{key}.palm")
    if os.path.isfile(path):
        return key
    lock = _render_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not os.path.isfile(path):
            blob = await device_api.render_palm(src, kind, size, fps)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
            _prune_cache()
    _render_locks.pop(key, None)
    return key


def _prune_cache() -> None:
    d = _cache_dir()
    files = sorted((os.path.join(d, n) for n in os.listdir(d) if n.endswith(".palm")),
                   key=os.path.getmtime)
    for p in files[:-_MAX_CACHE_FILES]:
        try:
            os.unlink(p)
        except OSError:
            pass


@router.get("/api/device/orb")
async def device_orb(request: Request):
    """{id, name, states: {state: {key, url}}} for the orb this device shows
    (?orb=<id> = its own pick; absent/"follow" = the household selection).
    Empty `states` → no media: the device draws its native orb."""
    auth = _auth(request)
    if isinstance(auth, Response):
        return auth
    state, _token = auth
    q = request.query_params
    size, fps = device_api.orb_params(q.get("size"), q.get("fps"))
    orb_id = q.get("orb") or "follow"
    if orb_id == "follow":
        orb_id = getattr(state, "current_orb", "default") or "default"
    store = getattr(state, "orbs", None)
    orb = None
    if orb_id != "default" and store is not None:
        o = store.get(orb_id)
        orb = {"id": orb_id, **o} if o else None
    root = getattr(store, "root", "") if store is not None else ""
    sources = device_api.resolve_orb_sources(_theme(state), orb, root)
    out = {"id": orb_id if orb else "default",
           "name": (orb or {}).get("name") or "default",
           "theme": getattr(state, "current_theme", ""), "size": size, "fps": fps, "states": {}}
    keys: dict[str, str] = {}
    try:
        for st, (src, kind) in sources.items():
            if src not in keys:
                keys[src] = await _render_cached(src, kind, size, fps)
            out["states"][st] = {"key": keys[src], "url": f"/api/device/orb/{keys[src]}.palm"}
    except Exception as e:
        log.warning(f"device orb render failed: {type(e).__name__}: {e}")
        return JSONResponse({"error": "orb render failed"}, status_code=502)
    return out


@router.get("/api/device/orb/{key}.palm")
async def device_orb_clip(request: Request, key: str):
    auth = _auth(request)
    if isinstance(auth, Response):
        return auth
    if not _KEY_RE.match(key):
        return Response(status_code=404)
    path = os.path.join(_cache_dir(), f"{key}.palm")
    if not os.path.isfile(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "max-age=31536000, immutable"})


@router.get("/api/device/pages")
async def device_pages(request: Request):
    """The chosen HA dashboard as device pages (see device_api.lovelace_to_pages)."""
    auth = _auth(request)
    if isinstance(auth, Response):
        return auth
    state, _token = auth
    from .ha_bridge import HAError
    bridge = getattr(state, "ha_bridge", None)
    if bridge is None:
        return JSONResponse({"error": "Home Assistant is not configured"}, status_code=503)
    dashboard = request.query_params.get("dashboard") or None
    try:
        cfg = await bridge.send({"type": "lovelace/config", "url_path": dashboard})
        boot = await bridge.bootstrap()
    except HAError as e:
        return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 502))
    reg_icons = {e.get("ei"): e.get("ic") for e in (boot.get("registry") or {}).get("entities", [])
                 if isinstance(e, dict) and e.get("ic")}
    states = getattr(bridge, "states", {}) or {}
    pages = device_api.lovelace_to_pages(cfg if isinstance(cfg, dict) else {}, states, reg_icons)
    return {"dashboard": dashboard or "", "pages": pages}


@router.get("/api/device/firmware/{model}.bin")
async def device_firmware(request: Request, model: str):
    auth = _auth(request)
    if isinstance(auth, Response):
        return auth
    if not firmware_info(model):
        return Response(status_code=404)
    return FileResponse(os.path.join(_firmware_dir(), model, "firmware.bin"),
                        media_type="application/octet-stream")


ROUND_NAME_PREFIX = "PAL Round"


@router.post("/api/satellite/ble_pair")
async def ble_pair(request: Request):
    """A paired phone (the Android app) asks every online PAL Round to open
    Bluetooth pairing for 2 minutes, so the phone can bond and relay the
    Round's traffic when it has no Wi-Fi. The Round then shows a passkey the
    user types into Android's pairing dialog. -> {"ok", "devices": N}."""
    from .main import _get_state
    from .pairing import extract_bearer
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not (token and state.pairing and state.pairing.is_valid_token(token)):
        return Response(status_code=401)
    if not state.pairing.scope_allows(token, "ws_ui"):
        return Response(status_code=403)
    requester = state.pairing.device_name(token) or "a phone"
    sent = 0
    for tok, ws in list(state.satellite_ws.items()):
        name = state.pairing.device_name(tok) or ""
        if tok == token or not name.startswith(ROUND_NAME_PREFIX):
            continue
        try:
            await ws.send_json({"type": "ble_pair", "from": requester})
            sent += 1
        except Exception:
            pass
    log.info(f"ble_pair requested by {requester}: {sent} PAL Round(s)")
    return {"ok": True, "devices": sent}
