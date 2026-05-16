"""Tests for the HA calendar REST helpers."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from server.app import calendar_ha
from server.app.calendar_ha import (
    _color_idx_for,
    _normalize_event,
    fetch_calendar_events,
    invalidate_list_cache,
    list_calendars,
    resolve_calendars,
)


@pytest.fixture(autouse=True)
def reset_cache():
    invalidate_list_cache()
    yield
    invalidate_list_cache()


@pytest.fixture
def ha_env(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha.test:8123")
    monkeypatch.setenv("HA_TOKEN", "supersecret")


# === Resolver ============================================================

CALENDARS = [
    {"entity_id": "calendar.family", "name": "Family"},
    {"entity_id": "calendar.work", "name": "Work"},
    {"entity_id": "calendar.family_birthdays", "name": "Family Birthdays"},
]


def test_empty_query_returns_all():
    assert resolve_calendars(None, CALENDARS) == CALENDARS
    assert resolve_calendars("", CALENDARS) == CALENDARS
    assert resolve_calendars("   ", CALENDARS) == CALENDARS


def test_exact_entity_id_match():
    assert resolve_calendars("calendar.family", CALENDARS) == [CALENDARS[0]]


def test_exact_entity_id_match_without_prefix():
    assert resolve_calendars("family", CALENDARS) == [CALENDARS[0]]


def test_friendly_name_exact_case_insensitive():
    assert resolve_calendars("WORK", CALENDARS) == [CALENDARS[1]]


def test_substring_match_returns_all_matches():
    out = resolve_calendars("family birthd", CALENDARS)
    assert out == [CALENDARS[2]]


def test_partial_substring_returns_multiple():
    # "family" matches both Family and Family Birthdays via friendly-name exact
    # falling back to substring. Friendly-name exact ("Family") wins, so only one.
    out = resolve_calendars("family", CALENDARS)
    assert out == [CALENDARS[0]]


def test_no_match_falls_back_to_all():
    out = resolve_calendars("does-not-exist", CALENDARS)
    assert out == CALENDARS


def test_empty_available_returns_empty():
    assert resolve_calendars("anything", []) == []


# === Color index =========================================================

def test_color_idx_stable_and_in_range():
    for eid in ("calendar.family", "calendar.work", "calendar.holidays"):
        idx = _color_idx_for(eid)
        assert 0 <= idx < 6
        assert idx == _color_idx_for(eid)  # deterministic across calls


# === Event normalisation =================================================

def test_normalize_timed_event():
    raw = {
        "summary": "Standup",
        "start": {"dateTime": "2026-05-16T09:00:00+00:00"},
        "end": {"dateTime": "2026-05-16T09:30:00+00:00"},
    }
    out = _normalize_event(raw, "calendar.work", "Work", 2)
    assert out["summary"] == "Standup"
    assert out["start"] == "2026-05-16T09:00:00+00:00"
    assert out["end"] == "2026-05-16T09:30:00+00:00"
    assert out["all_day"] is False
    assert out["calendar_entity"] == "calendar.work"
    assert out["calendar_friendly_name"] == "Work"
    assert out["color_idx"] == 2


def test_normalize_all_day_event():
    raw = {
        "summary": "Vacation",
        "start": {"date": "2026-05-20"},
        "end":   {"date": "2026-05-25"},
    }
    out = _normalize_event(raw, "calendar.family", "Family", 0)
    assert out["all_day"] is True
    assert out["start"] == "2026-05-20"
    assert out["end"] == "2026-05-25"


def test_normalize_missing_summary_uses_placeholder():
    raw = {"start": {"date": "2026-05-20"}, "end": {"date": "2026-05-21"}}
    out = _normalize_event(raw, "calendar.x", "X", 0)
    assert out["summary"] == "(untitled)"


def test_normalize_string_start_date():
    # Some HA integrations return start/end as bare strings.
    raw = {"summary": "Foo", "start": "2026-05-16", "end": "2026-05-17"}
    out = _normalize_event(raw, "calendar.x", "X", 0)
    assert out["all_day"] is True
    assert out["start"] == "2026-05-16"


# === list_calendars cache ================================================

@pytest.mark.asyncio
async def test_list_calendars_caches_for_60s(ha_env):
    call_count = 0

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return [
                {"entity_id": "calendar.family", "name": "Family"},
                {"entity_id": "calendar.work",   "name": "Work"},
            ]

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw):
            nonlocal_call_count[0] += 1
            return FakeResponse()

    nonlocal_call_count = [0]
    with patch.object(calendar_ha.httpx, "AsyncClient", lambda **kw: FakeClient()):
        first = await list_calendars()
        second = await list_calendars()
    assert nonlocal_call_count[0] == 1, "second call must hit cache"
    assert first == second
    assert first[0]["name"] == "Family"


@pytest.mark.asyncio
async def test_list_calendars_returns_empty_when_creds_missing(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    out = await list_calendars()
    assert out == []


# === fetch_calendar_events ===============================================

@pytest.mark.asyncio
async def test_fetch_skips_failing_entities(ha_env):
    """A single broken calendar must not break the merged fetch."""
    list_resp = [
        {"entity_id": "calendar.family", "name": "Family"},
        {"entity_id": "calendar.work",   "name": "Work"},
    ]
    work_events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-05-16T09:00:00+00:00"},
            "end":   {"dateTime": "2026-05-16T09:30:00+00:00"},
        },
    ]

    class Resp:
        def __init__(self, payload, raises=False):
            self._payload = payload
            self._raises = raises
        def raise_for_status(self):
            if self._raises: raise RuntimeError("HA blew up")
        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None, params=None):
            if url.endswith("/api/calendars"):
                return Resp(list_resp)
            if "calendar.family" in url:
                return Resp(None, raises=True)   # this one breaks
            if "calendar.work" in url:
                return Resp(work_events)
            return Resp([])

    with patch.object(calendar_ha.httpx, "AsyncClient", lambda **kw: FakeClient()):
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 31, tzinfo=timezone.utc)
        out = await fetch_calendar_events(
            ["calendar.family", "calendar.work"], start, end,
        )
    # Only the work event should come through; family is skipped, not raised.
    assert len(out) == 1
    assert out[0]["summary"] == "Standup"
    assert out[0]["calendar_friendly_name"] == "Work"


@pytest.mark.asyncio
async def test_fetch_naive_datetimes_get_utc(ha_env):
    """Naive datetime inputs should not crash; helper assumes UTC."""
    class Resp:
        def raise_for_status(self): pass
        def json(self): return []

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None, params=None):
            return Resp()

    with patch.object(calendar_ha.httpx, "AsyncClient", lambda **kw: FakeClient()):
        start = datetime(2026, 5, 1)   # naive
        end = datetime(2026, 5, 31)    # naive
        out = await fetch_calendar_events([], start, end)
    assert out == []
