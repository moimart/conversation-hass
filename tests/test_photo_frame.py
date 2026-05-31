"""Tests for the photo-frame session lifecycle (server/app/photo_frame.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app import photo_frame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(*, configured_entity: str = "", has_ws: bool = True, has_ha: bool = True,
           video_mode: bool = False, video_hash: str = ""):
    """Build a minimal AppState-ish object the photo_frame module accepts."""
    rt = SimpleNamespace()
    rt.get = lambda key, default=None: {
        "photo_frame_entity": configured_entity,
        "photo_frame_video_mode": video_mode,
    }.get(key, default)

    ws = AsyncMock() if has_ws else None
    if ws:
        ws.send_json = AsyncMock()

    state = SimpleNamespace()
    state.runtime_config = rt
    state.audio_websocket = ws
    state.ui_clients = set()
    state.photo_frame_session = None
    state.photo_frame_video_session = None
    state.photo_frame_video_hash = video_hash

    # Fake HA WS client
    if has_ha:
        ha = SimpleNamespace()
        ha.subscribe = AsyncMock(return_value=42)
        ha.unsubscribe = AsyncMock()
        state.ha_ws = ha
    else:
        state.ha_ws = None
    return state


def _push_types(state):
    return [c.args[0].get("type") for c in state.audio_websocket.send_json.await_args_list]


# ---------------------------------------------------------------------------
# Patch `_fetch_ha_image_entity` (called via `from .main import` inside the
# module) so we don't need fastapi / HA. We also patch the late `_ensure_ha_ws`.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_fetch(monkeypatch):
    """Patch _fetch_ha_image_entity (looked up via late import in photo_frame)."""
    import sys, types
    fake_main = types.ModuleType("server.app.main")

    async def fetch(entity_id):
        if entity_id == "image.missing":
            return None
        return (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")

    fake_main._fetch_ha_image_entity = fetch
    fake_main.broadcast_to_ui = AsyncMock()
    # _ensure_ha_ws returns state.ha_ws if present
    async def ensure_ha_ws(state):
        return getattr(state, "ha_ws", None)
    fake_main._ensure_ha_ws = ensure_ha_ws

    monkeypatch.setitem(sys.modules, "server.app.main", fake_main)
    return fake_main


# ---------------------------------------------------------------------------
# start_photo_frame
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_returns_not_configured_with_empty_config(fake_fetch):
    state = _state(configured_entity="")
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "not_configured", "session": False}
    assert state.photo_frame_session is None
    assert state.ha_ws.subscribe.await_count == 0


@pytest.mark.asyncio
async def test_start_with_configured_entity_succeeds(fake_fetch):
    state = _state(configured_entity="image.weather_radar")
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "ok", "session": True}
    assert state.photo_frame_session is not None
    assert state.photo_frame_session.entity_id == "image.weather_radar"
    assert state.photo_frame_session.sub_id == 42
    # show_photo_frame was pushed to RPi
    assert "show_photo_frame" in _push_types(state)


@pytest.mark.asyncio
async def test_start_with_arg_overrides_config(fake_fetch):
    state = _state(configured_entity="image.default")
    result = await photo_frame.start_photo_frame(state, entity_id="image.override")
    assert result["status"] == "ok"
    assert state.photo_frame_session.entity_id == "image.override"


@pytest.mark.asyncio
async def test_start_rejects_non_image_entity(fake_fetch):
    state = _state(configured_entity="")
    result = await photo_frame.start_photo_frame(state, entity_id="sensor.kitchen")
    assert result == {"status": "invalid_entity", "session": False}
    assert state.photo_frame_session is None


@pytest.mark.asyncio
async def test_start_returns_fetch_failed_on_404(fake_fetch):
    state = _state(configured_entity="image.missing")
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "fetch_failed", "session": False}
    assert state.photo_frame_session is None
    # No subscribe attempted if the fetch already failed
    assert state.ha_ws.subscribe.await_count == 0


@pytest.mark.asyncio
async def test_start_idempotent_same_entity_pushes_update(fake_fetch):
    state = _state(configured_entity="image.weather_radar")
    await photo_frame.start_photo_frame(state)
    first_sub_id = state.photo_frame_session.sub_id
    state.audio_websocket.send_json.reset_mock()

    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "already_active", "session": True}
    # Same session, same subscription
    assert state.photo_frame_session.sub_id == first_sub_id
    # But a fresh update message was pushed (in case the entity rotated)
    assert "photo_frame_update" in _push_types(state)


@pytest.mark.asyncio
async def test_start_switches_entity_closes_previous(fake_fetch):
    state = _state(configured_entity="image.first")
    await photo_frame.start_photo_frame(state)
    state.ha_ws.unsubscribe.reset_mock()
    state.audio_websocket.send_json.reset_mock()

    await photo_frame.start_photo_frame(state, entity_id="image.second")
    # Old subscription torn down
    assert state.ha_ws.unsubscribe.await_count == 1
    # And a new session opened
    assert state.photo_frame_session.entity_id == "image.second"


@pytest.mark.asyncio
async def test_start_degrades_when_ha_unavailable(fake_fetch):
    """No HA WS → still show the photo, just no auto-updates."""
    state = _state(configured_entity="image.weather_radar", has_ha=False)
    result = await photo_frame.start_photo_frame(state)
    assert result["status"] == "ok"
    assert state.photo_frame_session.sub_id is None
    assert "show_photo_frame" in _push_types(state)


# ---------------------------------------------------------------------------
# stop_photo_frame
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_unsubscribes_and_pushes_hide(fake_fetch):
    state = _state(configured_entity="image.weather_radar")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()

    result = await photo_frame.stop_photo_frame(state, reason="explicit")
    assert result == {"status": "ok", "session": False}
    assert state.photo_frame_session is None
    assert state.ha_ws.unsubscribe.await_count == 1
    assert "hide_photo_frame" in _push_types(state)


@pytest.mark.asyncio
async def test_stop_without_session_is_noop(fake_fetch):
    state = _state(configured_entity="image.x")
    result = await photo_frame.stop_photo_frame(state)
    assert result == {"status": "not_active", "session": False}


# ---------------------------------------------------------------------------
# _on_state_changed (image rotation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_pushes_when_hash_changes(fake_fetch, monkeypatch):
    state = _state(configured_entity="image.weather_radar")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()

    # Make the next fetch return DIFFERENT bytes (different hash).
    async def fetch_v2(entity_id):
        return (b"\xff\xd8\xff\xe0DIFFERENT", "image/jpeg")
    monkeypatch.setattr(fake_fetch, "_fetch_ha_image_entity", fetch_v2)

    await photo_frame._on_state_changed(state, "image.weather_radar")
    assert "photo_frame_update" in _push_types(state)


@pytest.mark.asyncio
async def test_update_skips_when_hash_unchanged(fake_fetch):
    state = _state(configured_entity="image.weather_radar")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()

    # Same fetch returns same bytes → hash matches session.last_hash → noop.
    await photo_frame._on_state_changed(state, "image.weather_radar")
    assert state.audio_websocket.send_json.await_count == 0


@pytest.mark.asyncio
async def test_update_ignores_unknown_entity(fake_fetch):
    state = _state(configured_entity="image.weather_radar")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()

    await photo_frame._on_state_changed(state, "image.something_else")
    assert state.audio_websocket.send_json.await_count == 0


# ---------------------------------------------------------------------------
# Looping-video branch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_video_mode_on_with_cache_loops_video(fake_fetch):
    """Mode ON + a cached video → push sync then show_photo_frame_video,
    create a video session, and do NOT fetch HA or subscribe."""
    fetch_calls = []
    async def spy_fetch(entity_id):
        fetch_calls.append(entity_id)
        return (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
    fake_fetch._fetch_ha_image_entity = spy_fetch

    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="abc123")
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "ok", "session": True, "mode": "video"}
    assert state.photo_frame_video_session is not None
    assert state.photo_frame_video_session.video_hash == "abc123"
    assert state.photo_frame_session is None
    types = _push_types(state)
    assert "photo_frame_video_sync" in types
    assert "show_photo_frame_video" in types
    # No HA work for the video path.
    assert fetch_calls == []
    assert state.ha_ws.subscribe.await_count == 0


@pytest.mark.asyncio
async def test_video_mode_on_without_cache_falls_back_to_photos(fake_fetch):
    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="")  # no cache
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "ok", "session": True}
    assert state.photo_frame_video_session is None
    assert state.photo_frame_session is not None
    assert "show_photo_frame" in _push_types(state)


@pytest.mark.asyncio
async def test_video_mode_off_with_cache_shows_photos(fake_fetch):
    state = _state(configured_entity="image.weather_radar",
                   video_mode=False, video_hash="abc123")
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "ok", "session": True}
    assert state.photo_frame_video_session is None
    assert "show_photo_frame" in _push_types(state)


@pytest.mark.asyncio
async def test_video_same_hash_is_idempotent(fake_fetch):
    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="abc123")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()
    result = await photo_frame.start_photo_frame(state)
    assert result == {"status": "already_active", "session": True, "mode": "video"}
    # No re-push for the same loop.
    assert state.audio_websocket.send_json.await_count == 0


@pytest.mark.asyncio
async def test_video_falls_back_when_no_ws(fake_fetch):
    """No audio WS → can't sync the file → fall through to the photo path."""
    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="abc123", has_ws=False)
    result = await photo_frame.start_photo_frame(state)
    assert result["status"] == "ok"
    assert state.photo_frame_video_session is None
    assert state.photo_frame_session is not None


@pytest.mark.asyncio
async def test_stop_clears_video_session_without_ha(fake_fetch):
    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="abc123")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()
    state.ha_ws.unsubscribe.reset_mock()

    result = await photo_frame.stop_photo_frame(state, reason="explicit")
    assert result == {"status": "ok", "session": False}
    assert state.photo_frame_video_session is None
    assert "hide_photo_frame" in _push_types(state)
    # A video session has no HA subscription.
    assert state.ha_ws.unsubscribe.await_count == 0


@pytest.mark.asyncio
async def test_handle_video_load_error_falls_back_to_photos(fake_fetch):
    state = _state(configured_entity="image.weather_radar",
                   video_mode=True, video_hash="abc123")
    await photo_frame.start_photo_frame(state)
    state.audio_websocket.send_json.reset_mock()

    result = await photo_frame.handle_video_load_error(state)
    # Fell back to the photo path despite video mode still being on.
    assert result["status"] == "ok"
    assert state.photo_frame_video_session is None
    assert state.photo_frame_session is not None
    assert "show_photo_frame" in _push_types(state)


# ---------------------------------------------------------------------------
# MQTT bridge: photo-frame discovery payload
# ---------------------------------------------------------------------------

def test_discovery_contains_photo_frame_entities():
    from server.app.mqtt_bridge import MQTTBridge
    bridge = MQTTBridge(host="x", device_id="hal-default")
    payloads = bridge._discovery_payloads()
    topics = [t for t, _ in payloads]
    assert any("photo_frame_show/config" in t for t in topics)
    assert any("photo_frame_hide/config" in t for t in topics)
    assert any("config_photo_frame_entity/config" in t for t in topics)
    # Looping-video config entities: a text URL field + an on/off switch.
    assert any("config_photo_frame_video_url/config" in t for t in topics)
    assert any("config_photo_frame_video_mode/config" in t for t in topics)
    # Text entity has no `max` field (the HA 255-cap rule we learned the hard way).
    for topic, body in payloads:
        if "config_photo_frame_entity/config" in topic:
            assert "max" not in body
        if "config_photo_frame_video_mode/config" in topic:
            assert "switch/" in topic
            assert body.get("payload_on") == "ON"
