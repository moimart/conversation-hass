"""Voice timers: TimerManager lifecycle, routing, HA mirroring, MQTT sensor.

The manager lazily imports routing/announce helpers from server.app.main, so a
fake module is injected into sys.modules (same pattern as the conversation-log
tests) — no real server boots here. Time-sensitive tests use 1-2s timers.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import server.app.timers as timers_mod
from server.app.timers import TimerManager, parse_duration_seconds, spoken_duration, _hms


@pytest.fixture
def fake_main(monkeypatch):
    fake = SimpleNamespace(
        send_to_device=AsyncMock(return_value=True),
        _push_to_rpi=AsyncMock(return_value=True),
        broadcast_to_ui=AsyncMock(),
        announce_everywhere=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return fake


def _state(**kw):
    return SimpleNamespace(
        timer_name_template="Timer {n}",
        timer_announce_template="{name} is ready.",
        mqtt_bridge=SimpleNamespace(publish_active_timers=AsyncMock()),
        mcp_client=None,
        **kw,
    )


# --- helpers ---------------------------------------------------------------

def test_parse_duration():
    assert parse_duration_seconds("set a timer for 5 minutes") == 300
    assert parse_duration_seconds("1 hour 10 minutes") == 4200
    assert parse_duration_seconds("90 seconds") == 90
    assert parse_duration_seconds("2 hours") == 7200
    assert parse_duration_seconds("half an hour") == 1800
    assert parse_duration_seconds("1 minute and 30 seconds") == 90
    assert parse_duration_seconds("set a timer") is None
    assert parse_duration_seconds("what time is it") is None


def test_spoken_duration():
    assert spoken_duration(300) == "5 minutes"
    assert spoken_duration(90) == "1 minute and 30 seconds"
    assert spoken_duration(3600) == "1 hour"
    assert spoken_duration(4200) == "1 hour and 10 minutes"
    assert spoken_duration(1) == "1 second"


def test_hms():
    assert _hms(90) == "0:01:30"
    assert _hms(4200) == "1:10:00"


# --- naming + pool -----------------------------------------------------------

@pytest.mark.asyncio
async def test_sequential_names_and_reset(fake_main):
    mgr = TimerManager(_state())
    t1 = await mgr.create(60, None)
    t2 = await mgr.create(60, None)
    assert (t1.name, t2.name) == ("Timer 1", "Timer 2")
    await mgr.cancel_all()
    assert mgr.count() == 0
    t3 = await mgr.create(60, None)   # numbering restarts when nothing active
    assert t3.name == "Timer 1"
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_name_template_and_fallback(fake_main):
    st = _state()
    st.timer_name_template = "Temporizador {n}"
    mgr = TimerManager(st)
    t = await mgr.create(60, None)
    assert t.name == "Temporizador 1"
    await mgr.cancel_all()
    st.timer_name_template = "{bogus}"      # bad template -> safe fallback
    t = await mgr.create(60, None)
    assert t.name == "Timer 1"
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_pool_allocation_exhaustion_and_reuse(fake_main):
    mgr = TimerManager(_state())
    ts = [await mgr.create(60, None) for _ in range(6)]
    assert [t.pool_entity for t in ts[:5]] == [f"timer.pal_timer_{i}" for i in range(1, 6)]
    assert ts[5].pool_entity is None        # pool exhausted -> tracked, unmirrored
    await mgr.cancel(ts[1].id)              # frees slot 2
    t7 = await mgr.create(60, None)
    assert t7.pool_entity == "timer.pal_timer_2"
    await mgr.cancel_all()


# --- countdown routing --------------------------------------------------------

@pytest.mark.asyncio
async def test_countdown_routes_to_originating_phone_only(fake_main):
    mgr = TimerManager(_state())
    t = await mgr.create(5, "tok-phone")    # <=10s -> countdown arms immediately
    await asyncio.sleep(0.05)
    fake_main.send_to_device.assert_awaited()
    args = fake_main.send_to_device.await_args.args
    assert args[1] == "tok-phone"
    assert args[2]["type"] == "timer_countdown"
    assert args[2]["name"] == "Timer 1"
    assert args[2]["timer_id"] == t.id
    fake_main._push_to_rpi.assert_not_awaited()   # kiosk untouched
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_countdown_routes_to_kiosk_when_no_origin(fake_main):
    mgr = TimerManager(_state())
    await mgr.create(5, None)
    await asyncio.sleep(0.05)
    fake_main._push_to_rpi.assert_awaited()
    fake_main.broadcast_to_ui.assert_awaited()
    fake_main.send_to_device.assert_not_awaited()
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_soonest_wins_then_rearm(fake_main):
    """Two same-device timers inside the window: only the soonest owns the
    countdown; when it fires the next one takes over."""
    mgr = TimerManager(_state())
    t1 = await mgr.create(1, None)
    t2 = await mgr.create(2, None)
    await asyncio.sleep(0.1)
    pushed = [c.args[1]["type"] == "timer_countdown" and c.args[1]["timer_id"]
              for c in fake_main._push_to_rpi.await_args_list
              if c.args[1]["type"] == "timer_countdown"]
    assert pushed == [t1.id]                # t2 deferred while t1 is sooner
    await asyncio.sleep(1.2)                # t1 fires -> t2 re-arms
    pushed = [c.args[1]["timer_id"]
              for c in fake_main._push_to_rpi.await_args_list
              if c.args[1]["type"] == "timer_countdown"]
    assert pushed == [t1.id, t2.id]
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_cross_device_timers_both_arm(fake_main):
    mgr = TimerManager(_state())
    await mgr.create(5, "tok-a")
    await mgr.create(5, "tok-b")
    await asyncio.sleep(0.05)
    tokens = [c.args[1] for c in fake_main.send_to_device.await_args_list
              if c.args[2]["type"] == "timer_countdown"]
    assert sorted(tokens) == ["tok-a", "tok-b"]
    await mgr.cancel_all()


# --- fire + cancel -------------------------------------------------------------

@pytest.mark.asyncio
async def test_fire_announces_everywhere_with_template(fake_main):
    st = _state()
    st.timer_announce_template = "¡{name} ha terminado!"
    mgr = TimerManager(st)
    await mgr.create(1, None)
    await asyncio.sleep(1.3)
    fake_main.announce_everywhere.assert_awaited_once()
    args, kwargs = fake_main.announce_everywhere.await_args
    assert args[1] == "¡Timer 1 ha terminado!"
    assert kwargs.get("log_source") == "timer"
    assert mgr.count() == 0


@pytest.mark.asyncio
async def test_cancel_pushes_countdown_cancel_when_armed(fake_main):
    mgr = TimerManager(_state())
    t = await mgr.create(5, "tok-phone")
    await asyncio.sleep(0.05)               # let it arm
    assert t.countdown_armed
    await mgr.cancel(t.id)
    types = [c.args[2]["type"] for c in fake_main.send_to_device.await_args_list]
    assert "timer_countdown_cancel" in types
    fake_main.announce_everywhere.assert_not_awaited()
    assert mgr.count() == 0


@pytest.mark.asyncio
async def test_cancel_by_name_and_number(fake_main):
    mgr = TimerManager(_state())
    await mgr.create(60, None)
    t2 = await mgr.create(60, None)
    assert await mgr.cancel_by_name("timer 2") is True
    assert t2.id not in [t.id for t in mgr.list_active()]
    assert await mgr.cancel_by_name("Timer 1") is True      # case-insensitive name
    assert await mgr.cancel_by_name("Timer 9") is False
    assert mgr.count() == 0


# --- HA mirror + sensor ---------------------------------------------------------

@pytest.mark.asyncio
async def test_ha_mirror_start_and_cancel(fake_main):
    st = _state()
    st.mcp_client = SimpleNamespace(
        tool_names=["ha_call_service"],
        call_tool=AsyncMock(return_value="ok"),
    )
    mgr = TimerManager(st)
    t = await mgr.create(90, None)
    await asyncio.sleep(0.05)               # mirror runs as a background task
    call = st.mcp_client.call_tool.await_args_list[0]
    assert call.args[0] == "ha_call_service"
    assert call.args[1]["service"] == "start"
    assert call.args[1]["entity_id"] == "timer.pal_timer_1"
    assert call.args[1]["data"]["duration"] == "0:01:30"
    await mgr.cancel(t.id)
    await asyncio.sleep(0.05)
    services = [c.args[1]["service"] for c in st.mcp_client.call_tool.await_args_list]
    assert services == ["start", "cancel"]


@pytest.mark.asyncio
async def test_resync_pool_cancels_all_slots(fake_main):
    st = _state()
    st.mcp_client = SimpleNamespace(
        tool_names=["ha_call_service"],
        call_tool=AsyncMock(return_value="ok"),
    )
    mgr = TimerManager(st)
    await mgr.resync_pool_on_startup()
    entities = [c.args[1]["entity_id"]
                for c in st.mcp_client.call_tool.await_args_list]
    assert entities == [f"timer.pal_timer_{i}" for i in range(1, 6)]
    assert all(c.args[1]["service"] == "cancel"
               for c in st.mcp_client.call_tool.await_args_list)


@pytest.mark.asyncio
async def test_sensor_published_on_create_and_cancel(fake_main):
    st = _state()
    mgr = TimerManager(st)
    t = await mgr.create(60, None)
    count, timers = st.mqtt_bridge.publish_active_timers.await_args.args
    assert count == 1 and timers[0]["name"] == "Timer 1"
    assert 0 <= timers[0]["remaining_s"] <= 60 and "ends_at" in timers[0]
    await mgr.cancel(t.id)
    count, timers = st.mqtt_bridge.publish_active_timers.await_args.args
    assert count == 0 and timers == []
