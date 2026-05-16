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
from typing import Iterable

import httpx

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
