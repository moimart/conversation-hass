"""Voice timers: in-memory countdown timers with HA mirroring.

PAL is the source of truth — each timer is an asyncio task that (a) pushes a
full-screen 10-second countdown to the ORIGINATING device only, (b) speaks a
finish announcement on ALL devices (kiosk + every satellite), and (c) mirrors
start/cancel onto a pre-created Home Assistant `timer.pal_timer_1..5` helper
pool so HA dashboards show live remaining time and automations can hook
`timer.finished`. Cancelling the HA entity does NOT stop PAL's timer (the
mirror is display-only), and timers do not survive an ai-server restart —
`resync_pool_on_startup()` cancels the pool so HA never shows ghosts.

Names come from the runtime-config template `timer_name_template`
(default "Timer {n}"); announcements from `timer_announce_template`
(default "{name} is ready."). Numbering restarts at 1 whenever no timers
are active.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("hal.timers")

COUNTDOWN_WINDOW_S = 10
POOL_SIZE = 5
MAX_DURATION_S = 24 * 3600


# --- duration helpers (shared by the tools and the conversation intercepts) ---

_DUR_RE = re.compile(
    r"(?:(\d+)\s*(?:hours?|hrs?|h)\b)?[\s,]*(?:and\s+)?"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\b)?[\s,]*(?:and\s+)?"
    r"(?:(\d+)\s*(?:seconds?|secs?|s)\b)?",
    re.I,
)

_HALF_RE = re.compile(r"\bhalf\s+an?\s+hour\b", re.I)


def parse_duration_seconds(text: str) -> int | None:
    """Total seconds for natural phrasings: "5 minutes", "1 hour 10 minutes",
    "90 seconds", "half an hour". None when no duration is present."""
    if _HALF_RE.search(text):
        return 1800
    best = 0
    for m in _DUR_RE.finditer(text):
        if not any(m.groups()):
            continue
        h, mn, s = (int(g) if g else 0 for g in m.groups())
        best = max(best, h * 3600 + mn * 60 + s)
    return best or None


def spoken_duration(seconds: int) -> str:
    """Human phrasing: 90 -> "1 minute and 30 seconds", 3600 -> "1 hour"."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s or not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _hms(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


@dataclass
class Timer:
    id: str
    name: str
    seq: int
    duration_s: int
    ends_at_monotonic: float
    ends_at_epoch_ms: int
    origin_token: str | None        # satellite pairing token; None = kiosk. NEVER logged.
    pool_entity: str | None         # timer.pal_timer_N mirror slot, or None
    task: asyncio.Task | None = field(default=None, repr=False)
    countdown_armed: bool = False

    def remaining_s(self) -> int:
        return max(0, int(round(self.ends_at_monotonic - time.monotonic())))


class TimerManager:
    """Owns all active timers. One instance lives on AppState."""

    def __init__(self, state) -> None:
        self._state = state
        self._timers: dict[str, Timer] = {}
        self._id_counter = 0
        self._lock = asyncio.Lock()
        self._free_pool: list[str] = [f"timer.pal_timer_{i}" for i in range(1, POOL_SIZE + 1)]

    # --- public API ----------------------------------------------------------

    def list_active(self) -> list[Timer]:
        return sorted(self._timers.values(), key=lambda t: t.ends_at_monotonic)

    def count(self) -> int:
        return len(self._timers)

    async def create(self, duration_s: int, origin_token: str | None) -> Timer:
        duration_s = max(1, min(MAX_DURATION_S, int(duration_s)))
        async with self._lock:
            self._id_counter += 1
            # Numbering restarts at 1 when nothing is active; otherwise the
            # next free number above the running ones ("Timer 1" + "Timer 2"
            # active -> "Timer 3").
            seq = max((t.seq for t in self._timers.values()), default=0) + 1
            name = self._render_name(seq)
            pool_entity = self._free_pool.pop(0) if self._free_pool else None
            if pool_entity is None:
                log.warning(f"timer pool exhausted (> {POOL_SIZE} concurrent) — "
                            f"{name} runs without an HA mirror")
            timer = Timer(
                id=f"t{self._id_counter}",
                name=name,
                seq=seq,
                duration_s=duration_s,
                ends_at_monotonic=time.monotonic() + duration_s,
                ends_at_epoch_ms=int((time.time() + duration_s) * 1000),
                origin_token=origin_token,
                pool_entity=pool_entity,
            )
            self._timers[timer.id] = timer
        timer.task = asyncio.create_task(self._run(timer))
        asyncio.create_task(self._ha_call(timer, "start"))
        await self._publish_sensor()
        log.info(f"timer created: {timer.name} ({duration_s}s, "
                 f"origin={'phone' if origin_token else 'kiosk'}, "
                 f"mirror={timer.pool_entity or 'none'})")
        return timer

    async def cancel(self, timer_id: str) -> bool:
        timer = self._timers.get(timer_id)
        if timer is None:
            return False
        await self._remove(timer)
        if timer.task and not timer.task.done() and timer.task is not asyncio.current_task():
            timer.task.cancel()
        if timer.countdown_armed:
            await self._push_to_origin(timer, {"type": "timer_countdown_cancel",
                                               "timer_id": timer.id})
            await self._rearm_next(timer.origin_token)
        asyncio.create_task(self._ha_call(timer, "cancel"))
        await self._publish_sensor()
        log.info(f"timer cancelled: {timer.name}")
        return True

    async def cancel_by_name(self, name: str) -> bool:
        """Cancel by rendered name (case-insensitive) or bare number ("2",
        "timer 2") so cancellation works under any naming template."""
        needle = (name or "").strip().lower()
        if not needle:
            return False
        digits = re.findall(r"\d+", needle)
        for t in list(self._timers.values()):
            if t.name.lower() == needle or (digits and t.seq == int(digits[-1])):
                return await self.cancel(t.id)
        return False

    async def cancel_all(self) -> int:
        n = 0
        for t in list(self._timers.values()):
            if await self.cancel(t.id):
                n += 1
        return n

    async def resync_pool_on_startup(self) -> None:
        """Best-effort timer.cancel on every pool entity: a restart loses the
        in-memory timers, so any still-running HA mirrors are ghosts."""
        for entity in [f"timer.pal_timer_{i}" for i in range(1, POOL_SIZE + 1)]:
            await self._ha_service("cancel", entity)

    # --- internals -----------------------------------------------------------

    def _render_name(self, seq: int) -> str:
        template = getattr(self._state, "timer_name_template", "") or "Timer {n}"
        try:
            name = template.format(n=seq)
            if name == template and "{n}" not in template:
                # Template forgot the placeholder — disambiguate anyway.
                name = f"{template} {seq}".strip()
            return name
        except Exception:
            return f"Timer {seq}"

    def _render_announcement(self, timer: Timer) -> str:
        template = getattr(self._state, "timer_announce_template", "") or "{name} is ready."
        try:
            return template.format(name=timer.name)
        except Exception:
            return f"{timer.name} is ready."

    async def _run(self, timer: Timer) -> None:
        try:
            lead = max(0.0, timer.ends_at_monotonic - time.monotonic() - COUNTDOWN_WINDOW_S)
            if lead:
                await asyncio.sleep(lead)
            await self._maybe_arm_countdown(timer)
            remaining = timer.ends_at_monotonic - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            await self._fire(timer)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"timer {timer.name} task failed: {e}", exc_info=True)
            await self._remove(timer)
            await self._publish_sensor()

    async def _fire(self, timer: Timer) -> None:
        await self._remove(timer)
        text = self._render_announcement(timer)
        # Safety dismiss — the client self-dismisses at 0 as well.
        await self._push_to_origin(timer, {"type": "timer_countdown_dismiss",
                                           "timer_id": timer.id})
        await self._rearm_next(timer.origin_token)
        from .main import announce_everywhere
        await announce_everywhere(self._state, text, log_source="timer")
        await self._publish_sensor()
        log.info(f"timer fired: {timer.name}")
        # No HA call needed: the mirrored entity finishes on its own clock.

    async def _remove(self, timer: Timer) -> None:
        self._timers.pop(timer.id, None)
        if timer.pool_entity and timer.pool_entity not in self._free_pool:
            self._free_pool.append(timer.pool_entity)
            self._free_pool.sort()  # deterministic reuse: lowest slot first

    async def _maybe_arm_countdown(self, timer: Timer) -> None:
        """Arm the full-screen countdown on the timer's device — but only if
        this timer is the SOONEST in-window timer for that device (two timers
        ending close together must not fight over the overlay)."""
        if timer.id not in self._timers:
            return  # cancelled while sleeping
        soonest = self._soonest_in_window(timer.origin_token)
        if soonest is None or soonest.id != timer.id:
            return
        timer.countdown_armed = True
        await self._push_to_origin(timer, {
            "type": "timer_countdown",
            "timer_id": timer.id,
            "name": timer.name,
            "ends_at_epoch_ms": timer.ends_at_epoch_ms,
            "remaining_s": timer.remaining_s(),
        })

    async def _rearm_next(self, origin_token: str | None) -> None:
        """After a fire/cancel, hand the countdown to the next same-device
        timer already inside its window (if any)."""
        nxt = self._soonest_in_window(origin_token)
        if nxt is not None and not nxt.countdown_armed:
            await self._maybe_arm_countdown(nxt)

    def _soonest_in_window(self, origin_token: str | None) -> Timer | None:
        candidates = [
            t for t in self._timers.values()
            if t.origin_token == origin_token and t.remaining_s() <= COUNTDOWN_WINDOW_S
        ]
        return min(candidates, key=lambda t: t.ends_at_monotonic) if candidates else None

    async def _push_to_origin(self, timer: Timer, msg: dict) -> None:
        """Kiosk-origin timers target the kiosk (+ web mirrors); phone-origin
        timers target ONLY that phone (same routing as the conversation log)."""
        from .main import _push_to_rpi, broadcast_to_ui, send_to_device
        try:
            if timer.origin_token:
                await send_to_device(self._state, timer.origin_token, msg)
            else:
                await _push_to_rpi(self._state, msg)
                await broadcast_to_ui(self._state, msg)
        except Exception as e:
            log.debug(f"timer push failed: {e}")

    async def _publish_sensor(self) -> None:
        bridge = getattr(self._state, "mqtt_bridge", None)
        if bridge is None:
            return
        timers = [
            {
                "name": t.name,
                "remaining_s": t.remaining_s(),
                "ends_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                         time.localtime(t.ends_at_epoch_ms / 1000)),
            }
            for t in self.list_active()
        ]
        try:
            await bridge.publish_active_timers(len(timers), timers)
        except Exception as e:
            log.debug(f"active_timers publish failed: {e}")

    async def _ha_call(self, timer: Timer, service: str) -> None:
        if timer.pool_entity is None:
            return
        await self._ha_service(service, timer.pool_entity,
                               duration=timer.duration_s if service == "start" else None)

    async def _ha_service(self, service: str, entity_id: str,
                          duration: int | None = None) -> None:
        """Mirror to HA via the MCP client; never raises (mirror is best-effort)."""
        mcp = getattr(self._state, "mcp_client", None)
        if mcp is None or "ha_call_service" not in getattr(mcp, "tool_names", []):
            return
        args: dict = {"domain": "timer", "service": service,
                      "target": {"entity_id": entity_id}}
        if duration is not None:
            args["service_data"] = {"duration": _hms(duration)}
        try:
            await asyncio.wait_for(mcp.call_tool("ha_call_service", args), timeout=5.0)
        except Exception as e:
            log.warning(f"HA timer mirror {service} failed for {entity_id}: {type(e).__name__}")
