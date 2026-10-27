"""HA bridge for satellite cards: state fan-out only to devices that retain an
entity, relayed subscriptions (allowlisted, per-device, re-subscribed after an
HA reconnect, dropped on disconnect), and the read-only callWS allowlist."""
import asyncio

import pytest

from server.app import ha_bridge as hb
from server.app.ha_bridge import HABridge, HAError, walk_lovelace


class FakeClient:
    """Stands in for HAWSClient: records commands/subscriptions, lets the test
    fire events and drop the connection."""

    def __init__(self, states=None):
        self.states = states or []
        self.commands = []
        self.subs = {}           # ha id -> (payload, handler)
        self.next_id = 100
        self.unsubscribed = []
        self.closed = asyncio.Event()

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed.set()

    async def wait_closed(self):
        await self.closed.wait()

    async def subscribe(self, payload, handler):
        self.next_id += 1
        self.subs[self.next_id] = (payload, handler)
        return self.next_id

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)
        self.subs.pop(sub_id, None)

    async def send_command(self, payload, timeout=10.0):
        self.commands.append(payload)
        if payload["type"] == "get_states":
            return {"success": True, "result": self.states}
        if payload["type"] == "lovelace/config":
            return {"success": True, "result": {"views": [{"title": "Home", "cards": [
                {"type": "tile", "entity": "light.kitchen"}]}]}}
        return {"success": True, "result": {"echo": payload}}

    async def fire_state(self, eid, state):
        for payload, handler in list(self.subs.values()):
            if payload.get("event_type") == "state_changed":
                new = {"entity_id": eid, "state": state, "attributes": {}} if state else None
                await handler({"event_type": "state_changed",
                               "data": {"entity_id": eid, "new_state": new}})


async def _bridge(monkeypatch, clients):
    sent = []

    async def send(token, msg):
        sent.append((token, msg))
        return True

    b = HABridge("http://ha:8123", "secret", send, device_name=lambda t: f"dev-{t}")
    it = iter(clients)
    monkeypatch.setattr(b, "_make_client", lambda: next(it))
    monkeypatch.setattr(hb, "BACKOFF_S", (0,))
    b.start()
    for _ in range(50):
        if b.connected:
            break
        await asyncio.sleep(0.01)
    assert b.connected
    return b, sent


LIGHT = {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen"}}
TEMP = {"entity_id": "sensor.temp", "state": "21", "attributes": {"unit_of_measurement": "°C"}}


@pytest.mark.asyncio
async def test_state_fanout_only_to_retaining_devices(monkeypatch):
    c = FakeClient([LIGHT, TEMP])
    b, sent = await _bridge(monkeypatch, [c])
    snap = b.retain("A", ["light.kitchen", "not an id", "sensor.missing"])
    assert snap["light.kitchen"]["state"] == "off"
    assert snap["sensor.missing"] is None and "not an id" not in snap
    b.retain("B", ["sensor.temp"])
    sent.clear()
    await c.fire_state("light.kitchen", "on")
    assert [(t, m["states"]["light.kitchen"]["state"]) for t, m in sent] == [("A", "on")]
    sent.clear()
    await c.fire_state("light.kitchen", None)          # entity removed
    assert sent == [("A", {"type": "ha_state", "states": {"light.kitchen": None}})]
    assert "light.kitchen" not in b.states
    await b.stop()


@pytest.mark.asyncio
async def test_subscribe_allowlist_and_relay(monkeypatch):
    c = FakeClient()
    b, sent = await _bridge(monkeypatch, [c])
    with pytest.raises(HAError) as e:
        await b.subscribe("A", {"type": "subscribe_events"})      # not allowlisted
    assert e.value.status == 403
    sub = await b.subscribe("A", {"type": "render_template", "template": "{{ 1 }}", "id": 9})
    ha_id = next(i for i, (p, _) in c.subs.items() if p["type"] == "render_template")
    assert "id" not in c.subs[ha_id][0]                # client id stripped
    sent.clear()
    await c.subs[ha_id][1]({"type": "result", "success": True})   # ack is not relayed
    await c.subs[ha_id][1]({"result": "1"})
    assert sent == [("A", {"type": "ha_sub", "sub": sub, "event": {"result": "1"}})]
    # Another device can't unsubscribe it.
    await b.unsubscribe("B", sub)
    assert ha_id in c.subs
    await b.unsubscribe("A", sub)
    assert c.unsubscribed == [ha_id]
    await b.stop()


@pytest.mark.asyncio
async def test_drop_device_releases_everything(monkeypatch):
    c = FakeClient([LIGHT])
    b, sent = await _bridge(monkeypatch, [c])
    b.retain("A", ["light.kitchen"])
    await b.subscribe("A", {"type": "render_template", "template": "x"})
    await b.subscribe("B", {"type": "render_template", "template": "y"})
    await b.drop_device("A")
    assert "A" not in b._retained
    assert [s["token"] for s in b._subs.values()] == ["B"]
    sent.clear()
    await c.fire_state("light.kitchen", "on")
    assert sent == []
    await b.stop()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_and_resends_state(monkeypatch):
    c1, c2 = FakeClient([LIGHT]), FakeClient([{**LIGHT, "state": "on"}])
    b, sent = await _bridge(monkeypatch, [c1, c2])
    b.retain("A", ["light.kitchen"])
    sub = await b.subscribe("A", {"type": "render_template", "template": "x"})
    sent.clear()
    c1.closed.set()                                   # HA went away
    for _ in range(50):
        if b.client is c2 and b.connected:
            break
        await asyncio.sleep(0.01)
    assert b.client is c2
    # Retained state re-sent with the fresh value; the relay lives on under the
    # same bridge id, re-subscribed on the new connection.
    assert ("A", {"type": "ha_states", "states": {"light.kitchen": b.states["light.kitchen"]}}) in sent
    assert b.states["light.kitchen"]["state"] == "on"
    assert any(p["type"] == "render_template" for p, _ in c2.subs.values())
    assert b._subs[sub]["ha_id"] in c2.subs
    await b.stop()


@pytest.mark.asyncio
async def test_ws_allowlist(monkeypatch):
    c = FakeClient()
    b, _ = await _bridge(monkeypatch, [c])
    assert await b.ws({"type": "get_config", "id": 5}) == {"echo": {"type": "get_config"}}
    # Integration icon translations (how HA picks e.g. a bus icon for a sensor).
    assert await b.ws({"type": "frontend/get_icons", "category": "entity", "integration": "x"})
    for bad in ("config/entity_registry/update", "auth/delete_refresh_token",
                "execute_script", "get_configx"):
        with pytest.raises(HAError) as e:
            await b.ws({"type": bad})
        assert e.value.status == 403
    # call_service is allowed through ws, validated like /api/ha/service.
    await b.ws({"type": "call_service", "domain": "light", "service": "toggle",
                "service_data": {"entity_id": "light.kitchen"}}, token="A")
    assert c.commands[-1]["domain"] == "light"
    with pytest.raises(HAError):
        await b.call_service("light; rm", "toggle")
    await b.stop()


@pytest.mark.asyncio
async def test_send_refuses_while_disconnected():
    async def send(_t, _m):
        return True
    b = HABridge("http://ha", "t", send)
    with pytest.raises(HAError) as e:
        await b.send({"type": "get_config"})
    assert e.value.status == 503


def test_entities_search_and_cap():
    async def send(_t, _m):
        return True
    b = HABridge("http://ha", "t", send)
    b.states = {f"light.l{i:03d}": {"entity_id": f"light.l{i:03d}", "state": "on",
                                    "attributes": {}} for i in range(100)}
    b.states["sensor.temp"] = TEMP
    assert len(b.entities()) == 80
    assert [e["id"] for e in b.entities("temp")] == ["sensor.temp"]
    assert b.entities(domain="sensor")[0]["unit"] == "°C"


def test_walk_lovelace():
    cards = walk_lovelace({"views": [
        {"title": "Home", "cards": [{"type": "tile", "entity": "light.a"}],
         "badges": ["sensor.x", {"entity": "sensor.y"}]},
        {"path": "p2", "sections": [{"cards": [{"type": "heading"},
                                                {"type": "entities", "title": "Lights"}]}]},
    ]})
    assert [(c["path"], c["type"], c["title"]) for c in cards] == [
        ("Home › 1", "tile", "light.a"),
        ("Home › badges", "glance", "Home badges"),
        ("p2 › section 1 › 2", "entities", "Lights"),
    ]
    assert cards[1]["card"]["entities"] == ["sensor.x", "sensor.y"]
