"""Weather-under-the-clock: the parse + message builders + gating + dedupe are
pure/network-free, so we pin them here. The HA read and the broadcast are the
only side effects (verified live)."""

import json
from types import SimpleNamespace

import pytest

from server.app import weather


# ---- pure builders --------------------------------------------------------

def test_build_weather_msg():
    msg = weather.build_weather_msg("partlycloudy", 21.4, "°C")
    assert msg == {
        "type": "weather_update", "show": True,
        "temp": 21.4, "unit": "°C", "condition": "partlycloudy",
    }


def test_build_weather_msg_unit_fallback():
    assert weather.build_weather_msg("sunny", 70, "")["unit"] == "°"


def test_hide_msg():
    assert weather._hide_msg() == {"type": "weather_update", "show": False}


# ---- parse_weather_response ----------------------------------------------

def test_parse_valid():
    raw = json.dumps({"data": {"state": "sunny",
                               "attributes": {"temperature": 23, "temperature_unit": "°C"}}})
    assert weather.parse_weather_response(raw) == {
        "type": "weather_update", "show": True,
        "temp": 23, "unit": "°C", "condition": "sunny",
    }


def test_parse_markdown_wrapped():
    raw = "Here you go:\n```json\n" + json.dumps(
        {"data": {"state": "rainy", "attributes": {"temperature": 12, "temperature_unit": "°F"}}}
    ) + "\n```"
    msg = weather.parse_weather_response(raw)
    assert msg["condition"] == "rainy" and msg["unit"] == "°F" and msg["temp"] == 12


def test_parse_unwrapped_entity():
    # Some responses aren't wrapped in {"data": ...}; accept the entity directly.
    raw = json.dumps({"state": "cloudy", "attributes": {"temperature": 9}})
    msg = weather.parse_weather_response(raw)
    assert msg["condition"] == "cloudy" and msg["unit"] == "°"  # no unit → fallback


def test_parse_missing_temp_is_none():
    raw = json.dumps({"data": {"state": "sunny", "attributes": {}}})
    assert weather.parse_weather_response(raw) is None


def test_parse_missing_condition_is_none():
    raw = json.dumps({"data": {"state": "", "attributes": {"temperature": 20}}})
    assert weather.parse_weather_response(raw) is None


def test_parse_garbage_is_none():
    assert weather.parse_weather_response("not json at all") is None
    assert weather.parse_weather_response("") is None


# ---- gating ---------------------------------------------------------------

def _rc(**vals):
    return SimpleNamespace(get=lambda k, d=None: vals.get(k, d))


def test_is_enabled():
    assert weather._is_enabled(SimpleNamespace(
        runtime_config=_rc(weather_enabled=True, weather_entity="weather.home"))) is True
    # toggle off
    assert weather._is_enabled(SimpleNamespace(
        runtime_config=_rc(weather_enabled=False, weather_entity="weather.home"))) is False
    # no entity
    assert weather._is_enabled(SimpleNamespace(
        runtime_config=_rc(weather_enabled=True, weather_entity=""))) is False
    # no runtime config
    assert weather._is_enabled(SimpleNamespace(runtime_config=None)) is False


# ---- refresh_now: push-on-change + dedupe --------------------------------

class _FakeMcp:
    def __init__(self, payload):
        self.tool_names = ["ha_get_state"]
        self._payload = payload
        self.calls = 0

    async def call_tool(self, name, args):
        self.calls += 1
        return self._payload


def _state(payload, *, enabled=True, entity="weather.home"):
    return SimpleNamespace(
        runtime_config=_rc(weather_enabled=enabled, weather_entity=entity),
        mcp_client=_FakeMcp(payload),
        current_weather=None,
    )


@pytest.mark.asyncio
async def test_refresh_pushes_then_dedupes(monkeypatch):
    pushed = []

    async def _fake_broadcast(state, msg, **kw):
        pushed.append(msg)

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr("server.app.main.broadcast_force_action", _fake_broadcast)
    monkeypatch.setattr("server.app.main._push_to_rpi", _noop)
    payload = json.dumps({"data": {"state": "sunny",
                                   "attributes": {"temperature": 25, "temperature_unit": "°C"}}})
    state = _state(payload)

    await weather.refresh_now(state)
    assert len(pushed) == 1
    assert state.current_weather["condition"] == "sunny"
    # Same weather again → no second push (dedupe).
    await weather.refresh_now(state)
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_refresh_hides_when_disabled(monkeypatch):
    pushed = []

    async def _fake_broadcast(state, msg, **kw):
        pushed.append(msg)

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr("server.app.main.broadcast_force_action", _fake_broadcast)
    monkeypatch.setattr("server.app.main._push_to_rpi", _noop)
    state = _state("ignored", enabled=False)
    await weather.refresh_now(state)
    assert pushed == [{"type": "weather_update", "show": False}]
    assert state.current_weather == {"type": "weather_update", "show": False}
