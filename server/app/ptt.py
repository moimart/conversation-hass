"""Push-to-Talk session management.

A PTT session is opened by any trigger surface (HTTP, MQTT, WebSocket) and
closed by `end_ptt` or the safety timeout. While open:

  * The mic is auto-unmuted on the RPi (and restored on end).
  * Any in-flight TTS is interrupted (`tts_cancel` to the RPi + immediate
    `set_ai_speaking(False)` server-side so STT picks up audio right away).
  * `conversation._wake_detected` is set to True so the existing
    `process_text`/`on_silence` plumbing accepts the incoming utterance as
    a command without needing the wake word.

On `end_ptt`, we ask the audio pipeline to flush whatever VAD speech segment
is currently buffered (so the user doesn't have to wait for the 1.5 s silence
detector), then call `conversation.on_silence()` exactly like a wake-word turn.

Press-and-release events arriving < `DEBOUNCE_S` apart are treated as a
cancel (drops the buffer, no LLM call). Filters bouncy hardware buttons.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal.ptt")

# Any `end` arriving sooner than this after `start` is considered a bounce —
# cancel the session and don't run the LLM.
DEBOUNCE_S = 0.10

# If `end` never arrives, finalize and process anyway after this long.
SAFETY_TIMEOUT_S = 20.0


@dataclass
class PTTSession:
    started_at: float
    prev_muted: bool
    was_tts_active: bool
    timeout_task: Optional[asyncio.Task] = field(default=None, repr=False)


async def _push_to_rpi(state: "AppState", msg: dict) -> bool:
    """Mirror of main._push_to_rpi — kept here to avoid an import cycle."""
    ws = state.audio_websocket
    if not ws:
        return False
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        log.warning(f"Could not send {msg.get('type')} to RPi: {e}")
        return False


async def _broadcast_ptt_active(state: "AppState", active: bool) -> None:
    """Tell both the RPi (which relays to the kiosk) and any direct UI clients
    that a PTT session just opened or closed. The kiosk uses this to flip
    the body.ptt-active class that drives the PTT chip + orb glow."""
    msg = {"type": "ptt_active", "active": bool(active)}
    await _push_to_rpi(state, msg)
    # Late import to avoid circulars at module load.
    from .main import broadcast_to_ui
    await broadcast_to_ui(state, msg)


async def start_ptt(state: "AppState") -> dict:
    """Open a PTT session. Idempotent: if one is already open, returns its
    status without touching anything.

    Returns a small dict suitable for an HTTP response — `{"status": "...",
    "session": bool}`."""
    # Pressing PTT counts as kiosk activity → wake the display AND reset
    # the photo-frame idle timer.
    try:
        from .main import _record_user_activity
        _record_user_activity(state)
    except Exception:
        pass
    conv = state.conversation
    pipeline = state.pipeline
    if not conv or not pipeline:
        log.warning("PTT start ignored: pipeline / conversation not ready")
        return {"status": "not_ready", "session": False}

    if state.ptt is not None:
        # Idempotent — second `start` while a session is active is a no-op.
        log.debug("PTT start: session already active, ignoring")
        return {"status": "already_active", "session": True}

    # The RPi audio_streamer holds the only path from microphone to STT.
    # If it's not currently connected, opening a session would visually
    # flip the kiosk to "listening" while no audio ever flows — exactly
    # the symptom the user reported. Refuse cleanly instead.
    if state.audio_websocket is None:
        log.warning("PTT start refused: RPi audio_streamer not connected")
        return {"status": "rpi_disconnected", "session": False}

    prev_muted = bool(state.mic_muted)
    was_tts_active = bool(pipeline._ai_speaking)

    session = PTTSession(
        started_at=time.monotonic(),
        prev_muted=prev_muted,
        was_tts_active=was_tts_active,
    )
    state.ptt = session

    # 1. If TTS is playing, cancel it on the RPi and clear the echo-suppression
    #    flag server-side so STT starts processing audio immediately. The RPi
    #    will still echo `tts_finished` after the cancel; the bookkeeping
    #    converges either way.
    if was_tts_active:
        await _push_to_rpi(state, {"type": "tts_cancel"})
        pipeline.set_ai_speaking(False)

    # 2. Auto-unmute if the user keeps the mic muted at rest.
    if prev_muted:
        await _push_to_rpi(state, {"type": "mute_set", "muted": False})

    # 3. Bypass the wake-word gate. From here, any STT output flows into the
    #    command buffer via the normal `process_text` path.
    conv._wake_detected = True
    conv._awaiting_followup = False
    conv._command_buffer.clear()
    conv.ptt_active = True
    await conv._set_state("listening")

    # 4. Tell the kiosk so it can show the PTT chip + orb glow.
    await _broadcast_ptt_active(state, True)

    # 5. Schedule the safety timeout.
    session.timeout_task = asyncio.create_task(_safety_timeout(state))

    log.info(
        f"PTT session opened (prev_muted={prev_muted}, was_tts_active={was_tts_active})"
    )
    return {"status": "ok", "session": True}


async def end_ptt(state: "AppState", cancel: bool = False) -> dict:
    """Close the active PTT session. Idempotent: if no session is open,
    returns a noop status.

    `cancel=True` drops the captured audio without running the LLM (used by
    the debounce path and by `/api/ptt/cancel`)."""
    session: Optional[PTTSession] = state.ptt
    if session is None:
        log.debug("PTT end ignored: no active session")
        return {"status": "not_active", "session": False}

    # Cancel the safety timeout — if WE are running because IT fired, the
    # task is already done and cancel() is a no-op.
    if session.timeout_task is not None:
        session.timeout_task.cancel()
        session.timeout_task = None

    held_s = time.monotonic() - session.started_at
    debounced = (held_s < DEBOUNCE_S) and not cancel
    if debounced:
        log.info(f"PTT debounced: held {held_s*1000:.0f}ms < {DEBOUNCE_S*1000:.0f}ms — cancelling")
        cancel = True

    # Detach the session BEFORE we await anything else so a re-entrant
    # `start` after this returns sees a clean slate.
    state.ptt = None

    conv = state.conversation
    pipeline = state.pipeline

    # 1. Restore the prior mute state.
    if session.prev_muted:
        await _push_to_rpi(state, {"type": "mute_set", "muted": True})

    # 2. Clear the PTT visual hint right away (regardless of cancel).
    if conv is not None:
        conv.ptt_active = False
    await _broadcast_ptt_active(state, False)

    if cancel:
        # Drop any captured audio — back to idle.
        if pipeline is not None:
            await pipeline.force_finalize(discard=True)
        if conv is not None:
            conv._command_buffer.clear()
            conv._wake_detected = False
            await conv._set_state("idle")
        log.info(f"PTT session closed (cancel, held {held_s*1000:.0f}ms)")
        return {"status": "cancelled", "session": False}

    # 3. Force-finalize whatever the VAD has been buffering so the user
    #    doesn't have to wait 1.5 s of silence. The pipeline returns a
    #    transcription dict shaped like the per-chunk silence-finalised
    #    one; we feed it through the same display + process_text path
    #    the audio loop uses.
    final = None
    if pipeline is not None:
        try:
            final = await pipeline.force_finalize()
        except Exception as e:
            log.warning(f"PTT force_finalize failed: {e}")

    if final and conv is not None:
        transcription_msg = {
            "type": "transcription",
            "text": final["text"],
            "is_partial": False,
            "speaker": final.get("speaker", "human"),
        }
        from .main import broadcast_to_ui
        await broadcast_to_ui(state, transcription_msg)
        await _push_to_rpi(state, transcription_msg)
        if final.get("speaker") != "ai":
            await conv.process_text(final["text"])

    # 4. Run the LLM exactly like a wake-word turn would, OR clean up if
    #    no audio was captured. Without this, an empty PTT (user pressed
    #    and released without speaking, or the mic was disconnected mid-
    #    session) would leave the conversation with _wake_detected=True
    #    and the kiosk stuck on state-listening forever.
    if conv is not None:
        has_text = (final is not None) and bool(final.get("text", "").strip()) and final.get("speaker") != "ai"
        if has_text:
            asyncio.create_task(conv.on_silence())
        else:
            log.info("PTT closed with no transcript — resetting to idle")
            conv._command_buffer.clear()
            conv._wake_detected = False
            await conv._set_state("idle")

    log.info(f"PTT session closed (held {held_s*1000:.0f}ms)")
    return {"status": "ok", "session": False}


async def _safety_timeout(state: "AppState") -> None:
    """Background task that closes a stuck session after SAFETY_TIMEOUT_S."""
    try:
        await asyncio.sleep(SAFETY_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    if state.ptt is None:
        return
    log.warning(f"PTT safety timeout ({SAFETY_TIMEOUT_S}s) — finalising stuck session")
    await end_ptt(state, cancel=False)
