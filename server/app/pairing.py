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


def _tokens_path() -> str:
    return os.environ.get("PAIRING_TOKENS_FILE", "/app/runtime/pairing_tokens.json")


def require_token_enabled() -> bool:
    """Whether token auth is enforced. Default OFF (LAN trust); turn on once a
    phone is paired by setting HAL_REQUIRE_TOKEN=1."""
    return os.environ.get("HAL_REQUIRE_TOKEN", "").strip().lower() in ("1", "true", "yes", "on")


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

    def redeem(self, code: str, device_name: str) -> str | None:
        """Exchange a valid code for a new device token, or None if invalid."""
        self._purge_expired()
        code = (code or "").strip()
        if code not in self._codes:
            return None
        del self._codes[code]                       # single-use
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {
            "device_name": (device_name or "device")[:64],
            "created_at": time.time(),
        }
        self._save()
        log.info(f"pairing: issued token for device {self._tokens[token]['device_name']!r}")
        return token

    def is_valid_token(self, token: str | None) -> bool:
        return bool(token) and token in self._tokens

    def device_name(self, token: str | None) -> str | None:
        """Human-readable name of the paired device for a token, or None.
        Used wherever a device must be identified WITHOUT exposing the secret
        token (e.g. the conversation log's origin column)."""
        if not token:
            return None
        entry = self._tokens.get(token)
        return entry.get("device_name") if entry else None

    def revoke(self, token: str) -> bool:
        if token in self._tokens:
            del self._tokens[token]
            self._save()
            return True
        return False

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
    token = state.pairing.redeem(req.code, req.device_name)
    if token is None:
        return _json_error({"error": "invalid_or_expired"}, 400)
    await _push_pairing(state, {"type": "hide_pairing_code"})
    # gateway_url (optional): the public satellite-gateway base. Pairing is
    # local-only, but the phone keeps this so it can reach PAL from anywhere
    # afterwards (it tries the local URL first, then falls back to this).
    return {
        "token": token,
        "server_name": os.environ.get("HAL_DEVICE_NAME", "HAL"),
        "gateway_url": os.environ.get("HAL_GATEWAY_URL", "").strip(),
    }


@router.get("/api/pair/status")
async def pair_status(request: Request):
    """Probe whether a Bearer token is still valid (used at app launch)."""
    from .main import _get_state
    state = _get_state(request.app)
    if state.pairing.is_valid_token(extract_bearer(request)):
        return {"valid": True}
    return _json_error({"valid": False}, 401)


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
