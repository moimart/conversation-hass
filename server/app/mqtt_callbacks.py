"""MQTT bridge callback wiring, extracted from main.lifespan.

`wire(state, bridge)` defines every MQTT command + live-config callback as a
closure over `state`/`bridge`, assigns command callbacks to the bridge's `on_*`
hooks and config callbacks via `bridge.set_config_callback(key, cb)`, seeds the
bridge's discovery caches (`bridge._cached_config[key] = ...`), and registers
the theme-change listener. Called
once during startup. Cross-module helpers are late-imported from `.main` to
dodge the import cycle (same pattern as photo_frame.py / media.py)."""

import asyncio
import json
import logging
import os
import time

log = logging.getLogger("hal")


async def wire(state, bridge) -> None:
    from .main import (
        _push_to_rpi, apply_theme, broadcast_to_ui, _push_image_payload,
        _record_user_activity, _dismiss_photo_frame_async, _query_sun_state,
        _is_valid_theme, _fetch_ollama_context_length, _theme_scheduler,
        ThemeRegistry, VALID_THEMES, _show_calendar, _hide_calendar,
        _download_photo_frame_video, _clear_photo_frame_video_cache,
        _set_display, _handle_openclaw_media,
    )

    async def _mqtt_volume(level: float):
        await _push_to_rpi(state, {"type": "volume", "level": level})
        state.tts_volume = level
        await bridge.publish_volume(level)

    async def _mqtt_mute(target: bool):
        # Only toggle if the cached state differs (avoid flapping)
        if state.mic_muted != target:
            await _push_to_rpi(state, {"type": "mute_toggle"})

    async def _mqtt_theme(theme: str):
        await apply_theme(state, theme)

    async def _mqtt_speak(text: str):
        if not text.strip() or not state.tts_engine:
            return
        ws = state.audio_websocket
        if not ws:
            log.warning("MQTT speak ignored — RPi not connected")
            return
        audio_bytes = await state.tts_engine.synthesize(text)
        if not audio_bytes:
            return
        try:
            # Show in the transcript scroll (persistent history)
            transcript_msg = {
                "type": "transcription",
                "text": text,
                "is_partial": False,
                "speaker": "ai",
            }
            await ws.send_json(transcript_msg)
            await broadcast_to_ui(state, transcript_msg)
            # And in the response panel (large, transient)
            msg = {"type": "response", "text": text}
            await ws.send_json(msg)
            await broadcast_to_ui(state, msg)
            # Force-speak also reaches satellites: text + HAL's voice on each
            # phone (dismisses any idle photo frame first so the text shows).
            from .main import speak_to_satellites
            await speak_to_satellites(state, text, audio_bytes)
            await state.mqtt_bridge.publish_last_response(text)
            if state.conversation_event_logger:
                await state.conversation_event_logger("announcement", text, source="mqtt")
            await ws.send_json({"type": "tts_start", "size": len(audio_bytes)})
            if state.pipeline:
                state.pipeline.set_ai_speaking(True)
            chunk_size = 8192
            for i in range(0, len(audio_bytes), chunk_size):
                await ws.send_bytes(audio_bytes[i : i + chunk_size])
            await ws.send_json({"type": "tts_end"})
        except Exception as e:
            log.error(f"MQTT speak failed: {e}")
            if state.pipeline:
                state.pipeline.set_ai_speaking(False)

    async def _mqtt_image_set(payload):
        try:
            result = await _push_image_payload(state, payload, default_duration=60)
            log.info(f"MQTT image: {result}")
        except Exception as e:
            log.error(f"MQTT image dispatch failed: {e}")

    async def _mqtt_rtsp_set(payload: str):
        text = payload.strip()
        if not text:
            return
        url = ""
        duration_s = 300
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                log.warning(f"MQTT rtsp/set: invalid JSON: {e}")
                return
            url = str(data.get("url") or data.get("rtsp_url") or "")
            try:
                duration_s = int(data.get("duration_s", 300))
            except (TypeError, ValueError):
                pass
        elif text.startswith("rtsp://") or text.startswith("rtsps://"):
            url = text
        else:
            log.warning("MQTT rtsp/set: payload is neither rtsp URL nor JSON")
            return
        if not state.local_tools:
            return
        result = await state.local_tools.call_tool(
            "stream_rtsp", {"rtsp_url": url, "duration_s": duration_s}
        )
        log.info(f"MQTT rtsp: {result}")

    async def _mqtt_camera_set(payload: str):
        text = payload.strip()
        if not text:
            return
        entity_id = ""
        live = False
        duration_s: int | None = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                log.warning(f"MQTT camera/set: invalid JSON: {e}")
                return
            entity_id = str(data.get("entity_id") or "").strip()
            live = bool(data.get("live", False))
            if "duration_s" in data:
                try:
                    duration_s = int(data["duration_s"])
                except (TypeError, ValueError):
                    pass
        elif text.startswith("camera.") or text.startswith("image."):
            entity_id = text
        else:
            log.warning("MQTT camera/set: payload is neither camera.*/image.* entity_id nor JSON")
            return
        if not state.local_tools:
            return
        if not (entity_id.startswith("camera.") or entity_id.startswith("image.")):
            return
        # Live streaming only makes sense for camera.* — ignore the
        # `live` flag silently for image.* entities and fall back to
        # the snapshot path.
        if live and entity_id.startswith("image."):
            log.info(f"MQTT camera/set: ignoring live=true for image entity {entity_id}")
            live = False
        tool = "stream_camera" if live else "show_camera"
        args: dict = {"entity_id": entity_id}
        if duration_s is not None:
            args["duration_s"] = duration_s
        result = await state.local_tools.call_tool(tool, args)
        log.info(f"MQTT camera ({tool}): {result}")

    async def _mqtt_video_set(payload: str):
        text = payload.strip()
        if not text:
            return
        args: dict = {}
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                log.warning(f"MQTT video/set: invalid JSON: {e}")
                return
            args["url"] = str(data.get("url") or "")
            if "duration_s" in data:
                args["duration_s"] = data["duration_s"]
            if "loop" in data:
                args["loop"] = data["loop"]
            if "muted" in data:
                args["muted"] = data["muted"]
        elif text.startswith("http://") or text.startswith("https://"):
            args["url"] = text
        else:
            log.warning("MQTT video/set: payload is neither http(s) URL nor JSON")
            return
        if not state.local_tools or not args.get("url"):
            return
        result = await state.local_tools.call_tool("play_video", args)
        log.info(f"MQTT video: {result}")

    async def _mqtt_command(text: str):
        text = text.strip()
        conversation = state.conversation
        if not text or not conversation:
            return
        log.info(f"MQTT command: {text!r}")
        _record_user_activity(state)
        _dismiss_photo_frame_async(state, reason="command")
        transcription_msg = {
            "type": "transcription",
            "text": text,
            "is_partial": False,
            "speaker": "human",
        }
        await broadcast_to_ui(state, transcription_msg)
        if state.audio_websocket:
            try:
                await state.audio_websocket.send_json(transcription_msg)
            except Exception:
                pass
        conversation._command_buffer.append(text)
        conversation._wake_detected = True
        asyncio.create_task(conversation.on_silence())

    # --- Live runtime-config handlers ---

    async def _persist(key: str, value):
        if state.runtime_config is not None:
            state.runtime_config.set(key, value)

    async def _re_evaluate_active_theme():
        """Recompute which theme should be active and push if changed."""
        if not state.auto_theme:
            return
        sun_state = await _query_sun_state(state)
        target = state.theme_night if sun_state == "below_horizon" else state.theme_day
        if target and target != state.current_theme:
            await apply_theme(state, target)

    async def _cfg_theme_day(name: str):
        if not _is_valid_theme(state, name):
            log.warning(f"config theme_day rejected: {name!r}")
            return
        state.theme_day = name
        await _persist("theme_day", name)
        await bridge.publish_config("theme_day", name)
        await _re_evaluate_active_theme()

    async def _cfg_theme_night(name: str):
        if not _is_valid_theme(state, name):
            log.warning(f"config theme_night rejected: {name!r}")
            return
        state.theme_night = name
        await _persist("theme_night", name)
        await bridge.publish_config("theme_night", name)
        await _re_evaluate_active_theme()

    async def _cfg_tts_voice(name: str):
        if state.voice_options and name not in state.voice_options:
            log.warning(f"config tts_voice rejected: {name!r} not in {state.voice_options}")
            return
        if state.tts_engine is not None:
            state.tts_engine.voice = name
        await _persist("tts_voice", name)
        await bridge.publish_config("tts_voice", name)

    async def _cfg_ollama_model(name: str):
        if state.model_options and name not in state.model_options:
            log.warning(f"config ollama_model rejected: {name!r} not in {state.model_options}")
            return
        if state.conversation is not None:
            state.conversation.ollama_model = name
        await _persist("ollama_model", name)
        await bridge.publish_config("ollama_model", name)
        # Refresh the num_ctx ceiling so the HA Number entity reflects
        # the new model's true context capacity (republishes discovery).
        ctx_max = await _fetch_ollama_context_length(
            os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            name,
        )
        if ctx_max:
            await bridge.update_num_ctx_max(ctx_max)

    async def _cfg_fallback_ollama_model(name: str):
        # Empty string = no fallback. Anything non-empty must be in
        # the discovered model list (when we have one).
        name = (name or "").strip()
        if name and state.model_options and name not in state.model_options:
            log.warning(
                f"config fallback_ollama_model rejected: {name!r} "
                f"not in {state.model_options}"
            )
            return
        if state.conversation is not None:
            state.conversation.fallback_ollama_model = name
        await _persist("fallback_ollama_model", name)
        await bridge.publish_config("fallback_ollama_model", name)

    async def _cfg_num_ctx(value: int):
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = 32768
        n = max(2048, min(int(bridge.num_ctx_max), n))
        if state.conversation is not None:
            state.conversation.num_ctx = n
        await _persist("num_ctx", n)
        await bridge.publish_config("num_ctx", n)

    async def _cfg_wake_word(value: str):
        value = (value or "").strip()
        if state.conversation is not None:
            state.conversation.wake_word = value.lower()
        await _persist("wake_word", value)
        await bridge.publish_config("wake_word", value)

    async def _cfg_auto_theme(enabled: bool):
        state.auto_theme = bool(enabled)
        await _persist("auto_theme", state.auto_theme)
        await bridge.publish_config("auto_theme", state.auto_theme)
        # Start/stop the scheduler to match the new value.
        existing = getattr(state, "theme_scheduler_task", None)
        if state.auto_theme and (existing is None or existing.done()):
            state.theme_scheduler_task = asyncio.create_task(_theme_scheduler(state))
        elif not state.auto_theme and existing is not None and not existing.done():
            existing.cancel()
            state.theme_scheduler_task = None

    bridge.voice_options = list(state.voice_options)
    bridge.model_options = list(state.model_options)
    # Theme options come straight from the plug-in registry. Order
    # them so dark themes sit before light themes; alphabetic within
    # each kind.
    def _ordered_theme_names() -> list[str]:
        if not isinstance(state.themes, ThemeRegistry):
            return sorted(VALID_THEMES)
        dark = sorted(t.name for t in state.themes.themes if t.kind == "dark")
        light = sorted(t.name for t in state.themes.themes if t.kind != "dark")
        return dark + light

    bridge.theme_options = _ordered_theme_names()

    async def _on_themes_changed(_themes_list):
        """Refresh bridge options + republish discovery + tell kiosks."""
        bridge.theme_options = _ordered_theme_names()
        try:
            await bridge.publish_discovery()
        except Exception as e:
            log.warning(f"themes: republish discovery failed: {e}")
        msg = {"type": "themes_changed"}
        ws = state.audio_websocket
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass
        await broadcast_to_ui(state, msg)

    if isinstance(state.themes, ThemeRegistry):
        state.themes.add_listener(_on_themes_changed)
    # Seed the bridge's caches with the current values so the first
    # publish on connect reflects reality.
    bridge._cached_config["theme_day"] = state.theme_day
    bridge._cached_config["theme_night"] = state.theme_night
    bridge._cached_config["tts_voice"] = (
        state.tts_engine.voice if state.tts_engine is not None else ""
    )
    bridge._cached_config["wake_word"] = (
        state.conversation.wake_word if state.conversation is not None else ""
    )
    bridge._cached_config["ollama_model"] = (
        state.conversation.ollama_model if state.conversation is not None else ""
    )
    bridge._cached_config["fallback_ollama_model"] = (
        state.conversation.fallback_ollama_model if state.conversation is not None else ""
    )
    bridge._cached_config["num_ctx"] = int(
        state.conversation.num_ctx if state.conversation is not None
        else state.runtime_config.get("num_ctx", 32768) or 32768
    )
    # Seed num_ctx_max from the current model's native ceiling so the
    # first publish_discovery() uses the right `max`.
    current_model = (
        state.conversation.ollama_model
        if state.conversation is not None else ""
    )
    ctx_max = await _fetch_ollama_context_length(
        os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        current_model,
    )
    if ctx_max:
        bridge.num_ctx_max = ctx_max
        # Clamp the cached current value to the ceiling we just learned.
        if bridge._cached_config["num_ctx"] > ctx_max:
            bridge._cached_config["num_ctx"] = ctx_max
            if state.conversation is not None:
                state.conversation.num_ctx = ctx_max
            await _persist("num_ctx", ctx_max)
    bridge._cached_config["auto_theme"] = state.auto_theme
    bridge._cached_config["calendar_default_source"] = str(
        state.runtime_config.get("calendar_default_source", "") or ""
    )
    bridge._cached_config["timer_name_template"] = str(
        state.runtime_config.get("timer_name_template", "Timer {n}") or "Timer {n}"
    )
    bridge._cached_config["timer_announce_template"] = str(
        state.runtime_config.get("timer_announce_template", "{name} is ready.") or "{name} is ready."
    )
    bridge._cached_config["calendar_dismiss_seconds"] = int(
        state.runtime_config.get("calendar_dismiss_seconds", 30) or 30
    )
    bridge._cached_config["start_muted"] = bool(
        state.runtime_config.get("start_muted", False)
    )
    bridge._cached_config["photo_frame_entity"] = str(
        state.runtime_config.get("photo_frame_entity", "") or ""
    )
    bridge._cached_config["photo_frame_video_url"] = str(
        state.runtime_config.get("photo_frame_video_url", "") or ""
    )
    bridge._cached_config["photo_frame_video_mode"] = bool(
        state.runtime_config.get("photo_frame_video_mode", False)
    )
    bridge._cached_config["photo_frame_show_clock"] = bool(
        state.runtime_config.get("photo_frame_show_clock", True)
    )
    # Display power: pull the timeout from config; the imperative
    # state defaults to "on" (the RPi's first display_state relay
    # after connect will correct it if reality differs).
    state.display_auto_off_seconds = int(
        state.runtime_config.get("display_auto_off_seconds", 0) or 0
    )
    state.display_state = "on"
    state.display_last_activity = time.time()
    bridge._cached_display_state = "on"
    bridge._cached_config["display_auto_off_seconds"] = state.display_auto_off_seconds
    # Photo-frame idle auto-activation.
    state.photo_frame_idle_minutes = int(
        state.runtime_config.get("photo_frame_idle_minutes", 0) or 0
    )
    state.user_last_activity = time.time()
    bridge._cached_config["photo_frame_idle_minutes"] = state.photo_frame_idle_minutes
    # OpenClaw
    bridge._cached_config["openclaw_enabled"] = state.openclaw_enabled
    bridge._cached_config["openclaw_gateway_url"] = state.openclaw_gateway_url
    bridge._cached_config["openclaw_workspace"] = state.openclaw_workspace
    # Display orientation
    bridge._cached_config["display_orientation"] = state.display_orientation
    bridge._cached_config["orb_side"] = state.orb_side
    # Router model
    bridge._cached_config["router_enabled"] = state.router_enabled
    bridge._cached_config["router_model"] = state.router_model
    # Cloud LLM override: options prefetched at startup; switch ALWAYS OFF at
    # boot (never read from runtime config).
    bridge.cloud_model_options = list(state.cloud_model_options)
    bridge._cached_config["cloud_llm_model"] = state.cloud_llm_model
    bridge._cached_config["cloud_llm_enabled"] = False
    async def _mqtt_calendar_show(args: dict):
        view = (args.get("view") or "month").lower()
        calendar_name = (args.get("calendar_name") or "").strip()
        anchor_date = (args.get("anchor_date") or "").strip()
        duration_s = args.get("duration_s")
        try:
            duration_s = int(duration_s) if duration_s is not None else None
        except (TypeError, ValueError):
            duration_s = None
        await _show_calendar(
            state,
            view=view,
            calendar_name=calendar_name,
            duration_s=duration_s,
            anchor_date=anchor_date,
        )

    async def _mqtt_calendar_hide():
        await _hide_calendar(state)

    async def _mqtt_conversation_log_show():
        # HA button → kiosk + web mirrors (phones open theirs via the app
        # button; satellites are deliberately NOT force-opened from here).
        msg = {"type": "show_conversation_log", "duration_s": 30}
        await _push_to_rpi(state, msg)
        await broadcast_to_ui(state, msg)

    async def _mqtt_conversation_log_hide():
        msg = {"type": "hide_conversation_log"}
        await _push_to_rpi(state, msg)
        await broadcast_to_ui(state, msg)

    async def _mqtt_context_clear():
        # Drop PAL's rolling LLM context (history + running summary); refresh
        # the gauge so HA reflects the reset immediately. Long-term Shodh
        # memory is untouched.
        if state.conversation is None:
            return
        stats = await state.conversation.clear_context()
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_context_usage(stats)
        log.info("MQTT: cleared rolling LLM context")

    async def _mqtt_photo_frame_show(args: dict):
        from .photo_frame import start_photo_frame
        entity_id = (args.get("entity_id") or "").strip()
        await start_photo_frame(state, entity_id=entity_id)

    async def _mqtt_photo_frame_hide():
        from .photo_frame import stop_photo_frame
        await stop_photo_frame(state, reason="mqtt")

    async def _cfg_photo_frame_entity(value: str):
        value = (value or "").strip()
        await _persist("photo_frame_entity", value)
        await bridge.publish_config("photo_frame_entity", value)

    async def _cfg_photo_frame_video_url(value: str):
        value = (value or "").strip()
        unchanged = value == state.photo_frame_video_url
        state.photo_frame_video_url = value
        await _persist("photo_frame_video_url", value)
        await bridge.publish_config("photo_frame_video_url", value)
        if unchanged:
            return
        if not value:
            # URL cleared → drop the cache and fall any live loop back
            # to photos.
            _clear_photo_frame_video_cache(state)
            if state.photo_frame_video_session is not None:
                from .photo_frame import stop_photo_frame, start_photo_frame
                await stop_photo_frame(state, reason="video_url_cleared")
                await start_photo_frame(state, "")
            return

        # Download off the MQTT callback path so a large fetch doesn't
        # stall the broker loop; refresh a live loop once it's ready.
        async def _fetch_then_refresh():
            ok = await _download_photo_frame_video(state, value)
            if ok and state.photo_frame_video_session is not None:
                from .photo_frame import start_photo_frame, stop_photo_frame
                await stop_photo_frame(state, reason="video_url_changed")
                await start_photo_frame(state, "")
        asyncio.create_task(_fetch_then_refresh())

    async def _cfg_photo_frame_video_mode(enabled):
        val = str(enabled).strip().lower() in ("true", "1", "yes", "on") \
            if not isinstance(enabled, bool) else enabled
        state.photo_frame_video_mode = bool(val)
        await _persist("photo_frame_video_mode", bool(val))
        await bridge.publish_config("photo_frame_video_mode", bool(val))
        # Apply live: if a frame is currently shown, restart it so it
        # re-picks video vs photo under the new switch.
        if (state.photo_frame_session is not None
                or state.photo_frame_video_session is not None):
            from .photo_frame import stop_photo_frame, start_photo_frame
            await stop_photo_frame(state, reason="video_mode_toggle")
            await start_photo_frame(state, "")

    async def _cfg_photo_frame_show_clock(enabled):
        val = str(enabled).strip().lower() in ("true", "1", "yes", "on") \
            if not isinstance(enabled, bool) else enabled
        state.photo_frame_show_clock = bool(val)
        await _persist("photo_frame_show_clock", bool(val))
        await bridge.publish_config("photo_frame_show_clock", bool(val))
        # Push to the kiosk so it takes effect immediately, even mid-frame
        # (the kiosk just toggles a body class — no frame restart needed).
        msg = {"type": "set_photo_frame_clock", "show": bool(val)}
        await _push_to_rpi(state, msg)
        await broadcast_to_ui(state, msg)

    async def _cfg_calendar_default_source(value: str):
        value = (value or "").strip()
        await _persist("calendar_default_source", value)
        await bridge.publish_config("calendar_default_source", value)

    async def _cfg_timer_name_template(value: str):
        value = (value or "").strip() or "Timer {n}"
        state.timer_name_template = value
        await _persist("timer_name_template", value)
        await bridge.publish_config("timer_name_template", value)

    async def _cfg_timer_announce_template(value: str):
        value = (value or "").strip() or "{name} is ready."
        state.timer_announce_template = value
        await _persist("timer_announce_template", value)
        await bridge.publish_config("timer_announce_template", value)

    async def _cfg_calendar_dismiss_seconds(seconds: int):
        try:
            seconds = max(5, min(600, int(seconds)))
        except (TypeError, ValueError):
            seconds = 30
        await _persist("calendar_dismiss_seconds", seconds)
        await bridge.publish_config("calendar_dismiss_seconds", seconds)

    async def _cfg_start_muted(enabled: bool):
        await _persist("start_muted", bool(enabled))
        await bridge.publish_config("start_muted", bool(enabled))
        # Apply right now too: push the desired state to the RPi if
        # connected, so flipping the HA switch takes effect without
        # waiting for the next reconnect.
        ws = state.audio_websocket
        if ws:
            try:
                await ws.send_json({"type": "mute_set", "muted": bool(enabled)})
            except Exception as e:
                log.warning(f"start_muted: could not push mute_set to RPi: {e}")

    async def _mqtt_display_set(on: bool):
        await _set_display(state, "on" if on else "off")

    async def _cfg_display_auto_off_seconds(seconds: int):
        try:
            n = int(seconds)
        except (TypeError, ValueError):
            n = 0
        n = max(0, min(7200, n))
        state.display_auto_off_seconds = n
        # Activity timestamp resets on config change so the user
        # doesn't get caught by a stale "already idle for 10 min"
        # the instant they enable the feature.
        state.display_last_activity = time.time()
        await _persist("display_auto_off_seconds", n)
        await bridge.publish_config("display_auto_off_seconds", n)

    async def _cfg_photo_frame_idle_minutes(minutes: int):
        try:
            n = int(minutes)
        except (TypeError, ValueError):
            n = 0
        n = max(0, min(720, n))
        state.photo_frame_idle_minutes = n
        # Reset the idle baseline so the user isn't immediately
        # caught by a stale "idle for an hour" the instant they
        # enable the feature.
        state.user_last_activity = time.time()
        await _persist("photo_frame_idle_minutes", n)
        await bridge.publish_config("photo_frame_idle_minutes", n)

    async def _cfg_openclaw_enabled(enabled):
        val = str(enabled).strip().lower() in ("true", "1", "yes", "on")
        state.openclaw_enabled = val
        if val and state.openclaw_gateway_url:
            if state.openclaw_client is None:
                from .openclaw_client import OpenClawClient
                hal_url = f"http://{os.environ.get('HAL_HOST', '0.0.0.0')}:{os.environ.get('PORT', '8765')}"
                state.openclaw_client = OpenClawClient(
                    gateway_url=state.openclaw_gateway_url,
                    hal_base_url=hal_url,
                )
            if state.conversation:
                state.conversation.openclaw_client = state.openclaw_client
                state.conversation.on_openclaw_media = lambda r: _handle_openclaw_media(state, r)
            log.info(f"OpenClaw enabled → {state.openclaw_gateway_url}")
        else:
            if state.conversation:
                state.conversation.openclaw_client = None
                state.conversation.on_openclaw_media = None
            if state.openclaw_client and not val:
                log.info("OpenClaw disabled")
        await _persist("openclaw_enabled", val)
        await bridge.publish_config("openclaw_enabled", val)

    async def _cfg_openclaw_gateway_url(url):
        url = str(url).strip()
        state.openclaw_gateway_url = url
        if state.openclaw_enabled and url:
            if state.openclaw_client:
                await state.openclaw_client.close()
            from .openclaw_client import OpenClawClient
            hal_url = f"http://{os.environ.get('HAL_HOST', '0.0.0.0')}:{os.environ.get('PORT', '8765')}"
            state.openclaw_client = OpenClawClient(
                gateway_url=url, hal_base_url=hal_url,
            )
            if state.conversation:
                state.conversation.openclaw_client = state.openclaw_client
            log.info(f"OpenClaw gateway URL updated → {url}")
        await _persist("openclaw_gateway_url", url)
        await bridge.publish_config("openclaw_gateway_url", url)

    async def _cfg_openclaw_workspace(name):
        name = str(name).strip()
        state.openclaw_workspace = name
        await _persist("openclaw_workspace", name)
        await bridge.publish_config("openclaw_workspace", name)

    bridge.set_config_callback("openclaw_enabled", _cfg_openclaw_enabled)
    bridge.set_config_callback("openclaw_gateway_url", _cfg_openclaw_gateway_url)
    bridge.set_config_callback("openclaw_workspace", _cfg_openclaw_workspace)

    async def _cfg_display_orientation(value: str):
        value = value.strip().lower()
        if value not in ("portrait", "landscape"):
            return
        state.display_orientation = value
        await _persist("display_orientation", value)
        await bridge.publish_config("display_orientation", value)
        msg = {"type": "set_orientation", "orientation": value, "orb_side": state.orb_side}
        await _push_to_rpi(state, msg)

    async def _cfg_orb_side(value: str):
        value = value.strip().lower()
        if value not in ("left", "right"):
            return
        state.orb_side = value
        await _persist("orb_side", value)
        await bridge.publish_config("orb_side", value)
        msg = {"type": "set_orientation", "orientation": state.display_orientation, "orb_side": value}
        await _push_to_rpi(state, msg)

    bridge.set_config_callback("display_orientation", _cfg_display_orientation)
    bridge.set_config_callback("orb_side", _cfg_orb_side)

    async def _cfg_router_enabled(enabled: bool):
        state.router_enabled = bool(enabled)
        if state.conversation is not None:
            state.conversation.router_enabled = state.router_enabled
        await _persist("router_enabled", state.router_enabled)
        await bridge.publish_config("router_enabled", state.router_enabled)

    async def _cfg_router_model(name: str):
        # Empty string = no router model. Anything non-empty must be in
        # the discovered model list (when we have one).
        name = (name or "").strip()
        if name and state.model_options and name not in state.model_options:
            log.warning(
                f"config router_model rejected: {name!r} not in {state.model_options}"
            )
            return
        state.router_model = name
        if state.conversation is not None:
            state.conversation.router_model = name
        await _persist("router_model", name)
        await bridge.publish_config("router_model", name)

    bridge.set_config_callback("router_enabled", _cfg_router_enabled)
    bridge.set_config_callback("router_model", _cfg_router_model)

    # --- Cloud LLM override -------------------------------------------------
    # Switch state is NEVER persisted (boots OFF every restart — deliberate
    # flip required for cloud spend). The model choice persists. Keys live
    # only in runtime/cloud_providers.json / env — never on MQTT.

    async def _refresh_cloud_models() -> None:
        if state.cloud_llm_client is None:
            return
        try:
            opts = await asyncio.wait_for(state.cloud_llm_client.list_models(), timeout=8.0)
            if opts:
                state.cloud_model_options = opts
                bridge.cloud_model_options = list(opts)
                await bridge.publish_discovery()  # re-render the select options
        except Exception as e:
            # keep stale options — one flaky provider shouldn't blank a
            # working dropdown
            log.warning(f"cloud models refresh failed: {type(e).__name__}")

    async def _cfg_cloud_llm_enabled(enabled):
        val = str(enabled).strip().lower() in ("true", "1", "yes", "on")
        state.cloud_llm_enabled = val
        if val:
            if state.cloud_llm_client is None:
                from .cloud_llm import CloudLLMClient, ProviderRegistry, DEFAULT_PROVIDERS_PATH
                state.cloud_llm_client = CloudLLMClient(ProviderRegistry(
                    os.environ.get("CLOUD_PROVIDERS_PATH", DEFAULT_PROVIDERS_PATH)
                ))
            await _refresh_cloud_models()
            if state.conversation is not None:
                state.conversation.cloud_llm = state.cloud_llm_client
                state.conversation.cloud_llm_model = state.cloud_llm_model
            log.info(f"Cloud override ENABLED (model={state.cloud_llm_model or '(none chosen)'})")
        else:
            if state.conversation is not None:
                state.conversation.cloud_llm = None
            log.info("Cloud override disabled")
        # NOTE: deliberately no _persist() — see header comment.
        await bridge.publish_config("cloud_llm_enabled", val)

    async def _cfg_cloud_llm_model(name: str):
        name = (name or "").strip()
        if name.startswith("("):  # the "(no cloud models)" placeholder
            return
        if name and state.cloud_model_options and name not in state.cloud_model_options:
            log.warning(f"config cloud_llm_model rejected: {name!r}")
            return
        state.cloud_llm_model = name
        if state.conversation is not None:
            state.conversation.cloud_llm_model = name
        await _persist("cloud_llm_model", name)
        await bridge.publish_config("cloud_llm_model", name)

    bridge.set_config_callback("cloud_llm_enabled", _cfg_cloud_llm_enabled)
    bridge.set_config_callback("cloud_llm_model", _cfg_cloud_llm_model)

    bridge.on_volume_set = _mqtt_volume
    bridge.on_mute_set = _mqtt_mute
    bridge.on_theme_set = _mqtt_theme
    bridge.on_speak = _mqtt_speak
    bridge.on_command = _mqtt_command
    bridge.on_image_set = _mqtt_image_set
    bridge.on_rtsp_set = _mqtt_rtsp_set
    bridge.on_video_set = _mqtt_video_set
    bridge.on_camera_set = _mqtt_camera_set
    bridge.set_config_callback("theme_day", _cfg_theme_day)
    bridge.set_config_callback("theme_night", _cfg_theme_night)
    bridge.set_config_callback("tts_voice", _cfg_tts_voice)
    bridge.set_config_callback("ollama_model", _cfg_ollama_model)
    bridge.set_config_callback("fallback_ollama_model", _cfg_fallback_ollama_model)
    bridge.set_config_callback("num_ctx", _cfg_num_ctx)
    bridge.set_config_callback("wake_word", _cfg_wake_word)
    bridge.set_config_callback("auto_theme", _cfg_auto_theme)
    bridge.on_calendar_show = _mqtt_calendar_show
    bridge.on_calendar_hide = _mqtt_calendar_hide
    bridge.on_conversation_log_show = _mqtt_conversation_log_show
    bridge.on_conversation_log_hide = _mqtt_conversation_log_hide
    bridge.on_context_clear = _mqtt_context_clear
    bridge.on_photo_frame_show = _mqtt_photo_frame_show
    bridge.on_photo_frame_hide = _mqtt_photo_frame_hide
    bridge.set_config_callback("photo_frame_entity", _cfg_photo_frame_entity)
    bridge.set_config_callback("photo_frame_video_url", _cfg_photo_frame_video_url)
    bridge.set_config_callback("photo_frame_video_mode", _cfg_photo_frame_video_mode)
    bridge.set_config_callback("photo_frame_show_clock", _cfg_photo_frame_show_clock)
    bridge.set_config_callback("calendar_default_source", _cfg_calendar_default_source)
    bridge.set_config_callback("calendar_dismiss_seconds", _cfg_calendar_dismiss_seconds)
    bridge.set_config_callback("timer_name_template", _cfg_timer_name_template)
    bridge.set_config_callback("timer_announce_template", _cfg_timer_announce_template)
    bridge.set_config_callback("start_muted", _cfg_start_muted)
    bridge.on_display_set = _mqtt_display_set
    bridge.set_config_callback("display_auto_off_seconds", _cfg_display_auto_off_seconds)
    bridge.set_config_callback("photo_frame_idle_minutes", _cfg_photo_frame_idle_minutes)

    async def _mqtt_ptt_start():
        from .ptt import start_ptt
        await start_ptt(state)

    async def _mqtt_ptt_end():
        from .ptt import end_ptt
        await end_ptt(state)

    async def _mqtt_ptt_cancel():
        from .ptt import end_ptt
        await end_ptt(state, cancel=True)

    bridge.on_ptt_start = _mqtt_ptt_start
    bridge.on_ptt_end = _mqtt_ptt_end
    bridge.on_ptt_cancel = _mqtt_ptt_cancel
