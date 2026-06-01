"""Home Assistant calendar REST helpers.

Hits HA's REST API directly with a bearer token, mirroring the pattern
used elsewhere (see main._fetch_image_url). Two operations:

  * list_calendars()        -> [{entity_id, name}, ...]   (60 s TTL cache)
  * fetch_calendar_events() -> [{summary, start, end, all_day,
                                calendar_entity, calendar_friendly_name,
                                color_idx}, ...]

A best-match resolver maps a free-text calendar name to one of HA's
calendar entities (case-insensitive, friendly-name fallback, fuzzy
substring). When the caller passes None or "" or a name with no match,
the resolver returns ALL calendars so the kiosk shows a merged view.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal.calendar_ha")

_LIST_TTL_S = 60.0
_REQUEST_TIMEOUT = 8.0

# Module-level cache for the calendar list. (timestamp, [entries])
_list_cache: tuple[float, list[dict]] | None = None


def _ha_creds() -> tuple[str, str] | None:
    url = os.environ.get("HA_URL", "").strip().rstrip("/")
    token = os.environ.get("HA_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def _color_idx_for(entity_id: str) -> int:
    """Stable 0..5 colour index from the entity_id. Uses Python hash so
    the same calendar gets the same colour every render."""
    return abs(hash(entity_id)) % 6


async def list_calendars(*, force_refresh: bool = False) -> list[dict]:
    """Return HA's calendar list. Each entry: {entity_id, name}.

    Cached in-process for 60 s so repeated invocations don't hammer HA.
    Returns an empty list if HA_URL / HA_TOKEN are unset, or if the
    request fails.
    """
    global _list_cache
    now = time.monotonic()
    if not force_refresh and _list_cache and (now - _list_cache[0]) < _LIST_TTL_S:
        return list(_list_cache[1])

    creds = _ha_creds()
    if not creds:
        log.warning("HA calendar list unavailable (HA_URL / HA_TOKEN unset)")
        return []
    url, token = creds

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.get(f"{url}/api/calendars", headers=headers)
            r.raise_for_status()
            data = r.json() or []
    except Exception as e:
        log.warning(f"HA calendar list fetch failed: {e}")
        return []

    entries: list[dict] = []
    for item in data:
        entity_id = (item.get("entity_id") or "").strip()
        if not entity_id:
            continue
        name = (item.get("name") or entity_id.split(".", 1)[-1]).strip()
        entries.append({"entity_id": entity_id, "name": name})

    _list_cache = (now, entries)
    return list(entries)


def invalidate_list_cache() -> None:
    """Drop the cached calendar list. Tests use this; runtime rarely needs it."""
    global _list_cache
    _list_cache = None


def resolve_calendars(query: str | None, available: list[dict]) -> list[dict]:
    """Pick the calendar entries matching `query`.

    Empty / None / no-match -> return ALL available (merged view).
    Match strategy, in order:
      1. exact case-insensitive match on entity_id (with or without "calendar." prefix)
      2. exact case-insensitive match on friendly name
      3. case-insensitive substring contains on friendly name
    """
    if not available:
        return []
    if not query:
        return list(available)

    q = query.strip().lower()
    if not q:
        return list(available)

    # 1. entity_id exact
    for c in available:
        eid = c["entity_id"].lower()
        if q == eid or q == eid.split(".", 1)[-1] or f"calendar.{q}" == eid:
            return [c]

    # 2. friendly name exact
    for c in available:
        if c["name"].lower() == q:
            return [c]

    # 3. substring
    matches = [c for c in available if q in c["name"].lower()]
    if matches:
        return matches

    log.info(f"calendar query {query!r} matched nothing, falling back to all calendars")
    return list(available)


async def fetch_calendar_events(
    entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Fetch events from one or more HA calendar entities.

    Returns a flat list, one entry per event, sorted by start time. Each
    entry has: summary, start (ISO8601), end (ISO8601), all_day (bool),
    calendar_entity, calendar_friendly_name, color_idx (0..5).

    `start` and `end` are timezone-aware datetimes (UTC recommended).
    Failures on individual entities are logged and skipped — we never
    raise out of this helper, so a single broken calendar can't break
    the overlay.
    """
    creds = _ha_creds()
    if not creds:
        log.warning("HA calendar events unavailable (HA_URL / HA_TOKEN unset)")
        return []
    url, token = creds

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    # HA expects ISO8601 with offset.
    qs = {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    headers = {"Authorization": f"Bearer {token}"}

    # Build a friendly-name lookup so each event carries its source label
    # without an extra round-trip per event.
    available = await list_calendars()
    name_lookup = {c["entity_id"]: c["name"] for c in available}

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for entity_id in entity_ids:
            try:
                r = await client.get(
                    f"{url}/api/calendars/{entity_id}",
                    headers=headers,
                    params=qs,
                )
                r.raise_for_status()
                events = r.json() or []
            except Exception as e:
                log.warning(f"HA calendar fetch failed for {entity_id}: {e}")
                continue

            color_idx = _color_idx_for(entity_id)
            friendly = name_lookup.get(entity_id, entity_id.split(".", 1)[-1])
            for ev in events:
                out.append(_normalize_event(ev, entity_id, friendly, color_idx))

    out.sort(key=lambda e: (not e["all_day"], e["start"]))
    return out


def _normalize_event(raw: dict, entity_id: str, friendly: str, color_idx: int) -> dict:
    """Flatten HA's event shape to the kiosk-side schema.

    HA returns {start: {dateTime|date}, end: {dateTime|date}, summary, ...}.
    We collapse the date/dateTime into a single string and a flat all_day flag.
    """
    start_obj = raw.get("start") or {}
    end_obj = raw.get("end") or {}
    if isinstance(start_obj, str):
        # Some HA integrations return a string directly.
        start_str = start_obj
        all_day = "T" not in start_str
    else:
        all_day = "date" in start_obj and "dateTime" not in start_obj
        start_str = start_obj.get("dateTime") or start_obj.get("date") or ""
    if isinstance(end_obj, str):
        end_str = end_obj
    else:
        end_str = end_obj.get("dateTime") or end_obj.get("date") or ""

    return {
        "summary": (raw.get("summary") or "").strip() or "(untitled)",
        "start": start_str,
        "end": end_str,
        "all_day": bool(all_day),
        "calendar_entity": entity_id,
        "calendar_friendly_name": friendly,
        "color_idx": color_idx,
    }


# === Calendar overlay orchestration =========================================
# Resolve which HA calendars to query, fetch their events for the requested
# range, and push a `show_calendar` payload to the kiosk + UI clients (and the
# matching `hide_calendar`). Extracted from main.py; callers reach these via the
# re-export in main (and the lazy `from .main import` pattern elsewhere).


def _calendar_range(view: str, anchor: "datetime | None" = None) -> "tuple[datetime, datetime]":
    """Return (start, end) UTC datetimes covering the requested view.

    `anchor` is the date the user wants to land on. Defaults to today (UTC).
    For "tomorrow's calendar", the caller passes tomorrow's date; this
    function then computes the day/week/month that contains it.

    * `day`:   anchor 00:00 → anchor+1 day 00:00 (just that day)
    * `week`:  Monday of anchor's week → following Monday
    * `month`: first of anchor's month → first of next month
    """
    from datetime import datetime, timedelta, timezone
    now = anchor or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if view == "day":
        return today, today + timedelta(days=1)
    if view == "week":
        # ISO weekday: Mon=0 .. Sun=6
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    # month — first to first-of-next-month
    first = today.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first


def _parse_anchor_date(s: str) -> "datetime | None":
    """Parse the LLM's / MQTT's `anchor_date` string into a UTC datetime.

    Accepts:
      * ISO date            "2026-05-18"
      * ISO datetime        "2026-05-18T00:00:00"
      * Short ISO datetime  "2026-05-18T15:30"

    Returns None on empty/unparseable input — callers should fall back to
    today() when None.
    """
    from datetime import datetime, timezone
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        log.warning(f"_parse_anchor_date: unparseable {s!r}, falling back to today")
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


async def _show_calendar(
    state: AppState,
    view: str = "month",
    calendar_name: str = "",
    duration_s: int | None = None,
    anchor_date: str = "",
) -> dict:
    """Resolve calendar(s), fetch events, push show_calendar to kiosk + UI clients.

    Returns the same payload that was pushed (useful for tests / LLM tool reply).
    `calendar_name` empty / no match → merged view of all HA calendars.
    `anchor_date` empty → today; otherwise ISO date string (see
    `_parse_anchor_date`) to show a specific day/week/month.
    """
    from .main import _record_user_activity, _push_to_rpi, broadcast_to_ui, _VALID_CAL_VIEWS
    _record_user_activity(state)
    view = (view or "month").lower().strip()
    if view not in _VALID_CAL_VIEWS:
        view = "month"

    if duration_s is None:
        duration_s = int(state.runtime_config.get("calendar_dismiss_seconds", 30) or 30)
    duration_s = max(5, min(600, int(duration_s)))

    name_query = (calendar_name or "").strip() or str(
        state.runtime_config.get("calendar_default_source", "") or ""
    ).strip()

    available = await list_calendars()
    chosen = resolve_calendars(name_query or None, available)

    anchor = _parse_anchor_date(anchor_date)
    start, end = _calendar_range(view, anchor)
    events = await fetch_calendar_events([c["entity_id"] for c in chosen], start, end)

    if not chosen:
        source_label = "(no calendars)"
    elif name_query and len(chosen) == 1:
        source_label = chosen[0]["name"]
    else:
        source_label = "All calendars"

    payload = {
        "type": "show_calendar",
        "view": view,
        "title": _format_range_title(view, start),
        "source_label": source_label,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "events": events,
        "duration_s": duration_s,
    }
    await _push_to_rpi(state, payload)
    await broadcast_to_ui(state, payload)
    log.info(
        f"calendar shown: view={view} source={source_label!r} "
        f"events={len(events)} duration={duration_s}s"
    )
    return payload


async def _hide_calendar(state: AppState) -> bool:
    from .main import _push_to_rpi, broadcast_to_ui
    msg = {"type": "hide_calendar"}
    pushed = await _push_to_rpi(state, msg)
    await broadcast_to_ui(state, msg)
    log.info("calendar hidden")
    return pushed


def _format_range_title(view: str, start: "datetime") -> str:
    """e.g. 'May 2026' / 'Week of 11 May 2026' / 'Saturday 16 May 2026'."""
    if view == "month":
        return start.strftime("%B %Y")
    if view == "week":
        return "Week of " + start.strftime("%d %B %Y")
    return start.strftime("%A %d %B %Y")
