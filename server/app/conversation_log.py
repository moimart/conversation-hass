"""Persistent conversation log backed by PostgreSQL.

Every user request, assistant answer, and announcement is appended here —
timestamped and origin-labeled — and browsed via the full-screen conversation
log view (GET /api/conversation/log). The ai-server is the ONLY writer;
satellites are HTTP clients of the server, so single-writer semantics hold no
matter how many phones are paired.

Design rules:
- A database failure must NEVER break a turn: `log()` swallows everything.
- Startup must never block on postgres: `connect()` is launched as a
  background task with retry/backoff, and `log()`/`fetch()` lazily reconnect
  (single-flight) so a postgres restart heals without an ai-server restart.
- `origin` stores the pairing `device_name` for satellite turns (never the
  secret token — resolved by the caller at write time), NULL for kiosk/voice,
  or a source label for announcements ('api', 'mqtt', 'openclaw', 'voice-tool').
- Empty DSN ⇒ feature disabled: writes no-op, reads report `disabled`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

try:  # the test env has no asyncpg; tests patch `asyncpg.create_pool`
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

log = logging.getLogger("hal.conversation_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_log (
    id     BIGSERIAL    PRIMARY KEY,
    ts     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    kind   TEXT         NOT NULL CHECK (kind IN ('user','assistant','announcement')),
    text   TEXT         NOT NULL,
    origin TEXT,
    meta   JSONB
);
CREATE INDEX IF NOT EXISTS conversation_log_id_desc ON conversation_log (id DESC);
"""

VALID_KINDS = ("user", "assistant", "announcement")


class ConversationLog:
    """Append + page the conversation log. Safe to use before/without postgres."""

    def __init__(self, dsn: str):
        self.dsn = (dsn or "").strip()
        self._pool = None
        self._connect_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.dsn) and asyncpg is not None

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        """Background-startup path: retry/backoff so a slow postgres boot never
        blocks (or kills) ai-server startup."""
        if not self.enabled:
            if self.dsn and asyncpg is None:
                log.warning("conversation_log: asyncpg not installed — logging disabled")
            else:
                log.info("conversation_log: no DSN configured — logging disabled")
            return
        delay = 1.0
        for attempt in range(12):
            if await self._try_connect():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 1.7, 8.0)
        log.error("conversation_log: could not reach postgres — will keep retrying lazily on use")

    async def _try_connect(self) -> bool:
        """Single connect attempt (used by connect() retries and lazy reconnect)."""
        if not self.enabled:
            return False
        async with self._connect_lock:
            if self._pool is not None:
                return True
            try:
                pool = await asyncpg.create_pool(
                    dsn=self.dsn, min_size=1, max_size=4, command_timeout=10,
                )
                async with pool.acquire() as conn:
                    await conn.execute(_SCHEMA)
                self._pool = pool
                log.info("conversation_log: connected to postgres")
                return True
            except Exception as e:
                log.warning(f"conversation_log: connect failed: {type(e).__name__}: {e}")
                return False

    async def log(
        self,
        kind: str,
        text: str,
        origin: str | None = None,
        meta: dict | None = None,
    ) -> None:
        """Append one entry. NEVER raises — a DB hiccup must not break a turn."""
        try:
            text = (text or "").strip()
            if not text or kind not in VALID_KINDS:
                return
            if self._pool is None and not await self._try_connect():
                log.warning(f"conversation_log: dropping {kind} entry (postgres unavailable)")
                return
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO conversation_log (kind, text, origin, meta) "
                    "VALUES ($1, $2, $3, $4)",
                    kind, text, origin,
                    json.dumps(meta) if meta is not None else None,
                )
        except Exception as e:
            log.warning(f"conversation_log: write failed ({type(e).__name__}: {e}) — entry dropped")
            # A failed write often means a dead pool (postgres restarted).
            # Drop it so the next call lazily reconnects.
            self._pool = None

    async def fetch(self, limit: int = 100, before_id: int | None = None) -> dict:
        """One page, newest-first query reversed to ASC (oldest→newest) for
        direct append/prepend in the view. Raises on DB failure (the HTTP
        layer maps it to 503)."""
        limit = max(1, min(500, int(limit)))
        if not self.enabled:
            return {"rows": [], "has_more": False, "disabled": True}
        if self._pool is None and not await self._try_connect():
            raise RuntimeError("conversation log database unavailable")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, ts, kind, text, origin, meta FROM conversation_log "
                "WHERE ($1::bigint IS NULL OR id < $1) "
                "ORDER BY id DESC LIMIT $2",
                before_id, limit,
            )
        out: list[dict[str, Any]] = []
        for r in reversed(rows):  # ASC for the client
            meta = r["meta"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (TypeError, ValueError):
                    meta = None
            out.append({
                "id": r["id"],
                "ts": r["ts"].isoformat() if r["ts"] is not None else None,
                "kind": r["kind"],
                "text": r["text"],
                "origin": r["origin"],
                "meta": meta,
            })
        return {"rows": out, "has_more": len(rows) == limit}

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass
