"""Tests for the mobile device-pairing module (server/app/pairing.py).

Covers the PairingManager (code mint/redeem/single-use/throttle/token
persistence), the auth helpers (require_token_enabled, extract_bearer), and the
route handlers (request/redeem/status) driven with a fake state + a fake
`server.app.main` module (same pattern as test_photo_frame.py).
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def pmod(monkeypatch, tmp_path):
    # Point token persistence at a temp file before importing/using the manager.
    monkeypatch.setenv("PAIRING_TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("HAL_REQUIRE_TOKEN", raising=False)
    import server.app.pairing as p
    return p


# --- PairingManager core ----------------------------------------------------

def test_create_code_is_six_digits(pmod):
    mgr = pmod.PairingManager()
    code, ttl = mgr.create_code()
    assert len(code) == 6 and code.isdigit()
    assert ttl == int(pmod.CODE_TTL_S)


def test_redeem_valid_code_issues_token(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    token = mgr.redeem(code, "Moises iPhone")
    assert token and mgr.is_valid_token(token)


def test_redeem_is_single_use(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    assert mgr.redeem(code, "x") is not None
    # second redeem of the same code fails (consumed)
    assert mgr.redeem(code, "x") is None


def test_redeem_invalid_code_returns_none(pmod):
    mgr = pmod.PairingManager()
    assert mgr.redeem("000000", "x") is None
    assert mgr.is_valid_token("nope") is False


def test_tokens_persist_across_instances(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    token = mgr.redeem(code, "phone")
    # a fresh manager loads the persisted token file
    mgr2 = pmod.PairingManager()
    assert mgr2.is_valid_token(token)


def test_revoke_token(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    token = mgr.redeem(code, "phone")
    assert mgr.revoke(token) is True
    assert mgr.is_valid_token(token) is False
    assert mgr.revoke(token) is False


def test_throttle_blocks_after_max_attempts(pmod):
    mgr = pmod.PairingManager()
    # first MAX_REDEEM_ATTEMPTS are allowed, then throttled
    for _ in range(pmod.MAX_REDEEM_ATTEMPTS):
        assert mgr.throttled() is False
    assert mgr.throttled() is True


# --- helpers ----------------------------------------------------------------

def test_require_token_enabled_env(monkeypatch, pmod):
    monkeypatch.setenv("HAL_REQUIRE_TOKEN", "1")
    assert pmod.require_token_enabled() is True
    monkeypatch.setenv("HAL_REQUIRE_TOKEN", "off")
    assert pmod.require_token_enabled() is False
    monkeypatch.delenv("HAL_REQUIRE_TOKEN", raising=False)
    assert pmod.require_token_enabled() is False


def test_extract_bearer(pmod):
    req = SimpleNamespace(headers={"authorization": "Bearer abc123"})
    assert pmod.extract_bearer(req) == "abc123"
    assert pmod.extract_bearer(SimpleNamespace(headers={})) is None
    assert pmod.extract_bearer(SimpleNamespace(headers={"authorization": "Basic x"})) is None


# --- route handlers ---------------------------------------------------------

def _fake_state():
    state = SimpleNamespace()
    state.pairing = None  # set by caller
    state.audio_websocket = None
    state.ui_clients = set()
    return state


@pytest.fixture
def fake_main(monkeypatch, pmod):
    """Install a fake server.app.main so pairing's late imports resolve to a
    controllable _get_state + broadcast_to_ui."""
    state = _fake_state()
    state.pairing = pmod.PairingManager()
    fake = SimpleNamespace(
        _get_state=lambda app: state,
        broadcast_to_ui=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return SimpleNamespace(state=state, broadcast=fake.broadcast_to_ui)


@pytest.mark.asyncio
async def test_pair_request_returns_code_and_broadcasts(pmod, fake_main):
    req = SimpleNamespace(app=SimpleNamespace())
    out = await pmod.pair_request(req)
    assert len(out["code"]) == 6 and out["expires_in"] == int(pmod.CODE_TTL_S)
    # show_pairing_code was broadcast to the display
    types = [c.args[1]["type"] for c in fake_main.broadcast.await_args_list]
    assert "show_pairing_code" in types


@pytest.mark.asyncio
async def test_pair_redeem_roundtrip(pmod, fake_main):
    req = SimpleNamespace(app=SimpleNamespace())
    code, _ = fake_main.state.pairing.create_code()
    out = await pmod.pair_redeem(req, pmod.RedeemRequest(code=code, device_name="iPhone"))
    assert "token" in out and fake_main.state.pairing.is_valid_token(out["token"])
    # gateway_url is always present (empty when HAL_GATEWAY_URL is unset) so the
    # app can store it for away-from-home failover.
    assert "gateway_url" in out


@pytest.mark.asyncio
async def test_pair_redeem_includes_gateway_url(pmod, fake_main, monkeypatch):
    monkeypatch.setenv("HAL_GATEWAY_URL", "https://pal.example.com")
    code, _ = fake_main.state.pairing.create_code()
    out = await pmod.pair_redeem(SimpleNamespace(app=SimpleNamespace()),
                                 pmod.RedeemRequest(code=code, device_name="iPhone"))
    assert out["gateway_url"] == "https://pal.example.com"


@pytest.mark.asyncio
async def test_pair_redeem_invalid_is_400(pmod, fake_main):
    req = SimpleNamespace(app=SimpleNamespace())
    resp = await pmod.pair_redeem(req, pmod.RedeemRequest(code="999999"))
    assert resp.status_code == 400
    assert json.loads(resp.body)["error"] == "invalid_or_expired"


@pytest.mark.asyncio
async def test_pair_status_valid_and_invalid(pmod, fake_main):
    code, _ = fake_main.state.pairing.create_code()
    token = fake_main.state.pairing.redeem(code, "x")
    ok = await pmod.pair_status(SimpleNamespace(app=SimpleNamespace(),
                                                headers={"authorization": f"Bearer {token}"}))
    assert ok == {"valid": True}
    bad = await pmod.pair_status(SimpleNamespace(app=SimpleNamespace(), headers={}))
    assert bad.status_code == 401
