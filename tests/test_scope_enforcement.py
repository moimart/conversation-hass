"""Per-route enforcement of pairing-token scopes (watch tokens).

The gateway edge only validates token VALIDITY (via /api/pair/status), so the
server must enforce scope on every gateway-proxied route. A watch-scoped token
may use /api/command, /api/pair/status and /api/pair/push-register — and
nothing else. These tests pin the deny side at the route level:

  - /api/cloud_llm GET+POST → 403 for a watch bearer (it has edge-valid auth!)
  - /ws/ui → closed with 4403, NOT downgraded to a tokenless mirror (which
    would hand the watch every household broadcast)
  - /api/command → 403 for a valid token with an unknown scope (fail closed);
    watch/full pass (allow side covered in test_pairing's permission matrix)
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def pairing_mgr(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRING_TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("HAL_REQUIRE_TOKEN", raising=False)
    import server.app.pairing as p
    return p.PairingManager()


@pytest.fixture
def tokens(pairing_mgr):
    full = pairing_mgr.redeem(pairing_mgr.create_code()[0], "iPhone")
    watch = pairing_mgr.derive(full, "watch", "Apple Watch")
    return SimpleNamespace(mgr=pairing_mgr, full=full, watch=watch)


def _state(mgr):
    return SimpleNamespace(
        pairing=mgr,
        cloud_llm_enabled=False, cloud_llm_model="", cloud_model_options=[],
        cloud_llm_client=None, mqtt_bridge=None,
        ui_clients=set(), satellite_ws={},
    )


@pytest.fixture
def fake_main(monkeypatch, tokens):
    state = _state(tokens.mgr)
    fake = SimpleNamespace(
        _get_state=lambda app: state,
        broadcast_to_ui=AsyncMock(),
        send_to_device=AsyncMock(),
        _record_user_activity=lambda s: None,
        _dismiss_photo_frame_async=lambda s, reason="": None,
    )
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return state


def _req(bearer=None):
    headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return SimpleNamespace(app=SimpleNamespace(), headers=headers, query_params={})


# --- /api/cloud_llm -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_llm_get_forbids_watch_scope(fake_main, tokens, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_PROVIDERS_PATH", str(tmp_path / "none.json"))
    from server.app.routes_http import get_cloud_llm
    resp = await get_cloud_llm(_req(tokens.watch))
    assert resp.status_code == 403
    ok = await get_cloud_llm(_req(tokens.full))      # full passes
    assert ok["enabled"] is False


@pytest.mark.asyncio
async def test_cloud_llm_post_forbids_watch_scope(fake_main, tokens):
    from server.app.routes_http import post_cloud_llm, CloudLLMRequest
    resp = await post_cloud_llm(_req(tokens.watch), CloudLLMRequest(enabled=True))
    assert resp.status_code == 403
    assert fake_main.cloud_llm_enabled is False      # nothing flipped
    out = await post_cloud_llm(_req(tokens.full), CloudLLMRequest(enabled=True))
    assert fake_main.cloud_llm_enabled is True       # full passes


# --- /ws/ui --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_ui_rejects_watch_scope_with_4403(fake_main, tokens):
    from server.app.routes_ws import ui_endpoint
    ws = SimpleNamespace(
        app=SimpleNamespace(),
        query_params={"token": tokens.watch},
        close=AsyncMock(), accept=AsyncMock(),
    )
    await ui_endpoint(ws)
    ws.close.assert_awaited_once_with(code=4403)
    ws.accept.assert_not_awaited()                   # never becomes a mirror
    assert tokens.watch not in fake_main.satellite_ws


# --- /api/command ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_command_forbids_valid_token_with_unknown_scope(fake_main, tokens):
    """Fail closed: a valid token whose scope grants nothing (e.g. written by a
    newer version) must not command the house."""
    from server.app.routes_http import post_command, CommandRequest
    tokens.mgr._tokens[tokens.watch]["scope"] = "mystery"
    resp = await post_command(_req(tokens.watch), CommandRequest(text="open the door"))
    assert resp.status_code == 403
