"""Display power (DPMS) + activity tracking + idle loops.

Tracks kiosk display power state, records user/kiosk activity to drive idle
blanking, and runs the background loops that blank the display and auto-activate
the photo frame after inactivity. Extracted from main.py; callers reach these
via the re-export in main (and the lazy `from .main import` pattern elsewhere).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal")


async def _set_display(state: AppState, target: str) -> bool:
    """Set the kiosk display power to "on" or "off".

    Updates server-side state, pushes the imperative `display_set`
    message to the RPi container (which dispatches to the right host
    binary), and republishes the new state to HA via MQTT. Returns
    False if the RPi container isn't connected — caller can surface
    that to the API client.
    """
    from .main import _push_to_rpi
    target = (target or "").strip().lower()
    if target not in ("on", "off"):
        return False
    state.display_state = target
    state.display_last_activity = time.time()
    pushed = await _push_to_rpi(state, {"type": "display_set", "state": target})
    if state.mqtt_bridge:
        try:
            await state.mqtt_bridge.publish_display_state(target)
        except Exception as e:
            log.debug(f"publish_display_state failed: {e}")
    log.info(f"display → {target} (rpi reachable: {pushed})")
    return pushed


def _record_kiosk_activity(state: AppState) -> None:
    """Mark the kiosk as active right now. If the display is currently
    off, schedule a wake. Caller-side fire-and-forget is fine — wake is
    queued via asyncio.create_task so the caller's hot path stays fast.
    """
    state.display_last_activity = time.time()
    if state.display_state == "off":
        # Wake before whatever's happening proceeds. We use a task so
        # the caller (which may be in a state-change hook) doesn't
        # block on a WS send.
        asyncio.create_task(_set_display(state, "on"))


def _record_user_activity(state: AppState) -> None:
    """Bump both display-wake and photo-frame-idle timers. Use for
    genuine user/system activity (PTT, wake, takeovers, HAL speaking).

    The photo frame opening itself uses _record_kiosk_activity directly
    so it doesn't re-arm its own idle timer.
    """
    state.user_last_activity = time.time()
    _record_kiosk_activity(state)


def _dismiss_photo_frame_async(state: AppState, reason: str) -> None:
    """Fire-and-forget tear-down of an active photo-frame session.

    Used when a command arrives or voice recognition starts processing:
    the frame must yield to the new activity. No-op if nothing is open.
    Scheduled as a task so callers (wake-word callback, command handler)
    don't block on the HA unsubscribe.
    """
    if state.photo_frame_session is None:
        return
    from .photo_frame import stop_photo_frame
    asyncio.create_task(stop_photo_frame(state, reason=reason))


async def _display_auto_off_loop(state: AppState) -> None:
    """Background task: blank the display after `display_auto_off_seconds`
    of no activity. 0 means disabled. The check is cheap; we poll every
    5 s so the worst-case blank latency is interval + cadence ≈ 5 s.
    """
    log.info("Display auto-off loop started")
    try:
        while True:
            await asyncio.sleep(5)
            timeout = int(state.display_auto_off_seconds or 0)
            if timeout <= 0:
                continue
            if state.display_state != "on":
                continue
            idle = time.time() - (state.display_last_activity or 0.0)
            if idle >= timeout:
                log.info(
                    f"display: idle {idle:.0f}s ≥ {timeout}s — blanking"
                )
                await _set_display(state, "off")
    except asyncio.CancelledError:
        log.info("Display auto-off loop cancelled")
        raise
    except Exception as e:
        log.error(f"Display auto-off loop crashed: {e}")


async def _photo_frame_idle_loop(state: AppState) -> None:
    """Background task: auto-activate the photo frame after
    `photo_frame_idle_minutes` of no user activity. 0 means disabled.
    Polls every 15 s (coarser than display since the threshold is in
    minutes anyway). Skips if a session is already open.
    """
    log.info("Photo-frame idle loop started")
    try:
        while True:
            await asyncio.sleep(15)
            minutes = int(state.photo_frame_idle_minutes or 0)
            if minutes <= 0:
                continue
            if (state.photo_frame_session is not None
                    or state.photo_frame_video_session is not None):
                continue
            idle_s = time.time() - (state.user_last_activity or 0.0)
            if idle_s >= minutes * 60:
                log.info(
                    f"photo_frame: idle {idle_s:.0f}s ≥ {minutes}min — activating"
                )
                # Reset BEFORE calling so a permanent failure (e.g. RPi
                # disconnected, no entity configured) doesn't spam every
                # 15 s — next attempt is one full window away.
                state.user_last_activity = time.time()
                try:
                    from .photo_frame import start_photo_frame
                    await start_photo_frame(state, "")
                except Exception as e:
                    log.warning(f"Photo-frame idle auto-start failed: {e}")
    except asyncio.CancelledError:
        log.info("Photo-frame idle loop cancelled")
        raise
    except Exception as e:
        log.error(f"Photo-frame idle loop crashed: {e}")
