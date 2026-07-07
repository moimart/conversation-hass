"""Device pairing + token auth for the mobile companion app.

The server is otherwise unauthenticated (LAN trust). Pairing lets a phone
exchange a short-lived numeric code — shown on the kiosk display — for a
long-lived device token, which it then presents on /api/command (Bearer) and
/ws/ui (?token=). Enforcement is OPT-IN via the HAL_REQUIRE_TOKEN env flag so the
existing unauthenticated kiosk/RPi keep working until the user turns it on.

Codes are in-memory and ephemeral (single-use, ~120s). Tokens are persisted to a
small JSON file under the runtime dir so they survive restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

log = logging.getLogger("hal.pairing")

CODE_TTL_S = 120.0
CODE_LEN = 6
REDEEM_WINDOW_S = 60.0          # throttle window
MAX_REDEEM_ATTEMPTS = 10        # max redeem attempts per window
_DEMO_TOKEN_CAP = 50            # public demo: cap accumulated demo tokens

# --- Token scopes -------------------------------------------------------------
# A token's scope bounds what it may do. "full" (the phone app, and every token
# issued before scopes existed) is unrestricted. "watch" is the standalone
# Apple Watch companion: it may speak to PAL through the LLM, probe its own
# validity, and register for push — nothing else (no /ws/ui mirror, no cloud
# override, no satellite routes). Scope limits the SURFACE, not the power of
# /api/command itself; its real wins are independent revocation and no lateral
# access. A scoped token is minted by `derive` (authorized by a full token),
# never by code redemption.
_SCOPE_PERMISSIONS: dict[str, set[str] | None] = {
    "full": None,                                   # None = unrestricted
    "watch": {"command", "pair_status", "push_register"},
}
DERIVABLE_SCOPES = {"watch"}


def _tokens_path() -> str:
    return os.environ.get("PAIRING_TOKENS_FILE", "/app/runtime/pairing_tokens.json")


def require_token_enabled() -> bool:
    """Whether token auth is enforced. Default OFF (LAN trust); turn on once a
    phone is paired by setting HAL_REQUIRE_TOKEN=1."""
    return os.environ.get("HAL_REQUIRE_TOKEN", "").strip().lower() in ("1", "true", "yes", "on")


def demo_pair_code() -> str | None:
    """The standing pairing code accepted by the public demo instance, or None.

    Set HAL_DEMO_PAIR_CODE (e.g. "000000") ONLY on the App-Store-review demo
    server. Unset in every real deployment ⇒ this whole path is dead code and
    the normal random single-use code flow is unchanged."""
    c = os.environ.get("HAL_DEMO_PAIR_CODE", "").strip()
    return c or None


def demo_mode() -> bool:
    """Public-demo behaviour switch (App Store review instance). When on, the
    assistant is a PURE LLM chat: no tools, no MCP, no intent-guard direct calls,
    a dummy activity log, and a locked theme. Set HAL_DEMO_MODE=1 ONLY on the demo
    server; unset everywhere else so real deployments are untouched."""
    return os.environ.get("HAL_DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def extract_bearer(request: Request) -> str | None:
    """Pull a Bearer token from the Authorization header, or None."""
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


class PairingManager:
    """Holds pending pairing codes (ephemeral) and issued device tokens
    (persisted). Not thread-safe; the server runs single-threaded asyncio."""

    def __init__(self) -> None:
        self._codes: dict[str, float] = {}     # code -> expiry (monotonic)
        self._tokens: dict[str, dict] = {}     # token -> {device_name, created_at}
        self._redeem_hits: list[float] = []    # throttle timestamps
        self._load()

    # --- persistence -------------------------------------------------------
    def _load(self) -> None:
        path = _tokens_path()
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._tokens = {k: v for k, v in data.items() if isinstance(k, str)}
                log.info(f"pairing: loaded {len(self._tokens)} device token(s)")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"pairing: could not load tokens from {path}: {e}")

    def _save(self) -> None:
        path = _tokens_path()
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pairing_tokens.", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._tokens, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- codes -------------------------------------------------------------
    def _purge_expired(self) -> None:
        now = time.monotonic()
        self._codes = {c: e for c, e in self._codes.items() if e > now}

    def create_code(self) -> tuple[str, int]:
        """Mint a single-use numeric code valid for CODE_TTL_S seconds."""
        self._purge_expired()
        code = ""
        for _ in range(20):
            code = "".join(secrets.choice("0123456789") for _ in range(CODE_LEN))
            if code not in self._codes:
                break
        self._codes[code] = time.monotonic() + CODE_TTL_S
        return code, int(CODE_TTL_S)

    def expire_code(self, code: str) -> None:
        self._codes.pop(code, None)

    # --- redeem / tokens ---------------------------------------------------
    def throttled(self) -> bool:
        """True if too many redeem attempts in the recent window."""
        now = time.monotonic()
        self._redeem_hits = [t for t in self._redeem_hits if now - t < REDEEM_WINDOW_S]
        if len(self._redeem_hits) >= MAX_REDEEM_ATTEMPTS:
            return True
        self._redeem_hits.append(now)
        return False

    def redeem(self, code: str, device_name: str,
               scope: str = "full") -> str | None:
        """Exchange a valid code for a new device token, or None if invalid.

        `scope` lets a device self-enroll least-privilege: a watch redeems with
        scope="watch". Unknown scopes are rejected (None) — the caller has the
        kiosk code either way, so client-chosen scope only ever *narrows*."""
        if scope not in _SCOPE_PERMISSIONS:
            return None
        self._purge_expired()
        code = (code or "").strip()
        demo = demo_pair_code()
        if demo is not None and code == demo:
            # Public review demo: the standing code always redeems (no code shown
            # on a display the reviewer can't see), minting a UNIQUE per-device
            # token so each device keeps its own satellite socket.
            return self._demo_token(device_name)
        if code not in self._codes:
            return None
        del self._codes[code]                       # single-use
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "device_name": (device_name or "device")[:64],
            "created_at": time.time(),
            "scope": scope,
        }
        self._save()
        log.info(f"pairing: issued {scope!r} token for device "
                 f"{self._tokens[token]['device_name']!r}")
        return token

    def _demo_token(self, device_name: str) -> str:
        """Mint a UNIQUE full token for each demo redeem.

        The demo code is shared (000000), but each device MUST get its own token:
        `state.satellite_ws` is keyed by token and enforces one live socket per
        token (routes_ws.py), so a shared token makes concurrent demo devices
        evict each other's /ws/ui socket mid-turn — replies never arrive. Unique
        tokens keep each device's socket alive. Tagged `demo:true` and capped so
        the public demo can't grow the token file without bound."""
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "device_name": (device_name or "App Review")[:64],
            "created_at": time.time(),
            "scope": "full",
            "demo": True,
        }
        # Cap accumulation: keep only the newest _DEMO_TOKEN_CAP demo tokens.
        demo_toks = sorted(
            ((m.get("created_at", 0.0), t) for t, m in self._tokens.items()
             if isinstance(m, dict) and m.get("demo")),
        )
        for _, old in demo_toks[:-_DEMO_TOKEN_CAP]:
            self._tokens.pop(old, None)
        self._save()
        log.info("pairing: issued demo token "
                 f"for {self._tokens[token]['device_name']!r}")
        return token

    def derive(self, parent_token: str | None, scope: str,
               device_name: str) -> str | None:
        """Mint a scoped child token, authorized by an existing FULL token.

        The phone-assisted enrollment primitive: a device that already holds
        full trust vouches for a less-privileged one (the watch) — no code
        typing on the new device. Children cannot derive further tokens
        (scoped parents are rejected), and `scope` must be a known derivable
        scope ("full" is deliberately not derivable). Returns None when the
        parent is invalid/insufficient or the scope unknown."""
        if not self.is_valid_token(parent_token):
            return None
        if self.token_scope(parent_token) != "full":
            return None
        if scope not in DERIVABLE_SCOPES:
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "device_name": (device_name or "device")[:64],
            "created_at": time.time(),
            "scope": scope,
            "derived_from": parent_token[:6],   # prefix only — never a copy of the secret
        }
        self._save()
        log.info(f"pairing: derived {scope!r} token for "
                 f"{self._tokens[token]['device_name']!r} "
                 f"(parent {parent_token[:6]}…)")
        return token

    def is_valid_token(self, token: str | None) -> bool:
        return bool(token) and token in self._tokens

    def token_scope(self, token: str | None) -> str | None:
        """The scope of a valid token ("full" for pre-scope entries), or None
        for an unknown/absent token."""
        if not token:
            return None
        entry = self._tokens.get(token)
        if entry is None:
            return None
        return entry.get("scope", "full")

    def scope_allows(self, token: str | None, permission: str) -> bool:
        """True iff `token` is valid AND its scope grants `permission`.
        Unknown scopes deny everything (fail closed)."""
        scope = self.token_scope(token)
        if scope is None:
            return False
        perms = _SCOPE_PERMISSIONS.get(scope)
        if perms is None:
            return scope in _SCOPE_PERMISSIONS   # "full" → unrestricted
        return permission in perms

    def device_name(self, token: str | None) -> str | None:
        """Human-readable name of the paired device for a token, or None.
        Used wherever a device must be identified WITHOUT exposing the secret
        token (e.g. the conversation log's origin column)."""
        if not token:
            return None
        entry = self._tokens.get(token)
        return entry.get("device_name") if entry else None

    @staticmethod
    def public_id(token: str | None) -> str | None:
        """A stable, non-secret PUBLIC address for a device, derived from its
        token: sha256(token)[:16]. Stable across restarts, collision-resistant,
        and NOT reversible to the secret token — so it can be handed to other
        clients (the intercom directory) as the call-routing address without
        leaking the bearer credential."""
        if not token:
            return None
        return hashlib.sha256(token.encode()).hexdigest()[:16]

    def token_for_public_id(self, public_id: str | None) -> str | None:
        """Resolve a public_id back to the secret token (server-side only), or
        None. O(devices) — fine for a household."""
        if not public_id:
            return None
        for token in self._tokens:
            if hashlib.sha256(token.encode()).hexdigest()[:16] == public_id:
                return token
        return None

    def revoke(self, token: str) -> bool:
        if token in self._tokens:
            del self._tokens[token]
            self._save()
            return True
        return False

    def rename(self, token: str | None, new_name: str) -> bool:
        """Set the friendly device_name for a token (self-service from the app).
        Lets the user give each device a unique, memorable name — iOS reports
        'iPhone' for EVERY device (privacy), so defaults collide and the intercom
        directory/voice-call ('call the kitchen') can't tell them apart."""
        entry = self._tokens.get(token) if token else None
        name = (new_name or "").strip()[:64]
        if entry is None or not name:
            return False
        entry["device_name"] = name
        self._save()
        log.info(f"device renamed → {name!r}")
        return True

    def revoke_by_device_name(self, device_name: str) -> int:
        """Revoke every token whose device_name matches (case-insensitive).
        Returns the count removed. Lets an admin deauthorize a phone by its
        friendly name without ever handling the secret token."""
        name = (device_name or "").strip().lower()
        if not name:
            return 0
        victims = [t for t, e in self._tokens.items()
                   if (e.get("device_name") or "").strip().lower() == name]
        for t in victims:
            del self._tokens[t]
        if victims:
            self._save()
        return len(victims)

    def list_devices(self) -> list[dict]:
        """Paired devices for the admin UI — device_name + created_at + a short
        token PREFIX for disambiguation. NEVER the full token (or push token)."""
        out = []
        for token, entry in self._tokens.items():
            out.append({
                "device_name": entry.get("device_name"),
                "created_at": entry.get("created_at"),
                "token_prefix": token[:6],
                "has_push": bool(entry.get("push_token")),
                "scope": entry.get("scope", "full"),
            })
        return sorted(out, key=lambda d: d.get("created_at") or 0)

    # --- push tokens -------------------------------------------------------
    def set_push_token(self, token: str, service: str, push_token: str) -> bool:
        """Attach (or upsert) a device's APNs/FCM push token to its pairing
        entry. The store is schemaless so no migration is needed. Returns False
        if the pairing token is unknown."""
        entry = self._tokens.get(token)
        if entry is None:
            return False
        # A push token identifies a single app install; keep it under exactly
        # one pairing entry so a re-paired device isn't notified once per stale
        # pairing token (the duplicate-FCM double-notification bug).
        for other_tok, other in self._tokens.items():
            if other_tok != token and other.get("push_token") == push_token:
                other.pop("push_token", None)
                other.pop("push_service", None)
                other.pop("push_registered_at", None)
        entry["push_service"] = service
        entry["push_token"] = push_token
        entry["push_registered_at"] = time.time()
        self._save()
        return True

    def clear_push_token(self, token: str) -> None:
        """Drop a device's push token (e.g. after APNs/FCM reports it dead)."""
        entry = self._tokens.get(token)
        if entry and ("push_token" in entry or "push_service" in entry):
            entry.pop("push_token", None)
            entry.pop("push_service", None)
            entry.pop("push_registered_at", None)
            self._save()

    def push_targets(self) -> list[tuple[str, str, str]]:
        """(pairing_token, service, push_token) for every device that has
        registered a push token. The push layer filters this to the offline set."""
        out = []
        for token, entry in self._tokens.items():
            pt = entry.get("push_token")
            svc = entry.get("push_service")
            if pt and svc:
                out.append((token, svc, pt))
        return out


# === Routes ===================================================================
router = APIRouter()


class RedeemRequest(BaseModel):
    code: str
    device_name: str = "device"
    scope: str = "full"      # "watch" lets a watch self-enroll least-privilege


def _json_error(payload: dict, status: int) -> Response:
    return Response(content=json.dumps(payload), status_code=status,
                    media_type="application/json")


async def _push_pairing(state, msg: dict) -> None:
    """Send a pairing overlay message to UI clients AND the kiosk (via RPi)."""
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)
    if state.audio_websocket:
        try:
            await state.audio_websocket.send_json(msg)
        except Exception:
            pass


async def begin_pairing(state) -> tuple[str, int]:
    """Mint a pairing code, show it full-screen on the kiosk display, and
    schedule it to auto-hide on expiry. Returns (code, ttl_seconds). Shared by
    the REST route and the `pair_phone` local tool."""
    code, ttl = state.pairing.create_code()
    await _push_pairing(state, {"type": "show_pairing_code", "code": code, "expires_in": ttl})

    async def _hide_later() -> None:
        await asyncio.sleep(ttl)
        # Only hide if this code is still the pending one (not already redeemed).
        state.pairing.expire_code(code)
        await _push_pairing(state, {"type": "hide_pairing_code"})

    asyncio.create_task(_hide_later())
    return code, ttl


@router.post("/api/pair/request")
async def pair_request(request: Request):
    """Mint a pairing code and show it on the display. Called when the user
    asks HAL to pair a phone."""
    from .main import _get_state
    state = _get_state(request.app)
    code, ttl = await begin_pairing(state)
    return {"code": code, "expires_in": ttl}


@router.post("/api/pair/redeem")
async def pair_redeem(request: Request, req: RedeemRequest):
    """Exchange a code for a device token (called by the mobile app)."""
    from .main import _get_state
    state = _get_state(request.app)
    if state.pairing.throttled():
        return _json_error({"error": "too_many_attempts"}, 429)
    scope = (req.scope or "full").strip()
    if scope not in _SCOPE_PERMISSIONS:
        return _json_error({"error": "invalid_scope"}, 400)
    token = state.pairing.redeem(req.code, req.device_name, scope)
    if token is None:
        return _json_error({"error": "invalid_or_expired"}, 400)
    await _push_pairing(state, {"type": "hide_pairing_code"})
    # gateway_url (optional): the public satellite-gateway base. Pairing is
    # local-only, but the device keeps this so it can reach PAL from anywhere
    # afterwards (it tries the local URL first, then falls back to this).
    return {
        "token": token,
        "scope": scope,
        "server_name": os.environ.get("HAL_DEVICE_NAME", "HAL"),
        "gateway_url": os.environ.get("HAL_GATEWAY_URL", "").strip(),
    }


@router.get("/api/pair/status")
async def pair_status(request: Request):
    """Probe whether a Bearer token is still valid (used at app launch). Returns
    the device's current name so the settings sheet can prefill the rename field."""
    from .main import _get_state
    state = _get_state(request.app)
    token = extract_bearer(request)
    if state.pairing.is_valid_token(token):
        return {"valid": True, "device_name": state.pairing.device_name(token) or ""}
    return _json_error({"valid": False}, 401)


class RenameRequest(BaseModel):
    name: str


@router.post("/api/pair/rename")
async def pair_rename(request: Request, req: RenameRequest):
    """Rename THIS device (the caller's own token). Each device names itself, so
    'iPhone'/'Android device' defaults can be made unique + memorable."""
    from .main import _get_state
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not (token and state.pairing.is_valid_token(token)):
        return _json_error({"error": "unauthorized"}, 401)
    if state.pairing.rename(token, req.name):
        return {"ok": True, "device_name": state.pairing.device_name(token)}
    return _json_error({"error": "bad_name"}, 400)


class RevokeRequest(BaseModel):
    device_name: str | None = None
    token: str | None = None


@router.get("/api/pair/devices")
async def pair_devices(request: Request):
    """List paired devices (name + created_at + token PREFIX, never the full
    token). LAN-only admin route — the satellite gateway does NOT proxy
    /api/pair/devices or /api/pair/revoke, so device management stays home-side
    exactly like pairing itself."""
    from .main import _get_state
    state = _get_state(request.app)
    return {"devices": state.pairing.list_devices()}


@router.post("/api/pair/revoke")
async def pair_revoke(request: Request, req: RevokeRequest):
    """Deauthorize a paired device — by friendly `device_name` (revokes every
    token for that name) or by exact `token`. LAN-only (not gateway-proxied);
    this is the 'lost phone' kill switch. A revoked token stops working
    immediately on the server and within the gateway's auth-cache TTL (~30s)
    on the remote path."""
    from .main import _get_state
    state = _get_state(request.app)
    if req.token:
        ok = state.pairing.revoke(req.token.strip())
        return {"revoked": 1 if ok else 0}
    if req.device_name:
        n = state.pairing.revoke_by_device_name(req.device_name)
        return {"revoked": n}
    return _json_error({"error": "device_name or token required"}, 400)


class DeriveRequest(BaseModel):
    scope: str = "watch"
    device_name: str = "Apple Watch"


@router.post("/api/pair/derive")
async def pair_derive(request: Request, req: DeriveRequest):
    """Mint a scoped child token, authorized by the caller's FULL pairing token
    (Bearer). The phone-assisted enrollment path for the watch: the phone calls
    this, then hands the child token to the watch over WatchConnectivity — no
    code typing on the watch. LAN-only BY OMISSION: the satellite gateway does
    not proxy this route, so (like code pairing) tokens can only be minted at
    home. Mirrors redeem's response shape so the enrollee also learns the
    gateway base for away-from-home failover."""
    from .main import _get_state
    state = _get_state(request.app)
    parent = extract_bearer(request)
    if not state.pairing.is_valid_token(parent) or \
            state.pairing.token_scope(parent) != "full":
        return _json_error({"error": "unauthorized"}, 401)
    child = state.pairing.derive(parent, (req.scope or "").strip(),
                                 req.device_name)
    if child is None:
        return _json_error({"error": "invalid_scope"}, 400)
    return {
        "token": child,
        "scope": (req.scope or "").strip(),
        "server_name": os.environ.get("HAL_DEVICE_NAME", "HAL"),
        "gateway_url": os.environ.get("HAL_GATEWAY_URL", "").strip(),
    }


class PushRegisterRequest(BaseModel):
    platform: str            # "ios" | "android" (aliases apns/fcm accepted)
    push_token: str


# Map the app's platform string to our internal transport name.
_PUSH_SERVICE = {"ios": "apns", "apns": "apns", "android": "fcm", "fcm": "fcm"}


@router.post("/api/pair/push-register")
async def pair_push_register(request: Request, req: PushRegisterRequest):
    """Register (upsert) a paired device's APNs/FCM push token so PAL can notify
    it while its app is closed. Requires the device's Bearer pairing token. The
    app calls this after pairing, on every cold launch, and on token refresh.
    Token-gated at the gateway edge too."""
    from .main import _get_state
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not state.pairing.is_valid_token(token):
        return _json_error({"error": "unauthorized"}, 401)
    service = _PUSH_SERVICE.get((req.platform or "").strip().lower())
    if service is None:
        return _json_error({"error": "invalid_platform"}, 400)
    push_token = (req.push_token or "").strip()
    if not push_token:
        return _json_error({"error": "missing_push_token"}, 400)
    state.pairing.set_push_token(token, service, push_token)
    return {"status": "ok", "service": service}
