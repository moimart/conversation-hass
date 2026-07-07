"""Canned content for the public review demo (HAL_DEMO_MODE).

The demo runs with no Postgres, so the live conversation log is empty. To keep
the "activity log" view looking real for an App Store reviewer, we serve a fixed
set of believable entries, shaped exactly like `ConversationLog.fetch()` rows
(id / ts / kind / text / origin / meta / has_image), oldest→newest.

Timestamps are generated relative to now at request time so the log always looks
fresh. Nothing here touches a database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# (minutes-ago, kind, text, origin). Ordered newest-first here for readability;
# emitted oldest-first (ASC) to match the real endpoint.
_SCRIPT: list[tuple[int, str, str, str | None]] = [
    (2,   "user",         "What's the weather looking like this afternoon?", "iPhone"),
    (2,   "assistant",    "Clear and mild — around 22 degrees through the afternoon, cooling to 15 by evening.", None),
    (16,  "user",         "Play something relaxing.", "Living Room"),
    (16,  "assistant",    "Sure — starting an ambient playlist in the living room.", None),
    (41,  "announcement", "Timer finished: pasta.", None),
    (44,  "user",         "Set a timer for 3 minutes.", "Kitchen"),
    (44,  "assistant",    "Timer set for 3 minutes.", None),
    (88,  "user",         "Remind me to call the plumber tomorrow morning.", "iPhone"),
    (88,  "assistant",    "Okay — I'll remind you tomorrow at 9 AM to call the plumber.", None),
    (140, "user",         "Good morning, PAL. What's on today?", None),
    (140, "assistant",    "Good morning! You have a team sync at 10 and dinner with Sam at 7.", None),
    (140, "announcement", "Good morning. It's 7:24 AM, 18 degrees and clear.", None),
]


def dummy_conversation_log() -> dict:
    """A fixed, believable activity log for the demo, shaped like the real
    `/api/conversation/log` response: {"rows": [...ASC...], "has_more": False}."""
    now = datetime.now(timezone.utc)
    ordered = sorted(_SCRIPT, key=lambda e: -e[0])  # oldest first
    rows = []
    for i, (mins_ago, kind, text, origin) in enumerate(ordered, start=1):
        ts = now - timedelta(minutes=mins_ago)
        rows.append({
            "id": i,
            "ts": ts.isoformat(),
            "kind": kind,
            "text": text,
            "origin": origin,
            "meta": None,
            "has_image": False,
        })
    return {"rows": rows, "has_more": False}
