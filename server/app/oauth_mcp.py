"""Remote MCP for Claude (and other MCP clients), behind OAuth that only a
person at home can approve.

    https://<gateway>/mcp  ──gateway──▶  ai-server /remote-mcp/mcp

The LAN MCP server (`/mcp/sse`, every tool, no auth) is untouched. This module
adds a SECOND, internet-reachable MCP server with:

* **OAuth 2.1** as Claude's connectors expect it: protected-resource metadata
  (RFC 9728), authorization-server metadata (RFC 8414), dynamic client
  registration (RFC 7591), authorization code + S256 PKCE, rotating refresh
  tokens (reuse of a rotated refresh token revokes the whole grant).
* **Approval = possession of a PAL screen.** The consent page never approves on
  its own: the user presses "Show code", a 6-digit code appears on the hub (and
  on paired phones), and must be typed back. 5 attempts per code, 5 min TTL,
  codes shown at most once per 20 s / 10 per hour.
* **Tiers, enforced by construction.** Each tier is its own FastMCP instance
  built with an allowlist; tools outside it are removed, not hidden. Tools that
  mint credentials (pair_phone) or make the server fetch a caller-supplied URL
  (create_orb, show_image, play_video, stream_rtsp) or put arbitrary content on
  the house displays (show_qr_code) are in NO tier.
* **Separate tokens.** Access/refresh tokens live in their own store (hashed);
  they are NOT pairing tokens, so they open nothing but /mcp.

Disabled unless PAL_REMOTE_MCP=1 and HAL_GATEWAY_URL is set (and never in demo
mode). Grants are listed/revoked on the LAN: GET /api/oauth/grants,
POST /api/oauth/revoke {"grant_id"} | {"all": true} (+ an HA button).
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import html
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger("hal.remote_mcp")
router = APIRouter()

# --- configuration -------------------------------------------------------------------

ACCESS_TTL_S = 3600
REFRESH_TTL_S = 60 * 86400
AUTH_CODE_TTL_S = 60
APPROVAL_CODE_TTL_S = 300
APPROVAL_MAX_ATTEMPTS = 5
PENDING_TTL_S = 900
SHOW_MIN_INTERVAL_S = 20
SHOW_MAX_PER_HOUR = 10
MAX_CLIENTS = 100

SCOPES = {"pal.read": "read", "pal.control": "control"}

TIER_TOOLS = {
    "read": {
        "get_theme", "list_timers", "list_notes", "get_note", "list_orbs", "get_sun_times",
    },
}
TIER_TOOLS["control"] = TIER_TOOLS["read"] | {
    "set_volume", "adjust_volume", "toggle_mute", "set_theme",
    "show_calendar", "hide_calendar", "show_conversation_log", "hide_conversation_log",
    "show_notes", "hide_notes", "start_timer", "cancel_timer",
    "show_photo_frame", "hide_photo_frame", "set_display_power", "set_photo_frame_idle_minutes",
    "select_orb", "delete_note", "speak_verbatim", "show_camera", "stop_streaming",
}
# Never remote, whatever the tier (documented here so a future tier can't add
# them by accident: tests assert they're absent).
NEVER_REMOTE = {
    "pair_phone", "create_orb", "delete_orb", "show_image", "play_video", "stream_rtsp",
    "stream_camera", "show_qr_code",
}

TIER_LABEL = {
    "read": "Read only — notes, timers, theme, orbs, sun times",
    "control": "Read + control — also volume, displays, timers, notes, theme, orb, "
               "speaking aloud and showing a camera on the hub",
}

FIXED_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}


def enabled() -> bool:
    from .pairing import demo_mode
    return (os.environ.get("PAL_REMOTE_MCP", "").strip().lower() in ("1", "true", "yes", "on")
            and bool(public_base()) and not demo_mode())


def public_base() -> str:
    return os.environ.get("HAL_GATEWAY_URL", "").strip().rstrip("/")


def resource_url() -> str:
    return f"{public_base()}/mcp"


def prm_url() -> str:
    return f"{public_base()}/.well-known/oauth-protected-resource"


def _extra_redirects() -> set[str]:
    raw = os.environ.get("PAL_OAUTH_EXTRA_REDIRECTS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _is_loopback(uri: str) -> bool:
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    return u.scheme == "http" and (u.hostname or "") in ("localhost", "127.0.0.1", "::1")


def redirect_allowed(uri: str) -> bool:
    """Claude's hosted callback, loopback (Claude Code, any port), or an
    operator-configured extra. Nothing else: an open redirect allowlist would
    let anyone register a client that sends codes to their own site."""
    return uri in FIXED_REDIRECTS or uri in _extra_redirects() or _is_loopback(uri)


def redirect_matches(registered: list[str], uri: str) -> bool:
    """Exact match, or loopback ignoring the port (RFC 8252 §7.3)."""
    if uri in registered:
        return True
    if not _is_loopback(uri):
        return False
    u = urlsplit(uri)
    for r in registered:
        if _is_loopback(r):
            v = urlsplit(r)
            if (v.scheme, v.hostname, v.path) == (u.scheme, u.hostname, u.path):
                return True
    return False


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge or not (43 <= len(verifier) <= 128):
        return False
    d = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(d, challenge)


# --- persistent store ------------------------------------------------------------------

class OAuthStore:
    """Clients, grants and (hashed) tokens, persisted to a 0600 JSON file.
    Approval codes, pending authorizations and auth codes stay in memory."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("OAUTH_STORE_FILE", "/app/runtime/oauth_mcp.json")
        self.clients: dict[str, dict] = {}
        self.grants: dict[str, dict] = {}
        self.access: dict[str, dict] = {}     # sha(token) -> {grant, exp}
        self.pending: dict[str, dict] = {}    # rid -> authorize request
        self.codes: dict[str, dict] = {}      # auth code -> {...}
        self.show_times: list[float] = []
        self._load()

    # -- persistence
    def _load(self) -> None:
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.clients = d.get("clients", {})
            self.grants = d.get("grants", {})
            self.access = d.get("access", {})
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"oauth store unreadable ({type(e).__name__}): starting empty")

    def save(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if v["exp"] > now and v["grant"] in self.grants}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"clients": self.clients, "grants": self.grants, "access": self.access}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning(f"oauth store save failed: {type(e).__name__}: {e}")

    # -- clients (DCR)
    def register(self, name: str, redirect_uris: list[str]) -> dict:
        if len(self.clients) >= MAX_CLIENTS:
            used = {g["client_id"] for g in self.grants.values()}
            for cid in sorted(self.clients, key=lambda c: self.clients[c]["created"]):
                if cid not in used:
                    del self.clients[cid]
                    break
        cid = "pal-" + secrets.token_urlsafe(16)
        self.clients[cid] = {"name": (name or "MCP client")[:64], "redirect_uris": redirect_uris,
                             "created": time.time()}
        self.save()
        return {"client_id": cid, **self.clients[cid]}

    # -- tokens
    def _issue(self, gid: str) -> dict:
        g = self.grants[gid]
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.access[_h(access)] = {"grant": gid, "exp": time.time() + ACCESS_TTL_S}
        if g.get("refresh"):
            g.setdefault("rotated", []).append(g["refresh"])
            g["rotated"] = g["rotated"][-20:]
        g["refresh"] = _h(refresh)
        g["refresh_exp"] = time.time() + REFRESH_TTL_S
        self.save()
        scope = "pal.control" if g["tier"] == "control" else "pal.read"
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh, "scope": scope}

    def create_grant(self, client_id: str, tier: str) -> dict:
        gid = secrets.token_urlsafe(9)
        self.grants[gid] = {"client_id": client_id,
                            "client_name": self.clients.get(client_id, {}).get("name", "MCP client"),
                            "tier": tier, "created": time.time(), "last_used": None}
        return self._issue(gid)

    def refresh(self, client_id: str, token: str) -> dict | None:
        h = _h(token or "")
        now = time.time()
        for gid, g in list(self.grants.items()):
            if g.get("refresh") == h:
                if g["client_id"] != client_id or g.get("refresh_exp", 0) < now:
                    return None
                return self._issue(gid)
            if h in g.get("rotated", []):
                # a rotated refresh token came back: someone holds a copy → kill the grant
                log.warning(f"remote mcp: refresh token reuse on grant {gid} ({g['client_name']}) — revoked")
                self.revoke(gid)
                return None
        return None

    def check(self, token: str | None) -> dict | None:
        if not token:
            return None
        a = self.access.get(_h(token))
        if not a or a["exp"] < time.time() or a["grant"] not in self.grants:
            return None
        g = self.grants[a["grant"]]
        if not g.get("last_used") or time.time() - g["last_used"] > 300:
            g["last_used"] = time.time()
            self.save()
        return {"id": a["grant"], **g}

    def revoke(self, gid: str | None = None) -> int:
        ids = list(self.grants) if gid is None else ([gid] if gid in self.grants else [])
        for i in ids:
            del self.grants[i]
        self.access = {k: v for k, v in self.access.items() if v["grant"] in self.grants}
        self.save()
        return len(ids)

    def list_grants(self) -> list[dict]:
        return [{"id": gid, "client": g["client_name"], "tier": g["tier"],
                 "created": g["created"], "last_used": g.get("last_used")}
                for gid, g in sorted(self.grants.items(), key=lambda x: x[1]["created"])]

    # -- approval-code throttle
    def may_show(self) -> bool:
        now = time.time()
        self.show_times = [t for t in self.show_times if now - t < 3600]
        if self.show_times and now - self.show_times[-1] < SHOW_MIN_INTERVAL_S:
            return False
        return len(self.show_times) < SHOW_MAX_PER_HOUR

    def purge(self) -> None:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v["created"] < PENDING_TTL_S}
        self.codes = {k: v for k, v in self.codes.items() if v["exp"] > now}


_store: OAuthStore | None = None


def store() -> OAuthStore:
    global _store
    if _store is None:
        _store = OAuthStore()
    return _store


def _state(request: Request):
    from .main import _get_state
    return _get_state(request.app)


def _off() -> Response:
    return Response(status_code=404)


# --- discovery ----------------------------------------------------------------------------

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    if not enabled():
        return _off()
    return JSONResponse({
        "resource": resource_url(),
        "authorization_servers": [public_base()],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "PAL",
    })


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    if not enabled():
        return _off()
    b = public_base()
    return JSONResponse({
        "issuer": b,
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(SCOPES) + ["offline_access"],
    })


# --- dynamic client registration -------------------------------------------------------------

@router.post("/oauth/register")
async def oauth_register(request: Request):
    if not enabled():
        return _off()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "redirect_uris required"}, status_code=400)
    bad = [u for u in uris if not redirect_allowed(u)]
    if bad:
        log.info(f"remote mcp: registration refused for redirect {bad[0]!r}")
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "only Claude's callback or a loopback redirect"},
                            status_code=400)
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "public clients only (token_endpoint_auth_method=none)"},
                            status_code=400)
    c = store().register(str(body.get("client_name") or "MCP client"), uris)
    log.info(f"remote mcp: registered client {c['name']!r}")
    return JSONResponse({
        "client_id": c["client_id"], "client_id_issued_at": int(c["created"]),
        "client_name": c["name"], "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# --- authorization (the consent page) ----------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Connect to PAL</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:30rem;margin:2rem auto;padding:0 1rem}}
h1{{font-size:1.4rem}}.card{{background:#1c1c1f;border:1px solid #333;border-radius:14px;padding:1.2rem;margin:1rem 0}}
label{{display:block;margin:.6rem 0;line-height:1.35}}input[type=text]{{font-size:1.6rem;letter-spacing:.3em;width:9em;padding:.4rem;
border-radius:8px;border:1px solid #555;background:#000;color:#fff;text-align:center}}
button{{font-size:1rem;padding:.7rem 1.2rem;border-radius:10px;border:0;background:#e07a4f;color:#000;font-weight:600;margin-top:.8rem}}
.warn{{color:#f3b27a}}.dim{{color:#999;font-size:.9rem}}.err{{color:#ff7b7b}}code{{color:#fff}}</style></head>
<body><h1>Connect <b>{client}</b> to PAL</h1>{body}</body></html>"""


def _page(client: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(client=html.escape(client), body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"})


def _consent_body(p: dict, rid: str, err: str = "") -> str:
    host = urlsplit(p["redirect_uri"]).hostname or "?"
    warn = ("<p class=warn>This is a local app on a computer (loopback redirect). Only approve "
            "if you just started this from Claude Code yourself.</p>") if _is_loopback(p["redirect_uri"]) else ""
    tiers = "".join(
        f"<label><input type=radio name=tier value={t} {'checked' if t == p['tier'] else ''}> {html.escape(TIER_LABEL[t])}</label>"
        for t in ("read", "control"))
    return (f"<div class=card><p>After approval you'll be sent back to <code>{html.escape(host)}</code>.</p>{warn}"
            f"<form method=post action=/oauth/authorize><input type=hidden name=rid value={rid}>"
            f"<input type=hidden name=action value=show>{tiers}"
            f"<p class=dim>Never available remotely: pairing new devices, fetching web URLs, "
            f"showing QR codes.</p>"
            f"<button>Show approval code on my PAL screens</button></form>"
            f"{'<p class=err>' + html.escape(err) + '</p>' if err else ''}</div>")


def _code_body(rid: str, err: str = "") -> str:
    return (f"<div class=card><p>A 6-digit code is now on your hub screen (and on your paired "
            f"phones). Type it here:</p><form method=post action=/oauth/authorize autocomplete=off>"
            f"<input type=hidden name=rid value={rid}><input type=hidden name=action value=approve>"
            f"<input type=text name=code inputmode=numeric maxlength=6 autofocus>"
            f"<br><button>Approve</button></form>"
            f"{'<p class=err>' + html.escape(err) + '</p>' if err else ''}"
            f"<p class=dim>Not at home? Approve later — the code only appears on your own PAL screens.</p></div>")


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    if not enabled():
        return _off()
    s = store()
    s.purge()
    q = request.query_params
    client = s.clients.get(q.get("client_id", ""))
    redirect_uri = q.get("redirect_uri", "")
    if not client or not redirect_uri or not redirect_matches(client["redirect_uris"], redirect_uri):
        # never redirect to an unverified URI: show the error here
        return _page("an unknown app", "<p class=err>This connection request isn't valid "
                     "(unknown client or redirect). Start again from Claude.</p>", 400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" \
            or not q.get("code_challenge"):
        return _redirect_error(redirect_uri, q.get("state"), "invalid_request",
                               "S256 PKCE and response_type=code are required")
    res = q.get("resource")
    if res and res.rstrip("/") != resource_url():
        return _redirect_error(redirect_uri, q.get("state"), "invalid_target", "unknown resource")
    scopes = (q.get("scope") or "").split()
    tier = "control" if "pal.control" in scopes else "read"
    rid = secrets.token_urlsafe(18)
    s.pending[rid] = {"client_id": q["client_id"], "client_name": client["name"],
                      "redirect_uri": redirect_uri, "state": q.get("state"),
                      "challenge": q["code_challenge"], "tier": tier, "created": time.time(),
                      "code": None, "code_exp": 0, "attempts": 0}
    return _page(client["name"], _consent_body(s.pending[rid], rid))


def _redirect_error(uri: str, state: str | None, err: str, desc: str) -> Response:
    params = {"error": err, "error_description": desc}
    if state:
        params["state"] = state
    return RedirectResponse(f"{uri}{'&' if '?' in uri else '?'}{urlencode(params)}", status_code=302)


async def _show_code(request: Request, code: str) -> None:
    from .pairing import _push_pairing
    st = _state(request)
    await _push_pairing(st, {"type": "show_pairing_code", "code": code, "expires_in": APPROVAL_CODE_TTL_S,
                             "title": "Connect Claude", "hint": "Type this code to approve the connection"})

    async def _hide_later():
        await asyncio.sleep(APPROVAL_CODE_TTL_S)
        await _push_pairing(st, {"type": "hide_pairing_code"})
    asyncio.create_task(_hide_later())


@router.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    if not enabled():
        return _off()
    s = store()
    s.purge()
    form = await request.form()
    rid = str(form.get("rid", ""))
    p = s.pending.get(rid)
    if not p:
        return _page("PAL", "<p class=err>This request expired. Start again from Claude.</p>", 400)
    action = form.get("action")
    if action == "show":
        tier = str(form.get("tier", p["tier"]))
        p["tier"] = tier if tier in TIER_TOOLS else "read"
        if not s.may_show():
            return _page(p["client_name"], _consent_body(p, rid, "A code was shown moments ago — "
                         "wait a little and try again."), 429)
        s.show_times.append(time.time())
        p["code"] = "".join(secrets.choice("0123456789") for _ in range(6))
        p["code_exp"] = time.time() + APPROVAL_CODE_TTL_S
        p["attempts"] = 0
        await _show_code(request, p["code"])
        log.info(f"remote mcp: approval code shown for {p['client_name']!r} ({p['tier']})")
        return _page(p["client_name"], _code_body(rid))
    if action == "approve":
        if not p["code"] or p["code_exp"] < time.time():
            return _page(p["client_name"], _consent_body(p, rid, "The code expired — show a new one."))
        p["attempts"] += 1
        typed = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
        if not secrets.compare_digest(typed, p["code"]):
            if p["attempts"] >= APPROVAL_MAX_ATTEMPTS:
                s.pending.pop(rid, None)
                log.warning(f"remote mcp: too many wrong codes for {p['client_name']!r} — request dropped")
                return _page(p["client_name"], "<p class=err>Too many wrong codes. Start again from Claude.</p>", 403)
            return _page(p["client_name"], _code_body(rid, f"Wrong code ({APPROVAL_MAX_ATTEMPTS - p['attempts']} tries left)."))
        s.pending.pop(rid, None)
        from .pairing import _push_pairing
        await _push_pairing(_state(request), {"type": "hide_pairing_code"})
        code = secrets.token_urlsafe(32)
        s.codes[code] = {"client_id": p["client_id"], "redirect_uri": p["redirect_uri"],
                         "challenge": p["challenge"], "tier": p["tier"],
                         "exp": time.time() + AUTH_CODE_TTL_S}
        log.info(f"remote mcp: approved {p['client_name']!r} ({p['tier']})")
        params = {"code": code, "iss": public_base()}
        if p.get("state"):
            params["state"] = p["state"]
        uri = p["redirect_uri"]
        return RedirectResponse(f"{uri}{'&' if '?' in uri else '?'}{urlencode(params)}", status_code=302)
    return _page(p["client_name"], "<p class=err>Unknown action.</p>", 400)


# --- token endpoint ------------------------------------------------------------------------

def _tok_err(err: str, status: int = 400, desc: str = "") -> JSONResponse:
    body = {"error": err}
    if desc:
        body["error_description"] = desc
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/oauth/token")
async def oauth_token(request: Request):
    if not enabled():
        return _off()
    s = store()
    s.purge()
    form = await request.form()
    gt = form.get("grant_type")
    client_id = str(form.get("client_id", ""))
    if client_id not in s.clients:
        return _tok_err("invalid_client", 401)
    if gt == "authorization_code":
        c = s.codes.pop(str(form.get("code", "")), None)       # single use, even on failure
        if not c or c["client_id"] != client_id or c["redirect_uri"] != form.get("redirect_uri"):
            return _tok_err("invalid_grant")
        if not pkce_ok(str(form.get("code_verifier", "")), c["challenge"]):
            return _tok_err("invalid_grant", desc="PKCE verification failed")
        out = s.create_grant(client_id, c["tier"])
    elif gt == "refresh_token":
        out = s.refresh(client_id, str(form.get("refresh_token", "")))
        if out is None:
            return _tok_err("invalid_grant")
    else:
        return _tok_err("unsupported_grant_type")
    return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# --- LAN admin -------------------------------------------------------------------------------

@router.get("/api/oauth/grants")
async def oauth_grants(request: Request):
    return {"enabled": enabled(), "grants": store().list_grants()}


@router.post("/api/oauth/revoke")
async def oauth_revoke(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    n = store().revoke(None if body.get("all") else str(body.get("grant_id", "")))
    log.info(f"remote mcp: revoked {n} grant(s)")
    return {"revoked": n}


# --- the gated MCP endpoint ------------------------------------------------------------------

current_grant: contextvars.ContextVar[dict | None] = contextvars.ContextVar("pal_mcp_grant", default=None)


class RemoteMCPApp:
    """ASGI app mounted at /remote-mcp. Every request needs a valid access
    token; it is then served by the FastMCP instance of the grant's tier.
    Anything else gets the 401 + WWW-Authenticate Claude needs to start OAuth."""

    def __init__(self, tier_apps: dict):
        self.tier_apps = tier_apps

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        g = store().check(token) if enabled() else None
        if g is None or g["tier"] not in self.tier_apps:
            await _send_401(send, "invalid_token" if token else None)
            return
        # audit: which tool the grant calls (JSON-RPC body), then replay the body
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        try:
            rpc = json.loads(body or b"{}")
            for m in rpc if isinstance(rpc, list) else [rpc]:
                if isinstance(m, dict) and m.get("method") == "tools/call":
                    log.info(f"remote mcp: {g['client_name']!r} ({g['tier']}) → "
                             f"{(m.get('params') or {}).get('name')}")
        except Exception:
            pass
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        tok = current_grant.set(g)
        try:
            await self.tier_apps[g["tier"]](scope, replay, send)
        finally:
            current_grant.reset(tok)


async def _send_401(send, err: str | None):
    v = f'Bearer resource_metadata="{prm_url()}", scope="pal.read"'
    if err:
        v += f', error="{err}"'
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"www-authenticate", v.encode()), (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


async def mount_remote_mcp(app, state, stack) -> bool:
    """Called from main.lifespan: build one FastMCP per tier, start their
    session managers on `stack` (an AsyncExitStack closed at shutdown), mount."""
    if not enabled():
        log.info("remote MCP disabled (PAL_REMOTE_MCP / HAL_GATEWAY_URL)")
        return False
    from .mcp_server import build_mcp_server
    apps = {}
    for tier, allow in TIER_TOOLS.items():
        assert not (allow & NEVER_REMOTE), "a never-remote tool slipped into a tier"
        m = build_mcp_server(state, allow=allow, stateless_http=True, json_response=True,
                             streamable_http_path="/mcp")
        apps[tier] = m.streamable_http_app()
        await stack.enter_async_context(m.session_manager.run())
    app.mount("/remote-mcp", RemoteMCPApp(apps))
    log.info(f"remote MCP mounted for {resource_url()} (tiers: "
             f"{', '.join(f'{t}={len(a)}' for t, a in TIER_TOOLS.items())})")
    return True
