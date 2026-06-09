"""Native push notifications (APNs + FCM) for paired devices whose app is
CLOSED — i.e. NOT currently connected as a satellite (`state.satellite_ws`).

Three event classes (see plan): speak text, finished-timer text, and orb
images. Images ride as an **inline thumbnail** the device fetches from the
user's own gateway via a short-lived HMAC-signed URL (`/api/push/image/N.jpg`);
the bytes never touch Apple/Google — only the notification + a signed URL do.

Transport-agnostic: `PushService.dispatch()` targets the offline-with-push-token
set and fans out to `ApnsSender` / `FcmSender`. Sends are fire-and-forget so the
live path is never blocked; each send re-checks connectivity (closing the
reconnect race) and clears a token the platform reports dead (APNs 410 /
Unregistered, FCM UNREGISTERED/NOT_FOUND).

No-ops entirely until `push_providers.json` is configured.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time

import httpx

log = logging.getLogger("hal.push")

_BODY_MAX = 1000            # APNs/FCM payloads cap ~4KB; keep the body well under
_IMAGE_TTL_S = 600         # signed image URL lifetime (also the verify clamp)
_JWT_REUSE_S = 3000        # reuse the APNs provider JWT (<60min, Apple requires)


# --- signed image URLs -------------------------------------------------------
# Sign over the canonical path+query (NOT including &sig=). The .jpg extension is
# mandatory so iOS NSE / Android image loaders infer the type.

def _canonical(row_id: int, exp: int) -> str:
    return f"/api/push/image/{int(row_id)}.jpg?exp={int(exp)}"


def _sig(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def sign_image_url(gateway_base: str, secret: str, row_id: int, ttl: int = _IMAGE_TTL_S) -> str:
    exp = int(time.time()) + int(ttl)
    canonical = _canonical(row_id, exp)
    return f"{gateway_base.rstrip('/')}{canonical}&sig={_sig(secret, canonical)}"


def verify_image_sig(secret: str, row_id: int, exp, sig, *, now: float | None = None,
                     max_ttl: int = _IMAGE_TTL_S) -> bool:
    """True iff sig matches and exp is in (now, now+max_ttl]. Constant-time."""
    if not secret or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    now_i = int(now if now is not None else time.time())
    if exp_i < now_i or exp_i > now_i + int(max_ttl):
        return False
    expected = _sig(secret, _canonical(row_id, exp_i))
    return hmac.compare_digest(expected, str(sig))


# --- payload builders (pure) -------------------------------------------------

def _clip(text: str) -> str:
    return (text or "").strip()[:_BODY_MAX]


def _title(kind: str) -> str:
    return {"timer": "Timer", "image": "PAL", "speak": "PAL"}.get(kind, "PAL")


def build_apns_payload(kind: str, text: str, image_url: str | None, category: str | None) -> dict:
    aps: dict = {"alert": {"title": _title(kind), "body": _clip(text)}, "sound": "default"}
    if category:
        aps["category"] = category
    if image_url:
        # mutable-content lets the Notification Service Extension fetch + attach
        # the image. Omitted for text-only pushes, so the NSE never runs for them.
        aps["mutable-content"] = 1
    payload: dict = {"aps": aps}
    if image_url:
        payload["image_url"] = image_url
    return payload


def build_fcm_message(token: str, kind: str, text: str, image_url: str | None,
                      category: str | None) -> dict:
    channel = "timers" if category == "timer" else "announcements"
    notif: dict = {"title": _title(kind), "body": _clip(text)}
    if image_url:
        notif["image"] = image_url          # system renders BigPicture when backgrounded
    return {
        "token": token,
        "notification": notif,
        "android": {"notification": {"channel_id": channel}},
    }


# --- senders -----------------------------------------------------------------

class ApnsSender:
    """APNs over HTTP/2 with a cached ES256 provider JWT."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        self._jwt: str | None = None
        self._jwt_ts: float = 0.0

    def _provider_jwt(self) -> str:
        now = time.time()
        if self._jwt and now - self._jwt_ts < _JWT_REUSE_S:
            return self._jwt
        import jwt  # PyJWT[crypto]
        self._jwt = jwt.encode(
            {"iss": self.cfg.team_id, "iat": int(now)},
            self.cfg.key_p8, algorithm="ES256",
            headers={"kid": self.cfg.key_id},
        )
        self._jwt_ts = now
        return self._jwt

    async def send(self, device_token: str, payload: dict) -> tuple[bool, bool]:
        """Returns (ok, should_clear_token)."""
        host = "api.push.apple.com" if self.cfg.production else "api.sandbox.push.apple.com"
        url = f"https://{host}/3/device/{device_token}"
        headers = {
            "authorization": f"bearer {self._provider_jwt()}",
            "apns-topic": self.cfg.bundle_id,
            "apns-push-type": "alert",
        }
        r = await self._client.post(url, json=payload, headers=headers)
        if r.status_code == 200:
            return True, False
        reason = ""
        try:
            reason = (r.json() or {}).get("reason", "")
        except Exception:
            pass
        clear = r.status_code == 410 or reason in ("Unregistered", "BadDeviceToken")
        log.info(f"apns send -> {r.status_code} {reason}")
        return False, clear

    async def close(self) -> None:
        await self._client.aclose()


class FcmSender:
    """FCM HTTP v1 with a service-account OAuth token (refreshed by google-auth)."""

    _SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, cfg):
        self.cfg = cfg
        from google.oauth2 import service_account
        self._creds = service_account.Credentials.from_service_account_info(
            cfg.service_account, scopes=[self._SCOPE])
        self._client = httpx.AsyncClient(timeout=10.0)

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request as GRequest
        if not self._creds.valid:
            self._creds.refresh(GRequest())   # sync; called via to_thread
        return self._creds.token

    async def send(self, message: dict) -> tuple[bool, bool]:
        """Returns (ok, should_clear_token)."""
        url = f"https://fcm.googleapis.com/v1/projects/{self.cfg.project_id}/messages:send"
        token = await asyncio.to_thread(self._access_token)
        r = await self._client.post(url, json={"message": message},
                                    headers={"authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return True, False
        status = ""
        try:
            status = ((r.json() or {}).get("error") or {}).get("status", "")
        except Exception:
            pass
        clear = r.status_code == 404 or status in ("UNREGISTERED", "NOT_FOUND")
        log.info(f"fcm send -> {r.status_code} {status}")
        return False, clear

    async def close(self) -> None:
        await self._client.aclose()


# --- offline targeting + dispatch --------------------------------------------

def _offline_targets(state) -> list[tuple[str, str, str]]:
    """(token, service, push_token) for paired devices with a push token whose
    app is NOT currently connected as a satellite. Deduped by push_token: a
    device re-registered under several pairing tokens is notified once — and not
    at all if ANY of its pairing entries is currently connected (app open)."""
    pairing = getattr(state, "pairing", None)
    if pairing is None:
        return []
    connected = getattr(state, "satellite_ws", {}) or {}
    targets = list(pairing.push_targets())
    # A push_token is "open" if any of its pairing tokens is connected.
    open_tokens = {pt for tok, _svc, pt in targets if tok in connected}
    out = []
    seen = set()
    for token, service, push_token in targets:
        if push_token in open_tokens or push_token in seen:
            continue
        seen.add(push_token)
        out.append((token, service, push_token))
    return out


class PushService:
    """Fans speak/timer/image events out to offline paired devices. No-ops when
    no provider is configured."""

    def __init__(self, registry, signing_secret: str, gateway_base: str):
        self._reg = registry
        self._secret = signing_secret or ""
        self._gateway = (gateway_base or "").rstrip("/")
        self._apns: ApnsSender | None = None
        self._fcm: FcmSender | None = None

    def _apns_sender(self) -> ApnsSender | None:
        cfg = self._reg.apns()
        if cfg is None:
            self._apns = None
            return None
        if self._apns is None or self._apns.cfg is not cfg:
            self._apns = ApnsSender(cfg)
        return self._apns

    def _fcm_sender(self) -> FcmSender | None:
        cfg = self._reg.fcm()
        if cfg is None:
            self._fcm = None
            return None
        if self._fcm is None or self._fcm.cfg is not cfg:
            self._fcm = FcmSender(cfg)
        return self._fcm

    def image_url(self, row_id: int | None) -> str | None:
        if row_id is None or not self._gateway or not self._secret:
            return None
        return sign_image_url(self._gateway, self._secret, int(row_id))

    async def dispatch(self, state, kind: str, text: str, *,
                       image_row_id: int | None = None, category: str | None = None) -> None:
        if not self._reg.configured():
            return
        category = category or kind
        image_url = self.image_url(image_row_id)
        targets = _offline_targets(state)
        if not targets:
            return
        # Fire-and-forget: never block the live announce/show path on push I/O.
        asyncio.create_task(self._send_all(state, targets, kind, text, image_url, category))

    async def _send_all(self, state, targets, kind, text, image_url, category) -> None:
        connected = getattr(state, "satellite_ws", {}) or {}
        for token, service, push_token in targets:
            if token in connected:        # reconnected since the snapshot — skip
                continue
            try:
                if service == "apns":
                    sender = self._apns_sender()
                    if sender is None:
                        continue
                    ok, clear = await sender.send(
                        push_token, build_apns_payload(kind, text, image_url, category))
                elif service == "fcm":
                    sender = self._fcm_sender()
                    if sender is None:
                        continue
                    ok, clear = await sender.send(
                        build_fcm_message(push_token, kind, text, image_url, category))
                else:
                    continue
                if clear:
                    state.pairing.clear_push_token(token)
            except Exception as e:
                log.warning(f"push send failed ({service}): {type(e).__name__}: {e}")

    async def close(self) -> None:
        for s in (self._apns, self._fcm):
            if s is not None:
                try:
                    await s.close()
                except Exception:
                    pass
