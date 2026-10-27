"""HTTP routes for satellite HA cards + per-device layouts.

Every /api/ha/* and /api/satellite/layout route needs a valid pairing token
whose scope grants the permission (`ha` / `layout` — both `full`-only, so a
watch token is refused). Live entity state does NOT flow here: it rides the
device's /ws/ui socket (ha_retain / ha_subscribe in routes_ws.py).

/local/* and /hacsfiles/* are proxied from HA WITHOUT a PAL token: custom-card
scripts are loaded by <script type=module> tags and import their siblings by
absolute /hacsfiles/... URLs, none of which can carry a header. HA itself serves
these paths unauthenticated, so this exposes nothing HA doesn't.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .ha_bridge import HAError

log = logging.getLogger("hal.routes_ha")
router = APIRouter()

_PROXY_RE = re.compile(
    r"^/api/(camera_proxy|media_player_proxy|image_proxy)/[\w.-]+(\?[\w=&%.-]*)?$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[\w-]+$")
_MAX_BODY = 64 * 1024


def _ha_env() -> tuple[str, str]:
    return (os.environ.get("HA_URL", "").strip().rstrip("/"),
            os.environ.get("HA_TOKEN", "").strip())


def _err(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


def _authorize(request: Request, permission: str, *, allow_query: bool = False):
    """(state, token) for a valid token whose scope grants `permission`, else
    an error Response."""
    from .main import _get_state
    from .pairing import extract_bearer
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not token and allow_query:
        token = request.query_params.get("token")
    pairing = state.pairing
    if not (token and pairing and pairing.is_valid_token(token)):
        return _err(401, "unauthorized")
    if not pairing.scope_allows(token, permission):
        return _err(403, "forbidden_scope")
    return state, token


def _bridge(state):
    bridge = getattr(state, "ha_bridge", None)
    if bridge is None:
        raise HAError("Home Assistant is not configured", 503)
    return bridge


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HAError("body too large", 413)
    try:
        import json
        data = json.loads(body or b"{}")
    except ValueError:
        raise HAError("invalid JSON")
    if not isinstance(data, dict):
        raise HAError("body must be an object")
    return data


async def _guard(request: Request, permission: str, fn, *, allow_query=False):
    auth = _authorize(request, permission, allow_query=allow_query)
    if isinstance(auth, Response):
        return auth
    state, token = auth
    try:
        return await fn(state, token)
    except HAError as e:
        return _err(e.status, str(e))
    except Exception as e:
        log.warning(f"{request.url.path} failed: {type(e).__name__}: {e}")
        return _err(502, "Home Assistant request failed")


async def _ha_rest(method: str, path: str, json_body=None):
    import httpx
    url, token = _ha_env()
    if not url or not token:
        raise HAError("Home Assistant is not configured", 503)
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.request(method, url + path, json=json_body,
                                 headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise HAError("Home Assistant rejected the token", 502)
    if r.status_code >= 400:
        raise HAError(f"Home Assistant returned {r.status_code}", 502)
    return r


# --- HA card API --------------------------------------------------------------

@router.get("/api/ha/status")
async def ha_status(request: Request):
    async def run(state, _token):
        b = getattr(state, "ha_bridge", None)
        return {"configured": b is not None, "connected": bool(b and b.connected),
                "entities": len(b.states) if b else 0}
    return await _guard(request, "ha", run)


@router.get("/api/ha/bootstrap")
async def ha_bootstrap(request: Request):
    async def run(state, _token):
        return await _bridge(state).bootstrap()
    return await _guard(request, "ha", run)


@router.post("/api/ha/ws")
async def ha_ws(request: Request):
    async def run(state, token):
        data = await _json_body(request)
        return {"result": await _bridge(state).ws(data.get("msg"), token=token)}
    return await _guard(request, "ha", run)


@router.post("/api/ha/service")
async def ha_service(request: Request):
    async def run(state, token):
        d = await _json_body(request)
        await _bridge(state).call_service(d.get("domain"), d.get("service"),
                                          d.get("data"), d.get("target"), token=token)
        return {"ok": True}
    return await _guard(request, "ha", run)


@router.get("/api/ha/entities")
async def ha_entities(request: Request):
    async def run(state, _token):
        b = _bridge(state)
        q = request.query_params
        return {"entities": b.entities(q.get("q", ""), q.get("domain", "")),
                "connected": b.connected}
    return await _guard(request, "ha", run)


@router.get("/api/ha/history")
async def ha_history(request: Request):
    async def run(_state, _token):
        q = request.query_params
        ids = [x for x in (q.get("e") or "").split(",") if _ENTITY_RE.match(x)][:10]
        if not ids:
            raise HAError("e= entity ids required")
        try:
            hours = max(1, min(168, int(q.get("hours") or 24)))
        except ValueError:
            hours = 24
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        r = await _ha_rest("GET", f"/api/history/period/{start}?filter_entity_id="
                                  f"{','.join(ids)}&minimal_response&no_attributes")
        return {"history": r.json()}
    return await _guard(request, "ha", run)


@router.post("/api/ha/template")
async def ha_template(request: Request):
    async def run(_state, _token):
        d = await _json_body(request)
        r = await _ha_rest("POST", "/api/template", {"template": str(d.get("template") or "")})
        return {"text": r.text}
    return await _guard(request, "ha", run)


@router.get("/api/ha/forecast")
async def ha_forecast(request: Request):
    async def run(state, _token):
        q = request.query_params
        return {"forecast": await _bridge(state).forecast(q.get("e", ""), q.get("type", "daily"))}
    return await _guard(request, "ha", run)


@router.get("/api/ha/dashboards")
async def ha_dashboards(request: Request):
    async def run(state, _token):
        return {"dashboards": await _bridge(state).dashboards()}
    return await _guard(request, "ha", run)


@router.get("/api/ha/cards")
async def ha_cards(request: Request):
    async def run(state, _token):
        return {"cards": await _bridge(state).cards(request.query_params.get("dashboard") or None)}
    return await _guard(request, "ha", run)


@router.get("/api/ha/proxy")
async def ha_proxy(request: Request):
    """Images HA serves behind auth (camera snapshots, album art). Accepts
    ?token= because <img> can't send headers."""
    async def run(_state, _token):
        target = request.query_params.get("path") or ""
        if not _PROXY_RE.match(target):
            raise HAError("not allowed")
        r = await _ha_rest("GET", target)
        return Response(content=r.content,
                        media_type=r.headers.get("content-type", "application/octet-stream"),
                        headers={"Cache-Control": "no-store"})
    return await _guard(request, "ha", run, allow_query=True)


# --- HA static files (custom cards) -----------------------------------------

def safe_static_path(raw_path: str) -> str | None:
    """The decoded path if it is safe to forward, else None. Decoding first is
    what stops %2e%2e (and double-encoded) traversal reaching HA."""
    p = raw_path
    for _ in range(3):  # repeated decoding defeats double/triple encoding
        dec = unquote(p)
        if dec == p:
            break
        p = dec
    if "\\" in p or "\x00" in p:
        return None
    if any(seg in ("..", ".") for seg in p.split("/")):
        return None
    return p


async def _static(request: Request, prefix: str, path: str):
    import httpx
    clean = safe_static_path(path)
    if clean is None:
        return Response(status_code=400, content="bad path")
    url, token = _ha_env()
    if not url or not token:
        return Response(status_code=503)
    target = f"{url}/{prefix}/{clean}"
    if request.url.query:
        target += f"?{request.url.query}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
    try:
        resp = await client.send(
            client.build_request("GET", target, headers={"Authorization": f"Bearer {token}"}),
            stream=True)
    except Exception as e:
        await client.aclose()
        log.debug(f"static proxy {prefix}/{clean} failed: {e}")
        return Response(status_code=502)
    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        return Response(status_code=404 if status == 404 else 502)

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(body(), media_type=resp.headers.get("content-type",
                                                                   "application/octet-stream"),
                             headers={"Cache-Control": resp.headers.get(
                                 "cache-control", "public, max-age=300")})


@router.get("/local/{path:path}")
async def ha_local(request: Request, path: str):
    return await _static(request, "local", path)


@router.get("/hacsfiles/{path:path}")
async def ha_hacsfiles(request: Request, path: str):
    return await _static(request, "hacsfiles", path)


# --- per-device layouts -------------------------------------------------------

def _layout_store(state):
    store = getattr(state, "layouts", None)
    if store is None:
        from .layouts import LayoutStore
        store = LayoutStore()
        state.layouts = store
    return store


@router.get("/api/satellite/layout")
async def get_layout(request: Request):
    async def run(state, token):
        pid = state.pairing.public_id(token)
        return _layout_store(state).get(pid) or {"cards": [], "portrait": {"items": []},
                                                 "landscape": {"items": []}, "version": 0}
    return await _guard(request, "layout", run)


@router.put("/api/satellite/layout")
async def put_layout(request: Request):
    from .layouts import LayoutError

    async def run(state, token):
        body = await request.body()
        if len(body) > 2 * 1024 * 1024:
            raise HAError("layout too large", 413)
        import json
        try:
            raw = json.loads(body or b"{}")
        except ValueError:
            raise HAError("invalid JSON")
        try:
            return _layout_store(state).put(state.pairing.public_id(token), raw)
        except LayoutError as e:
            raise HAError(str(e))
    return await _guard(request, "layout", run)


@router.delete("/api/satellite/layout")
async def delete_layout(request: Request):
    async def run(state, token):
        _layout_store(state).delete(state.pairing.public_id(token))
        return {"ok": True}
    return await _guard(request, "layout", run)
