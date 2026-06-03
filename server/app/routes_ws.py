"""WebSocket route handlers (audio / PTT / UI) split out of main via APIRouter."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("hal")
router = APIRouter()


@router.websocket("/ws/audio")
async def audio_endpoint(websocket: WebSocket):
    """
    Main audio WebSocket for the RPi audio streamer.

    Receives: raw PCM 16-bit LE audio chunks
    Sends:    JSON messages (transcription, state) and binary (TTS audio)
    """
    from .main import (
        _get_state,
        broadcast_to_ui,
        _record_user_activity,
        _dismiss_photo_frame_async,
        _ma_pause_if_playing,
        _ma_resume_if_we_paused,
        _ma_volume_step,
        _negotiate_rtsp_offer,
        _start_webrtc_stream,
        _forward_kiosk_candidate,
    )

    state = _get_state(websocket.app)
    pipeline = state.pipeline
    conversation = state.conversation

    await websocket.accept()
    state.audio_websocket = websocket
    log.info("Audio client connected")

    # Push the configured "start muted" state so a freshly-booted
    # audio_streamer (which always inits mic_muted=False) lands in the
    # state the user configured rather than always unmuted.
    try:
        start_muted = bool(state.runtime_config.get("start_muted", False))
        await websocket.send_json({"type": "mute_set", "muted": start_muted})
        log.info(f"Pushed initial mute state to RPi: {start_muted}")
    except Exception as e:
        log.warning(f"Could not push initial mute state to RPi: {e}")

    try:
        await websocket.send_json({
            "type": "set_orientation",
            "orientation": state.display_orientation,
            "orb_side": state.orb_side,
        })
        log.info(f"Pushed initial orientation to RPi: {state.display_orientation}/{state.orb_side}")
    except Exception as e:
        log.warning(f"Could not push initial orientation to RPi: {e}")

    # Push the "show clock during photo mode" preference so the kiosk has
    # the right value before any frame opens (default on).
    try:
        await websocket.send_json({
            "type": "set_photo_frame_clock",
            "show": bool(state.photo_frame_show_clock),
        })
    except Exception as e:
        log.warning(f"Could not push photo-frame clock setting to RPi: {e}")

    # Reconcile the kiosk's photo/video frame with our session state. After a
    # server restart our session is None, but the kiosk may still be showing
    # an orphaned frame — a static photo, or a video looping from the RPi's
    # local /media/loop.mp4 (which survives the server restart). Without this
    # the orphan can't be dismissed (the config callbacks bail when there's no
    # session), so toggling Video Mode / Hide appears to "do nothing". Only
    # clear when we genuinely have no active session, so a live frame on a
    # brief RPi reconnect is left alone.
    if state.photo_frame_session is None and state.photo_frame_video_session is None:
        try:
            await websocket.send_json({"type": "hide_photo_frame", "reason": "server_reconnect"})
        except Exception as e:
            log.warning(f"Could not push photo-frame reset to RPi: {e}")

    async def on_wake_word():
        """Callback: play chime on RPi and flash the UI."""
        # Wake the display first so the chime / listening cue actually
        # appears on the panel. Fire-and-forget — the chime audio is
        # already on its way.
        _record_user_activity(state)
        # Voice recognition is starting — dismiss the photo frame so
        # the listening cue / response panel has the screen.
        _dismiss_photo_frame_async(state, reason="wake_word")
        try:
            await broadcast_to_ui(state, {"type": "wake"})
            await websocket.send_json({"type": "wake"})
            if state.wake_chime:
                await websocket.send_json({"type": "chime_start", "size": len(state.wake_chime)})
                await websocket.send_bytes(state.wake_chime)
                await websocket.send_json({"type": "chime_end"})
        except Exception as e:
            log.error(f"Error sending wake chime: {e}")

    async def on_response(text: str, audio_bytes: bytes | None):
        """Callback: send LLM response + TTS audio back to the kiosk (RPi) — or,
        for a satellite-origin turn, ONLY to the originating phone."""
        from .main import send_to_device, cache_satellite_tts
        _record_user_activity(state)

        origin = getattr(state.conversation, "_turn_origin", None)
        if origin is not None:
            # Satellite turn: response text + HAL-voice audio go ONLY to the
            # phone. Nothing to the RPi/broadcast/MQTT, and we don't touch the
            # RPi mic-ducking flag (set_ai_speaking) — that's a kiosk concern.
            await send_to_device(state, origin, {"type": "response", "text": text})
            if audio_bytes:
                seq = cache_satellite_tts(state, origin, audio_bytes, "audio/wav")
                await send_to_device(state, origin, {
                    "type": "tts_play", "url": "/api/satellite/tts",
                    "mime": "audio/wav", "seq": seq,
                })
            return

        try:
            await websocket.send_json({"type": "response", "text": text})
            await broadcast_to_ui(state, {"type": "response", "text": text})
            if state.mqtt_bridge:
                await state.mqtt_bridge.publish_last_response(text)
            if audio_bytes:
                await websocket.send_json({"type": "tts_start", "size": len(audio_bytes)})
                pipeline.set_ai_speaking(True)
                log.info(f"AI speaking: True (sending {len(audio_bytes)} bytes TTS)")
                chunk_size = 8192
                for i in range(0, len(audio_bytes), chunk_size):
                    await websocket.send_bytes(audio_bytes[i : i + chunk_size])
                await websocket.send_json({"type": "tts_end"})
        except Exception as e:
            log.error(f"Error sending response: {e}")
            # Ensure ai_speaking is cleared even on send failure
            pipeline.set_ai_speaking(False)
            log.info("AI speaking: False (error recovery)")

    async def on_state_change(new_state: str):
        """Callback: broadcast state changes to RPi, UI and MQTT — or, for a
        satellite-origin turn, ONLY to the originating phone (so the kiosk orb
        doesn't react to a phone command)."""
        origin = getattr(state.conversation, "_turn_origin", None)
        if origin is not None:
            from .main import send_to_device
            await send_to_device(state, origin, {"type": "state", "state": new_state})
            return

        msg = {"type": "state", "state": new_state}
        try:
            await websocket.send_json(msg)
        except Exception:
            pass
        await broadcast_to_ui(state, msg)
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_state(new_state)
        # Optional Shape C: explicitly pause/resume the Sendspin MA
        # player around TTS instead of relying on Pulse role-ducking.
        # Only resumes if we were the ones who paused it — never
        # overrides a user's manual pause.
        if state.sendspin_pause_during_tts and state.sendspin_player_entity:
            if new_state == "speaking":
                asyncio.create_task(_ma_pause_if_playing(state))
            elif new_state == "idle":
                asyncio.create_task(_ma_resume_if_we_paused(state))

    async def on_metrics(metrics: dict):
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_task_metrics(metrics)
            model = metrics.get("model", "")
            engine = "openclaw" if model == "openclaw" else "ollama"
            await state.mqtt_bridge.publish_conversation_engine(
                engine,
                duration_s=metrics.get("llm_total_s", 0.0),
                model=model,
            )

    conversation.on_response = on_response
    conversation.on_wake_word = on_wake_word
    conversation.on_state_change = on_state_change
    conversation.on_metrics = on_metrics

    # Keepalive: ping RPi every 15s, detect dead connections
    last_pong = time.monotonic()
    ping_interval = 15.0
    pong_timeout = 45.0  # 3 missed pongs = dead

    async def _keepalive():
        nonlocal last_pong
        while True:
            await asyncio.sleep(ping_interval)
            try:
                await websocket.send_json({"type": "ping"})
                elapsed = time.monotonic() - last_pong
                if elapsed > pong_timeout:
                    # Zombie connection — TCP still open but no pongs.
                    # Close the WS so the handler's receive loop exits
                    # and the client sees ConnectionClosed and reconnects.
                    log.error(
                        f"No pong from RPi in {elapsed:.0f}s — closing zombie WS"
                    )
                    try:
                        await websocket.close(code=1011, reason="pong-timeout")
                    except Exception as ce:
                        log.warning(f"Keepalive: ws.close raised {ce}")
                    break
            except Exception as e:
                log.error(f"Keepalive send failed: {e}")
                break

    # Watchdog: independent task that proves the event loop is alive
    chunk_at_last_watchdog = 0
    async def _watchdog():
        nonlocal chunk_at_last_watchdog
        while True:
            await asyncio.sleep(30)
            current_chunks = pipeline._chunk_count
            chunks_delta = current_chunks - chunk_at_last_watchdog
            chunk_at_last_watchdog = current_chunks
            log.info(f"Watchdog: event_loop=alive, chunks_delta={chunks_delta}/30s, "
                      f"ai_speaking={pipeline._ai_speaking}, "
                      f"vad_executor_alive={not pipeline._vad_executor._shutdown}, "
                      f"reload_pending={pipeline._reload_pending}")

    keepalive_task = asyncio.create_task(_keepalive())
    watchdog_task = asyncio.create_task(_watchdog())

    chunk_count = 0

    try:
        while True:
            log.debug("Waiting for WebSocket data...")
            data = await websocket.receive()
            chunk_count += 1

            if "bytes" in data:
                audio_chunk = data["bytes"]
                t0 = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        pipeline.process_chunk(audio_chunk),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    log.error(f"process_chunk timed out (10s) at chunk #{chunk_count} — skipping")
                    continue

                elapsed = time.monotonic() - t0
                if elapsed > 1.0:
                    log.warning(f"process_chunk slow: {elapsed:.2f}s at chunk #{chunk_count}")

                if result:
                    transcription_msg = {
                        "type": "transcription",
                        "text": result["text"],
                        "is_partial": result.get("is_partial", False),
                        "speaker": result.get("speaker", "unknown"),
                    }
                    await broadcast_to_ui(state, transcription_msg)
                    await websocket.send_json(transcription_msg)

                    if not result.get("is_partial", False) and result.get("speaker") != "ai":
                        await conversation.process_text(result["text"])

                    if result.get("silence_after", False):
                        asyncio.create_task(conversation.on_silence())

            elif "text" in data:
                msg = json.loads(data["text"])
                msg_type = msg.get("type")
                if msg_type == "tts_finished":
                    log.info("AI speaking: False (tts_finished received)")
                    pipeline.set_ai_speaking(False)
                elif msg_type == "pong":
                    last_pong = time.monotonic()
                    log.debug("Pong received from RPi")
                elif msg_type == "mute_sync":
                    state.mic_muted = bool(msg.get("muted", False))
                    log.debug(f"Cached mute state: {state.mic_muted}")
                    if state.mqtt_bridge:
                        await state.mqtt_bridge.publish_mute(state.mic_muted)
                elif msg_type == "volume_sync":
                    try:
                        state.tts_volume = max(0.0, min(1.0, float(msg.get("level", 0.7))))
                        if state.mqtt_bridge:
                            await state.mqtt_bridge.publish_volume(state.tts_volume)
                    except (TypeError, ValueError):
                        pass
                elif msg_type == "snapshot":
                    # RPi pushed a JPEG snapshot — forward to MQTT
                    pass  # snapshot is binary; handled via dedicated message below
                elif msg_type == "ma_volume_adjust":
                    try:
                        step = float(msg.get("step", 0.0))
                    except (TypeError, ValueError):
                        step = 0.0
                    if step != 0.0:
                        asyncio.create_task(_ma_volume_step(state, step))
                elif msg_type == "photo_frame_dismissed":
                    # Kiosk auto-dismissed (state change / volume / pointer /
                    # preempt). Tear down the HA subscription so we don't
                    # keep receiving state_changed events for nothing.
                    from .photo_frame import stop_photo_frame
                    reason = str(msg.get("reason") or "kiosk")
                    await stop_photo_frame(state, reason=reason)
                    # Treat dismissal as user activity so the idle loop
                    # doesn't immediately re-arm the frame on the next tick.
                    _record_user_activity(state)

                elif msg_type == "photo_frame_video_error":
                    # The kiosk couldn't load/autoplay the looping video.
                    # Fall back to cycling photos for this activation.
                    from .photo_frame import handle_video_load_error
                    await handle_video_load_error(state)

                elif msg_type == "display_state":
                    # Confirmation from the RPi container after a
                    # display_set call (or a periodic probe). Keep our
                    # server-side state in sync and republish to HA so
                    # the switch reflects reality.
                    reported = (str(msg.get("state") or "")).lower()
                    available = bool(msg.get("available", True))
                    state.display_available = available
                    if reported in ("on", "off") and reported != state.display_state:
                        state.display_state = reported
                    if state.mqtt_bridge:
                        try:
                            await state.mqtt_bridge.publish_display_state(state.display_state)
                        except Exception as e:
                            log.debug(f"publish_display_state from upstream relay failed: {e}")

                elif msg_type == "webrtc_signal":
                    # Kiosk → server signaling for an active stream session.
                    # offer is sent on stream start; candidate is sent during ICE.
                    session_id = str(msg.get("session_id", ""))
                    kind = msg.get("kind")
                    if kind == "offer" and msg.get("sdp"):
                        if state.active_stream and state.active_stream.get("session_id") == session_id:
                            stream_kind = state.active_stream.get("kind", "ha")
                            if stream_kind == "rtsp":
                                asyncio.create_task(
                                    _negotiate_rtsp_offer(state, session_id, msg["sdp"])
                                )
                            else:
                                entity_id = state.active_stream.get("entity_id", "")
                                asyncio.create_task(
                                    _start_webrtc_stream(state, entity_id, session_id, msg["sdp"])
                                )
                    elif kind == "candidate":
                        # Only HA streams use trickle ICE; go2rtc bundles candidates
                        # into the offer/answer so kiosk-side trickle is dropped.
                        if (
                            state.active_stream
                            and state.active_stream.get("session_id") == session_id
                            and state.active_stream.get("kind") != "rtsp"
                        ):
                            asyncio.create_task(_forward_kiosk_candidate(state, session_id, msg))

    except (WebSocketDisconnect, RuntimeError):
        log.info("Audio client disconnected")
    except Exception as e:
        log.error(f"Audio WebSocket error: {e}", exc_info=True)
    finally:
        state.audio_websocket = None
        keepalive_task.cancel()
        watchdog_task.cancel()


@router.websocket("/ws/ptt")
async def ptt_endpoint(websocket: WebSocket):
    """Persistent WebSocket for low-latency Push-to-Talk triggers.

    Apps that press the button repeatedly (e.g. a desktop client) keep
    one connection open and send JSON messages `{"type":"start"}`,
    `{"type":"end"}`, or `{"type":"cancel"}`. Each maps onto the same
    `start_ptt` / `end_ptt` server functions that drive the HTTP +
    MQTT trigger surfaces, so behaviour is identical."""
    from .main import _get_state
    from .ptt import start_ptt, end_ptt
    state = _get_state(websocket.app)
    await websocket.accept()
    log.info("PTT WebSocket client connected")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = (msg.get("type") or "").lower()
            if t == "start":
                result = await start_ptt(state)
            elif t == "end":
                result = await end_ptt(state)
            elif t == "cancel":
                result = await end_ptt(state, cancel=True)
            else:
                result = {"status": "unknown_type", "type": t}
            try:
                await websocket.send_json(result)
            except Exception:
                pass
    except WebSocketDisconnect:
        log.info("PTT WebSocket client disconnected")
    except Exception as e:
        log.warning(f"PTT WebSocket error: {e}")


@router.websocket("/ws/ui")
async def ui_endpoint(websocket: WebSocket):
    """WebSocket for the web UI to receive live transcription and state updates."""
    from .main import _get_state
    from .pairing import require_token_enabled
    state = _get_state(websocket.app)

    # A /ws/ui connection presenting a VALID pairing token is a SATELLITE (a
    # paired phone): its turns route only to it and it's excluded from the global
    # broadcast. A tokenless connection is a mirror (web dev UI). The token is
    # read unconditionally so classification works regardless of HAL_REQUIRE_TOKEN.
    token = websocket.query_params.get("token")
    is_satellite = bool(token) and state.pairing is not None and state.pairing.is_valid_token(token)

    # Auth gate (opt-in): when HAL_REQUIRE_TOKEN is set, a valid token is required.
    if require_token_enabled() and not is_satellite:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    state.ui_clients.add(websocket)
    if is_satellite:
        # One live satellite per token — replace/close any prior connection.
        old = state.satellite_ws.get(token)
        if old is not None and old is not websocket:
            state.satellite_ws_tokens.pop(old, None)
            try:
                await old.close(code=4409)
            except Exception:
                pass
        state.satellite_ws[token] = websocket
        state.satellite_ws_tokens[websocket] = token
    log.info(f"UI client connected (satellite={is_satellite}, total: {len(state.ui_clients)})")

    if state.conversation:
        await websocket.send_json({
            "type": "state",
            "state": state.conversation.state,
            "wake_word": state.conversation.wake_word,
        })

    # Push the active theme so a freshly-connected client (e.g. the mobile app,
    # which has no local history) matches the display instead of defaulting to
    # its stored 'dark'. The kiosk persists its own theme in localStorage, but
    # mobile relies on this.
    try:
        await websocket.send_json({"type": "set_theme", "name": state.current_theme})
    except Exception:
        pass

    # Satellites also get the household's ACTIVE visuals replayed (live stream /
    # snapshot / video / calendar). Mobile websockets flap (background, VPN,
    # cellular) — without this, a force action fired during a gap is lost.
    if is_satellite:
        from .main import replay_visual_state
        await replay_visual_state(state, websocket)

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            mtype = msg.get("type")
            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
            elif mtype == "webrtc_signal" and is_satellite:
                # A satellite consuming a fanned-out live stream. Two stream kinds:
                #   rtsp — go2rtc, stateless per-offer (non-trickle): negotiate the
                #          offer and answer back over THIS satellite's socket.
                #   ha   — Home Assistant camera (trickle ICE): start a per-peer HA
                #          subscription for the offer and relay this peer's ICE
                #          candidates to HA, with answer/candidates routed back here.
                session_id = str(msg.get("session_id", ""))
                sess = state.active_stream
                kind = sess.get("kind") if sess else None
                if sess and sess.get("session_id") == session_id:
                    signal_kind = msg.get("kind")
                    if signal_kind == "offer" and msg.get("sdp"):
                        if kind == "rtsp":
                            from .main import _negotiate_rtsp_offer
                            await _negotiate_rtsp_offer(
                                state, session_id, msg["sdp"], reply_ws=websocket,
                            )
                        elif kind == "ha":
                            from .main import _start_webrtc_stream
                            asyncio.create_task(_start_webrtc_stream(
                                state, sess.get("entity_id", ""), session_id,
                                msg["sdp"], reply_ws=websocket,
                            ))
                    elif signal_kind == "candidate" and kind == "ha":
                        from .main import _forward_kiosk_candidate
                        asyncio.create_task(_forward_kiosk_candidate(
                            state, session_id, msg, reply_ws=websocket,
                        ))
    except WebSocketDisconnect:
        pass
    finally:
        state.ui_clients.discard(websocket)
        # Satellite cleanup: deregister (only if this ws is still the registered
        # one — a newer reconnect may have replaced it), drop its TTS cache, and
        # tear down any per-device photo-frame session.
        tok = state.satellite_ws_tokens.pop(websocket, None)
        if tok is not None:
            if state.satellite_ws.get(tok) is websocket:
                state.satellite_ws.pop(tok, None)
            state.satellite_tts.pop(tok, None)
            if tok in state.satellite_photo_sessions:
                try:
                    from .photo_frame import stop_photo_frame_for_device
                    await stop_photo_frame_for_device(state, tok, reason="disconnect")
                except Exception as e:
                    log.debug(f"satellite photo cleanup failed: {e}")
        log.info(f"UI client disconnected (total: {len(state.ui_clients)})")
