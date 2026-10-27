"""Satellite HA card + layout routes: token and scope gating, service-call
validation, the proxy path allowlist, static-file traversal rejection, and the
/ws/ui ha_* message handler."""
import json
import sys
from types import SimpleNamespace

import pytest

from server.app import routes_ha
from server.app.ha_bridge import HAError
from server.app.layouts import LayoutStore


class FakeBridge:
    connected = True

    def __init__(self):
        self.calls = []
        self.states = {"light.kitchen": {"entity_id": "light.kitchen", "state": "on"}}

    async def call_service(self, domain, service, data=None, target=None, *, token=None,
                           return_response=False):
        from server.app.ha_bridge import _NAME_RE
        if not _NAME_RE.match(str(domain or "")) or not _NAME_RE.match(str(service or "")):
            raise HAError("bad domain/service")
        self.calls.append((domain, service, data, target, token))

    async def bootstrap(self):
        return {"config": {}, "registry": {"entities": []}, "resources": []}

    def entities(self, q="", domain=""):
        return [{"id": "light.kitchen"}]

    def retain(self, token, ids):
        return {i: self.states.get(i) for i in ids}

    async def subscribe(self, token, msg):
        if msg.get("type") != "render_template":
            raise HAError("subscription type not allowed", 403)
        return 7

    async def unsubscribe(self, token, sub):
        self.calls.append(("unsub", token, sub))


@pytest.fixture
def st(monkeypatch, tmp_path):
    state = SimpleNamespace(
        pairing=SimpleNamespace(
            is_valid_token=lambda t: t in ("full", "watch"),
            scope_allows=lambda t, perm: t == "full",
            public_id=lambda t: f"pid-{t}",
        ),
        ha_bridge=FakeBridge(),
        layouts=LayoutStore(str(tmp_path / "layouts.json")),
    )
    monkeypatch.setitem(sys.modules, "server.app.main",
                        SimpleNamespace(_get_state=lambda app: state))
    return state


def _req(token=None, body=None, query=None, path="/api/ha/x"):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    raw = json.dumps(body).encode() if body is not None else b""

    async def _body():
        return raw

    return SimpleNamespace(app=SimpleNamespace(), headers=headers, query_params=query or {},
                           body=_body, url=SimpleNamespace(path=path, query=""))


def _json(resp):
    return json.loads(resp.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["ha_bootstrap", "ha_entities", "ha_status", "get_layout"])
async def test_token_and_scope_gate(st, handler):
    fn = getattr(routes_ha, handler)
    assert (await fn(_req())).status_code == 401
    assert (await fn(_req("bogus"))).status_code == 401
    assert (await fn(_req("watch"))).status_code == 403          # watch scope denied
    ok = await fn(_req("full"))
    assert not hasattr(ok, "status_code") or ok.status_code == 200


@pytest.mark.asyncio
async def test_service_call_validated_and_attributed(st):
    r = await routes_ha.ha_service(_req("full", {"domain": "light", "service": "toggle",
                                                 "data": {"entity_id": "light.kitchen"}}))
    assert r == {"ok": True}
    assert st.ha_bridge.calls == [("light", "toggle", {"entity_id": "light.kitchen"}, None, "full")]
    bad = await routes_ha.ha_service(_req("full", {"domain": "light", "service": "toggle; x"}))
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_no_bridge_is_503(st):
    st.ha_bridge = None
    assert (await routes_ha.ha_bootstrap(_req("full"))).status_code == 503


@pytest.mark.asyncio
async def test_proxy_path_allowlist_and_query_token(st, monkeypatch):
    fetched = []

    async def fake_rest(method, path, json_body=None):
        fetched.append(path)
        return SimpleNamespace(content=b"jpg", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(routes_ha, "_ha_rest", fake_rest)
    for bad in ("/api/states", "/api/camera_proxy/../config", "/api/services/light/toggle"):
        r = await routes_ha.ha_proxy(_req(query={"token": "full", "path": bad}))
        assert r.status_code == 400
    ok = await routes_ha.ha_proxy(_req(query={"token": "full",
                                             "path": "/api/camera_proxy/camera.door?t=1"}))
    assert ok.status_code == 200 and ok.body == b"jpg"
    assert fetched == ["/api/camera_proxy/camera.door?t=1"]
    # ?token= is only accepted here; a watch token via query is still refused.
    assert (await routes_ha.ha_proxy(_req(query={"token": "watch", "path": "/api/x"}))).status_code == 403


@pytest.mark.parametrize("raw,ok", [
    ("community/mushroom/mushroom.js", True),
    ("house.png", True),
    ("../secrets.yaml", False),
    ("a/../../b", False),
    ("%2e%2e/secrets.yaml", False),
    ("%252e%252e/secrets.yaml", False),
    ("a/%2E%2E/b", False),
    ("a\\..\\b", False),
    ("./x", False),
])
def test_static_path_traversal(raw, ok):
    assert (routes_ha.safe_static_path(raw) is not None) is ok


@pytest.mark.asyncio
async def test_layout_roundtrip(st):
    layout = {"cards": [{"id": "c1", "config": {"type": "tile", "entity": "light.kitchen"}}],
              "portrait": {"items": [{"id": "c1", "x": 0, "y": 0, "w": 2, "h": 2}]},
              "landscape": {"items": []}}
    empty = await routes_ha.get_layout(_req("full"))
    assert empty["version"] == 0 and empty["cards"] == []
    saved = await routes_ha.put_layout(_req("full", layout))
    assert saved["version"] == 1
    assert st.layouts.get("pid-full")["cards"][0]["id"] == "c1"
    bad = await routes_ha.put_layout(_req("full", {"cards": [{"id": "Bad", "config": {}}]}))
    assert bad.status_code == 400
    assert (await routes_ha.put_layout(_req("watch", layout))).status_code == 403
    assert await routes_ha.delete_layout(_req("full")) == {"ok": True}
    assert st.layouts.get("pid-full") is None


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


@pytest.mark.asyncio
async def test_ws_ha_messages(st):
    from server.app.routes_ws import _handle_ha_message
    ws = FakeWS()
    await _handle_ha_message(st, "full", ws, "ha_retain", {"ids": ["light.kitchen"]})
    assert ws.sent[-1]["type"] == "ha_states"
    assert ws.sent[-1]["states"]["light.kitchen"]["state"] == "on"
    await _handle_ha_message(st, "full", ws, "ha_subscribe",
                             {"req": 1, "msg": {"type": "render_template"}})
    assert ws.sent[-1] == {"type": "ha_subscribed", "req": 1, "sub": 7}
    await _handle_ha_message(st, "full", ws, "ha_subscribe",
                             {"req": 2, "msg": {"type": "subscribe_events"}})
    assert ws.sent[-1]["req"] == 2 and "error" in ws.sent[-1]
    await _handle_ha_message(st, "full", ws, "ha_unsubscribe", {"sub": 7})
    assert st.ha_bridge.calls[-1] == ("unsub", "full", 7)
    # A token without the `ha` scope gets nothing.
    ws2 = FakeWS()
    await _handle_ha_message(st, "watch", ws2, "ha_retain", {"ids": ["light.kitchen"]})
    assert ws2.sent == []
