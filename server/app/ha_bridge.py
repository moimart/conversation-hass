"""Home Assistant bridge for satellite cards.

Satellites can place Lovelace cards on their display (mobile/src/ha). The
cards never see the HA token: they get entity state and relayed subscriptions
from this bridge over their existing /ws/ui socket, and make reads/service
calls through the /api/ha/* routes (routes_ha.py), which land here.

Design (ported from docker-home's server/ha.mjs):
  * ONE dedicated HA WebSocket, owned here with its own reconnect loop. It is
    deliberately separate from state.ha_ws (WebRTC streaming / photo frame), so
    a reconnect here never drops their subscriptions and vice versa.
  * A full state cache (state_changed keeps it current). Each device RETAINS the
    entity ids it has on screen; a state change is sent only to the devices that
    retain that entity.
  * Relayed subscriptions (render_template, forecasts, history streams, …) are
    addressed by a bridge-local id that survives HA reconnects; everything a
    device holds is dropped when its /ws/ui disconnects.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from .ha_ws import HAWSClient

log = logging.getLogger("hal.ha_bridge")

# Read-only WS commands a card may run via hass.callWS (docker-home HA_WS_READ).
HA_WS_READ = re.compile(
    r"^(get_config|get_services|get_states|history/history_during_period|"
    r"recorder/(statistics_during_period|statistic_during_period|get_statistics_metadata|list_statistic_ids)|"
    r"logbook/get_events|"
    r"config/(entity_registry/(list_for_display|get)|device_registry/list|area_registry/list|floor_registry/list|label_registry/list)|"
    r"energy/(get_prefs|info|fossil_energy_consumption)|weather/convertible_units|frontend/get_translations|frontend/get_icons|"
    r"lovelace/(resources|config|dashboards/list)|search/related|calendar/event/list)$"
)
# Subscriptions a card may relay via hass.connection.subscribeMessage.
HA_WS_SUBSCRIBE = re.compile(
    r"^(render_template|weather/subscribe_forecast|history/stream|logbook/event_stream|"
    r"subscribe_entities|energy/solar_forecast)$"
)
_NAME_RE = re.compile(r"^[a-z_]+$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[\w-]+$")

BACKOFF_S = (1, 2, 5, 10, 30)
CACHE_TTL_S = 300.0
MAX_RETAINED = 500
MAX_SUBS_PER_DEVICE = 50

Sender = Callable[[str, dict], Awaitable[bool]]


class HAError(Exception):
    """A request the bridge refuses or HA rejected. `status` maps to HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def slim(s: dict) -> dict:
    return {
        "entity_id": s.get("entity_id"),
        "state": s.get("state"),
        "attributes": s.get("attributes") or {},
        "last_changed": s.get("last_changed"),
        "last_updated": s.get("last_updated"),
    }


def walk_lovelace(cfg: dict) -> list[dict]:
    """Every card in a Lovelace config with a readable path (views, sections,
    and view badges folded into one glance card)."""
    out: list[dict] = []

    def visit(card, where):
        if not isinstance(card, dict):
            return
        out.append({
            "path": where,
            "type": card.get("type") or "unknown",
            "title": card.get("title") or card.get("name") or card.get("entity") or None,
            "card": card,
        })

    for vi, v in enumerate((cfg or {}).get("views") or []):
        if not isinstance(v, dict):
            continue
        view = v.get("title") or v.get("path") or f"View {vi + 1}"
        for ci, c in enumerate(v.get("cards") or []):
            visit(c, f"{view} › {ci + 1}")
        for si, s in enumerate(v.get("sections") or []):
            for ci, c in enumerate((s or {}).get("cards") or []):
                if isinstance(c, dict) and c.get("type") == "heading":
                    continue
                visit(c, f"{view} › section {si + 1} › {ci + 1}")
        badges = v.get("badges") or []
        ents = [b if isinstance(b, str) else (b or {}).get("entity") for b in badges]
        ents = [e for e in ents if e]
        if ents:
            visit({"type": "glance", "title": f"{view} badges", "entities": ents},
                  f"{view} › badges")
    return out


class HABridge:
    def __init__(self, url: str, token: str, send: Sender,
                 device_name: Callable[[str], str | None] | None = None):
        self._url = url
        self._token = token
        self._send = send
        self._device_name = device_name or (lambda _t: None)
        self.client: HAWSClient | None = None
        self.states: dict[str, dict] = {}
        self.connected = False
        self.version: str | None = None
        self._retained: dict[str, set[str]] = {}       # device token -> entity ids
        # bridge sub id -> {"token", "msg", "ha_id"}; ha_id is None while HA is down.
        self._subs: dict[int, dict] = {}
        self._next_sub = 1
        self._cache: dict[str, tuple[float, object]] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ha-bridge")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.client:
            await self.client.disconnect()
        self.connected = False

    def _make_client(self) -> HAWSClient:
        return HAWSClient(self._url, self._token)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            client = self._make_client()
            try:
                await client.connect()
                self.client = client
                await self._on_connected()
                attempt = 0
                await client.wait_closed()
                log.warning("HA bridge: connection closed")
            except asyncio.CancelledError:
                await client.disconnect()
                raise
            except Exception as e:
                log.warning(f"HA bridge: connect failed: {type(e).__name__}: {e}")
            self.connected = False
            for sub in self._subs.values():
                sub["ha_id"] = None
            await client.disconnect()
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _on_connected(self) -> None:
        client = self.client
        assert client is not None
        self._cache.clear()
        await client.subscribe({"type": "subscribe_events", "event_type": "state_changed"},
                               self._on_state_event)
        res = await self._result(client.send_command({"type": "get_states"}, timeout=20.0))
        self.states = {s["entity_id"]: slim(s) for s in (res or []) if isinstance(s, dict)
                       and s.get("entity_id")}
        self.connected = True
        log.info(f"HA bridge connected ({len(self.states)} entities)")
        # Devices may have stale state from before the outage: resend theirs.
        for token, ids in list(self._retained.items()):
            snap = {i: self.states.get(i) for i in ids}
            if snap:
                await self._send(token, {"type": "ha_states", "states": snap})
        for sub_id, sub in list(self._subs.items()):
            try:
                sub["ha_id"] = await client.subscribe(sub["msg"], self._relay(sub_id))
            except Exception as e:
                log.debug(f"HA bridge: resubscribe {sub_id} failed: {e}")

    @staticmethod
    async def _result(coro):
        """Await a send_command and unwrap HA's {success, result}."""
        msg = await coro
        if not msg.get("success", False):
            err = (msg.get("error") or {}).get("message") or "HA request failed"
            raise HAError(err, 502)
        return msg.get("result")

    # --- state fan-out -------------------------------------------------------
    async def _on_state_event(self, event: dict) -> None:
        if event.get("type") == "result":
            return  # subscription ack
        data = event.get("data") or {}
        eid = data.get("entity_id")
        if not eid:
            return
        new = data.get("new_state")
        if new:
            self.states[eid] = slim(new)
        else:
            self.states.pop(eid, None)
        payload = {"type": "ha_state", "states": {eid: self.states.get(eid)}}
        for token, ids in list(self._retained.items()):
            if eid in ids:
                await self._send(token, payload)

    def retain(self, token: str, ids) -> dict:
        """Replace a device's on-screen entity set. Returns their current state
        (None for unknown ids) for the caller to send as ha_states."""
        clean = {i for i in (ids or []) if isinstance(i, str) and _ENTITY_RE.match(i)}
        clean = set(sorted(clean)[:MAX_RETAINED])
        if clean:
            self._retained[token] = clean
        else:
            self._retained.pop(token, None)
        return {i: self.states.get(i) for i in clean}

    # --- relayed subscriptions ----------------------------------------------
    def _relay(self, sub_id: int):
        async def handler(event: dict) -> None:
            if event.get("type") == "result":
                return
            sub = self._subs.get(sub_id)
            if sub is not None:
                await self._send(sub["token"], {"type": "ha_sub", "sub": sub_id, "event": event})
        return handler

    async def subscribe(self, token: str, msg: dict) -> int:
        if not isinstance(msg, dict) or not HA_WS_SUBSCRIBE.match(str(msg.get("type", ""))):
            raise HAError("subscription type not allowed", 403)
        if sum(1 for s in self._subs.values() if s["token"] == token) >= MAX_SUBS_PER_DEVICE:
            raise HAError("too many subscriptions", 429)
        msg = {k: v for k, v in msg.items() if k != "id"}
        sub_id = self._next_sub
        self._next_sub += 1
        self._subs[sub_id] = {"token": token, "msg": msg, "ha_id": None}
        if self.connected and self.client:
            try:
                self._subs[sub_id]["ha_id"] = await self.client.subscribe(msg, self._relay(sub_id))
            except Exception as e:
                self._subs.pop(sub_id, None)
                raise HAError(f"subscribe failed: {e}", 502)
        return sub_id

    async def unsubscribe(self, token: str, sub_id) -> None:
        sub = self._subs.get(sub_id)
        if sub is None or sub["token"] != token:
            return
        self._subs.pop(sub_id, None)
        if sub["ha_id"] is not None and self.client and self.connected:
            await self.client.unsubscribe(sub["ha_id"])

    async def drop_device(self, token: str) -> None:
        """Release everything a device held (its /ws/ui went away)."""
        self._retained.pop(token, None)
        for sub_id in [i for i, s in self._subs.items() if s["token"] == token]:
            await self.unsubscribe(token, sub_id)

    # --- requests ------------------------------------------------------------
    async def send(self, msg: dict, timeout: float = 15.0):
        if not self.connected or not self.client:
            raise HAError("Home Assistant is not connected", 503)
        return await self._result(self.client.send_command(msg, timeout=timeout))

    async def ws(self, msg: dict, token: str | None = None):
        """A card's hass.callWS: read-only allowlist, plus call_service."""
        if not isinstance(msg, dict):
            raise HAError("msg required")
        mtype = str(msg.get("type", ""))
        if mtype == "call_service":
            return await self.call_service(msg.get("domain"), msg.get("service"),
                                           msg.get("service_data"), msg.get("target"),
                                           token=token,
                                           return_response=bool(msg.get("return_response")))
        if not HA_WS_READ.match(mtype):
            raise HAError(f"command not allowed: {mtype}", 403)
        return await self.send({k: v for k, v in msg.items() if k != "id"})

    async def call_service(self, domain, service, data=None, target=None, *,
                           token: str | None = None, return_response: bool = False):
        if not _NAME_RE.match(str(domain or "")) or not _NAME_RE.match(str(service or "")):
            raise HAError("bad domain/service")
        msg: dict = {"type": "call_service", "domain": domain, "service": service,
                     "service_data": data if isinstance(data, dict) else {}}
        if isinstance(target, dict) and target:
            msg["target"] = target
        if return_response:
            msg["return_response"] = True
        who = (self._device_name(token) if token else None) or "?"
        log.info(f"[ha] call_service {domain}.{service} from {who!r} "
                 f"target={msg.get('target') or msg['service_data'].get('entity_id')}")
        return await self.send(msg)

    async def _cached(self, key: str, factory):
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await factory()
        self._cache[key] = (time.monotonic() + CACHE_TTL_S, value)
        return value

    async def bootstrap(self) -> dict:
        async def registry():
            try:
                return await self.send({"type": "config/entity_registry/list_for_display"})
            except HAError:
                return {"entities": []}

        async def resources():
            try:
                return await self.send({"type": "lovelace/resources"})
            except HAError:
                return []

        return {
            "config": await self._cached("config", lambda: self.send({"type": "get_config"})),
            "registry": await self._cached("registry", registry),
            "resources": await self._cached("resources", resources),
        }

    def entities(self, q: str = "", domain: str = "") -> list[dict]:
        q = (q or "").lower()
        out = []
        for s in self.states.values():
            eid = s["entity_id"]
            if domain and not eid.startswith(f"{domain}."):
                continue
            name = (s.get("attributes") or {}).get("friendly_name") or eid
            if q and q not in f"{eid} {name}".lower():
                continue
            out.append({"id": eid, "name": name, "state": s.get("state"),
                        "unit": (s.get("attributes") or {}).get("unit_of_measurement")})
            if len(out) >= 80:
                break
        return sorted(out, key=lambda e: e["id"])

    async def forecast(self, entity_id: str, kind: str = "daily") -> list:
        if not re.match(r"^weather\.[\w-]+$", entity_id or ""):
            raise HAError("weather entity required")
        if kind not in ("daily", "hourly", "twice_daily"):
            kind = "daily"
        r = await self.send({"type": "call_service", "domain": "weather",
                             "service": "get_forecasts", "service_data": {"type": kind},
                             "target": {"entity_id": entity_id}, "return_response": True})
        return (((r or {}).get("response") or {}).get(entity_id) or {}).get("forecast") or []

    async def dashboards(self) -> list[dict]:
        try:
            lst = await self.send({"type": "lovelace/dashboards/list"})
        except HAError:
            lst = []
        return [{"url_path": "", "title": "Overview (default)"}] + [
            {"url_path": d.get("url_path"), "title": d.get("title")}
            for d in (lst or []) if isinstance(d, dict)
        ]

    async def cards(self, dashboard: str | None) -> list[dict]:
        cfg = await self.send({"type": "lovelace/config", "url_path": dashboard or None})
        return walk_lovelace(cfg if isinstance(cfg, dict) else {})
