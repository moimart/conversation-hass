"""LocalToolsClient construction — all the in-process MCP tool registrations.

Extracted from main.py. ``build_local_tools(state)`` builds and returns the
``LocalToolsClient`` populated with HAL's in-process tools: theme picker,
volume / mute, sun times, verbatim speech, camera / image / video / RTSP,
calendar overlay, photo frame, display power, and the idle-minutes setter.

Cross-references to main.py helpers (apply_theme, _push_to_rpi,
_fetch_ha_image_entity, _stop_active_stream/_video, _ensure_go2rtc,
_VALID_CAL_VIEWS, _show_calendar, _hide_calendar, _set_display, broadcast_to_ui,
_theme_names) are imported *lazily inside* ``build_local_tools`` to avoid the
circular import that would otherwise happen (main.py imports this module).
Dispatchers and image push live in ``.media``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid

from .local_tools import LocalToolsClient

log = logging.getLogger("hal.local_tools_register")


def build_local_tools(state) -> LocalToolsClient:
    """Construct the LocalToolsClient with all tools wired to AppState."""
    # Late imports — these would deadlock at module top because main.py
    # imports this module during its own load. By the time build_local_tools
    # is called (from the lifespan), main.py is fully defined.
    from .main import (
        _VALID_CAL_VIEWS,
        _ensure_go2rtc,
        _fetch_ha_image_entity,
        _hide_calendar,
        _push_to_rpi,
        _set_display,
        _show_calendar,
        _stop_active_stream,
        _stop_active_video,
        _theme_names,
        apply_theme,
        broadcast_to_ui,
    )
    from .media import (
        _dispatch_play_video,
        _dispatch_show_image,
        _push_image_payload,
    )

    tools = LocalToolsClient()

    async def set_theme(args: dict) -> str:
        name = (args.get("name") or "").lower().strip()
        valid = _theme_names(state)
        if name not in valid:
            return f"Invalid theme '{name}'. Valid: {', '.join(valid)}"
        ok = await apply_theme(state, name)
        return f"Theme set to {name}" if ok else "Failed to apply theme"

    tools.register(
        "ui_set_theme",
        "Change the HAL web UI theme. Use 'dark' at night/dusk, 'birch' or 'japandi' for light rooms during the day, 'odyssey' for a sterile white look.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": _theme_names(state), "description": "Theme name"},
            },
            "required": ["name"],
        },
        set_theme,
    )

    async def get_current_theme(_args: dict) -> str:
        return f"Current theme: {state.current_theme}"

    tools.register(
        "ui_get_theme",
        "Get the currently active HAL UI theme.",
        {"type": "object", "properties": {}},
        get_current_theme,
    )

    async def set_volume(args: dict) -> str:
        try:
            level = float(args.get("level", 0.7))
        except (TypeError, ValueError):
            return "Volume level must be a number between 0 and 1"
        level = max(0.0, min(1.0, level))
        ok = await _push_to_rpi(state, {"type": "volume", "level": level})
        return f"Volume set to {level:.0%}" if ok else "RPi not connected"

    tools.register(
        "audio_set_volume",
        "Set the speaker volume on the Raspberry Pi (0.0 = silent, 1.0 = max).",
        {
            "type": "object",
            "properties": {
                "level": {"type": "number", "minimum": 0, "maximum": 1, "description": "Volume level"},
            },
            "required": ["level"],
        },
        set_volume,
    )

    async def adjust_volume(args: dict) -> str:
        direction = (args.get("direction") or "").lower()
        if direction not in ("up", "down"):
            return "direction must be 'up' or 'down'"
        try:
            step = float(args.get("step", 0.1))
        except (TypeError, ValueError):
            step = 0.1
        if direction == "down":
            step = -abs(step)
        else:
            step = abs(step)
        ok = await _push_to_rpi(state, {"type": "volume_adjust", "step": step})
        return f"Volume {direction} by {abs(step):.0%}" if ok else "RPi not connected"

    tools.register(
        "audio_adjust_volume",
        "Adjust the speaker volume up or down by a small step (default 10%).",
        {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "step": {"type": "number", "default": 0.1},
            },
            "required": ["direction"],
        },
        adjust_volume,
    )

    async def mute(_args: dict) -> str:
        ok = await _push_to_rpi(state, {"type": "mute_toggle"})
        return "Mic mute toggled" if ok else "RPi not connected"

    tools.register(
        "audio_toggle_mute",
        "Toggle the microphone mute on the Raspberry Pi.",
        {"type": "object", "properties": {}},
        mute,
    )

    async def get_sun_times(_args: dict) -> str:
        """Query Home Assistant's sun.sun entity for sunrise/sunset/dusk."""
        # Find an MCP server that has ha_get_state
        if not state.mcp_client or "ha_get_state" not in state.mcp_client.tool_names:
            return "Home Assistant integration not available"
        result = await state.mcp_client.call_tool("ha_get_state", {"entity_id": "sun.sun"})
        return result

    tools.register(
        "get_sun_times",
        "Get sunrise, sunset, dusk and dawn times from Home Assistant for the current location.",
        {"type": "object", "properties": {}},
        get_sun_times,
    )

    async def speak_verbatim(args: dict) -> str:
        """Speak the given text out loud through the RPi, exactly as written.

        Bypasses the natural-language reply: synthesizes TTS for the
        provided text and pushes it directly to the RPi audio websocket.
        Sets a flag so the LLM's final text response does not get spoken
        again (avoids double audio).
        """
        text = (args.get("text") or "").strip()
        if not text:
            return "Cannot speak empty text"

        if not state.tts_engine:
            return "TTS engine not available"

        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot play audio"

        # Synthesize the verbatim text
        try:
            audio_bytes = await state.tts_engine.synthesize(text)
        except Exception as e:
            return f"TTS synthesis failed: {e}"

        if not audio_bytes:
            return "TTS produced no audio"

        # Send the verbatim text to UI clients as the response
        msg = {"type": "response", "text": text}
        try:
            await ws.send_json(msg)
        except Exception:
            pass
        await broadcast_to_ui(state, msg)
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_last_response(text)

        # Push audio to RPi using the same protocol as on_response
        try:
            await ws.send_json({"type": "tts_start", "size": len(audio_bytes)})
            if state.pipeline:
                state.pipeline.set_ai_speaking(True)
            chunk_size = 8192
            for i in range(0, len(audio_bytes), chunk_size):
                await ws.send_bytes(audio_bytes[i : i + chunk_size])
            await ws.send_json({"type": "tts_end"})
        except Exception as e:
            if state.pipeline:
                state.pipeline.set_ai_speaking(False)
            return f"Failed to send audio: {e}"

        # Suppress the final TTS so the LLM's wrap-up doesn't get spoken too
        if state.conversation:
            state.conversation._suppress_final_tts = True

        return f"Spoke verbatim: {text[:80]}"

    tools.register(
        "speak_verbatim",
        "Speak text out loud through HAL's speaker, exactly as written. Use this when the user asks to 'say X out loud', 'repeat X', 'speak X exactly', or any request that needs the precise wording vocalized. After calling this, your text response will NOT be spoken (only the verbatim text is heard), so keep your text reply minimal — the user has already heard the words.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The exact text to speak out loud"},
            },
            "required": ["text"],
        },
        speak_verbatim,
    )

    async def show_camera(args: dict) -> str:
        entity_id = (args.get("entity_id") or "").strip()
        if not (entity_id.startswith("camera.") or entity_id.startswith("image.")):
            return "entity_id must be a camera.* or image.* entity"
        try:
            duration_s = int(args.get("duration_s", 150))
        except (TypeError, ValueError):
            duration_s = 150
        duration_s = max(5, min(900, duration_s))

        # camera.* — fetch via MCP (returns base64 already)
        if entity_id.startswith("camera."):
            if not state.mcp_client or "ha_get_camera_image" not in state.mcp_client.tool_names:
                return "Camera fetch unavailable (Home Assistant MCP not connected)"
            content = await state.mcp_client.call_tool_content(
                "ha_get_camera_image", {"entity_id": entity_id}
            )
            image_b64 = ""
            mime = "image/jpeg"
            for item in content:
                data = getattr(item, "data", None)
                if data:
                    image_b64 = data
                    mime = getattr(item, "mimeType", None) or mime
                    break
            if not image_b64:
                return f"No image returned for {entity_id}"
            await _dispatch_show_image(state, image_b64, mime, duration_s, entity_id=entity_id)
            return f"Showing {entity_id} on the orb for {duration_s} seconds"

        # image.* — fetch via HA REST /api/image_proxy + bearer
        fetched = await _fetch_ha_image_entity(entity_id)
        if not fetched:
            return f"Could not fetch {entity_id}"
        raw, mime = fetched
        await _dispatch_show_image(
            state, base64.b64encode(raw).decode("ascii"), mime, duration_s, entity_id=entity_id,
        )
        return f"Showing {entity_id} on the orb for {duration_s} seconds"

    tools.register(
        "show_camera",
        "Display a Home Assistant camera snapshot OR image entity inside HAL's orb on the kiosk. Accepts both camera.* (fetched via MCP) and image.* (fetched via HA REST /api/image_proxy with bearer token). Use when the user asks to 'show me the <camera>' or 'show the <image entity>'. The image is shown for duration_s seconds (default 150) then the orb returns to normal. To find the right entity_id, use ha_search_entities with domain=camera or domain=image first.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "camera.* or image.* entity_id, e.g. camera.front_door or image.weather_radar"},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 900, "default": 150, "description": "How long to keep the image on screen, in seconds"},
            },
            "required": ["entity_id"],
        },
        show_camera,
    )

    async def show_image(args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "url must start with http:// or https://"
        try:
            duration_s = int(args.get("duration_s", 60))
        except (TypeError, ValueError):
            duration_s = 60
        duration_s = max(5, min(600, duration_s))
        result = await _push_image_payload(state, url, default_duration=duration_s)
        return result

    tools.register(
        "show_image",
        "Display an arbitrary image (by URL) inside HAL's orb. Use when the user asks to 'show a picture of X', 'put X on screen', or 'display the photo at <url>'. Default duration is 60 s; pass duration_s for longer or shorter. Replaces any active camera snapshot or stream.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http(s) URL of the image"},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 60, "description": "How long to show the image, in seconds"},
            },
            "required": ["url"],
        },
        show_image,
    )

    async def stream_camera(args: dict) -> str:
        entity_id = (args.get("entity_id") or "").strip()
        if not entity_id.startswith("camera."):
            return "entity_id must be a camera.* entity"
        try:
            duration_s = int(args.get("duration_s", 300))
        except (TypeError, ValueError):
            duration_s = 300
        duration_s = max(10, min(1800, duration_s))

        if not os.environ.get("HA_URL") or not os.environ.get("HA_TOKEN"):
            return "Live streaming unavailable (HA_URL/HA_TOKEN not configured)"

        # Replace any active modality (snapshot or stream)
        await _stop_active_stream(state)
        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot start stream"

        session_id = uuid.uuid4().hex

        async def safety_stop():
            try:
                await asyncio.sleep(duration_s)
                await _stop_active_stream(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        state.active_stream = {
            "session_id": session_id,
            "kind": "ha",
            "entity_id": entity_id,
            "ha_sub_id": None,
            "safety_task": asyncio.create_task(safety_stop()),
        }

        try:
            await ws.send_json({
                "type": "stream_start",
                "session_id": session_id,
                "entity_id": entity_id,
                "mode": "trickle",
            })
        except Exception as e:
            await _stop_active_stream(state, notify_kiosk=False)
            return f"Failed to notify kiosk: {e}"

        return f"Streaming {entity_id} live for up to {duration_s} seconds — say 'stop streaming' to end early"

    tools.register(
        "stream_camera",
        "Start a live WebRTC video stream from a Home Assistant camera and display it inside HAL's orb. Use when the user asks to 'watch <camera> live', 'stream the <camera>', or 'show <camera> live'. The stream stays up to duration_s seconds (default 300 = 5 minutes); the user can end it earlier with 'stop streaming'. Replaces any active snapshot or stream. To find the right entity_id, use ha_search_entities with domain=camera first.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Camera entity_id, e.g. camera.front_door"},
                "duration_s": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300, "description": "Maximum stream duration in seconds (safety auto-stop)"},
            },
            "required": ["entity_id"],
        },
        stream_camera,
    )

    async def stream_rtsp(args: dict) -> str:
        rtsp_url = (args.get("rtsp_url") or "").strip()
        if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
            return "rtsp_url must start with rtsp:// or rtsps://"
        try:
            duration_s = int(args.get("duration_s", 300))
        except (TypeError, ValueError):
            duration_s = 300
        duration_s = max(10, min(1800, duration_s))

        client = _ensure_go2rtc(state)
        if not client:
            return "Live RTSP streaming unavailable (GO2RTC_URL not configured)"

        await _stop_active_stream(state)
        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot start stream"

        session_id = uuid.uuid4().hex
        stream_name = f"hal_{session_id}"

        if not await client.register_stream(stream_name, rtsp_url):
            return f"Failed to register RTSP stream with go2rtc"

        async def safety_stop():
            try:
                await asyncio.sleep(duration_s)
                await _stop_active_stream(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        state.active_stream = {
            "session_id": session_id,
            "kind": "rtsp",
            "rtsp_url": rtsp_url,
            "go2rtc_name": stream_name,
            "safety_task": asyncio.create_task(safety_stop()),
        }

        try:
            await ws.send_json({
                "type": "stream_start",
                "session_id": session_id,
                "rtsp_url": rtsp_url,
                # go2rtc's HTTP API is non-trickle: kiosk waits for ICE
                # gathering to complete before sending its bundled offer.
                "mode": "non-trickle",
            })
        except Exception as e:
            await _stop_active_stream(state, notify_kiosk=False)
            return f"Failed to notify kiosk: {e}"

        return f"Streaming RTSP for up to {duration_s} seconds — say 'stop streaming' to end early"

    tools.register(
        "stream_rtsp",
        "Start a live WebRTC video stream from any RTSP URL (any IP camera, NVR, Frigate go2rtc, etc.) and display it inside HAL's orb. Use when the user gives an explicit rtsp:// or rtsps:// URL, or asks to 'stream the RTSP at <url>'. The stream stays up to duration_s seconds (default 300 = 5 minutes); end early with 'stop streaming'. Replaces any active snapshot, image, or stream. RTSP credentials may be inline in the URL (rtsp://user:pass@host/path).",
        {
            "type": "object",
            "properties": {
                "rtsp_url": {"type": "string", "description": "RTSP URL, e.g. rtsp://user:pass@10.0.0.20:554/stream1"},
                "duration_s": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300, "description": "Maximum stream duration in seconds (safety auto-stop)"},
            },
            "required": ["rtsp_url"],
        },
        stream_rtsp,
    )

    async def play_video(args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "url must start with http:// or https://"
        try:
            duration_s = args.get("duration_s")
            duration_s = int(duration_s) if duration_s is not None else None
        except (TypeError, ValueError):
            duration_s = None
        if duration_s is not None:
            duration_s = max(1, min(7200, duration_s))
        loop = bool(args.get("loop", False))
        muted = bool(args.get("muted", False))
        ok = await _dispatch_play_video(
            state, url, duration_s=duration_s, loop=loop, muted=muted
        )
        if not ok:
            return "RPi not connected — kiosk did not receive the play_video message"
        bits = []
        if loop:
            bits.append("looping")
        if muted:
            bits.append("muted")
        if duration_s:
            bits.append(f"max {duration_s}s")
        suffix = f" ({', '.join(bits)})" if bits else ""
        return f"Playing video on the orb{suffix} — say 'stop streaming' to dismiss"

    tools.register(
        "play_video",
        "Play an HTTP(S) video (MP4, WebM, or HLS .m3u8 playlist) inside HAL's orb. Use when the user asks to 'play <video>', 'show this video', 'put on a movie'. Plays once and auto-stops on the video's end event unless `loop=true`. Optional `duration_s` is a hard cap. Audio plays unless `muted=true`; HAL's TTS auto-ducks the video while speaking. Replaces any active snapshot, image, or live stream. End early with stop_streaming.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http(s) URL of the video file or HLS playlist"},
                "duration_s": {"type": "integer", "minimum": 1, "maximum": 7200, "description": "Optional hard cap on playback duration"},
                "loop": {"type": "boolean", "default": False, "description": "Loop forever until stopped"},
                "muted": {"type": "boolean", "default": False, "description": "Start muted"},
            },
            "required": ["url"],
        },
        play_video,
    )

    async def stop_streaming(_args: dict) -> str:
        # Always tell the kiosk to clear video too — videos are kiosk-only
        # state, so state.active_stream tells us nothing about them.
        had_stream = state.active_stream is not None
        label = ""
        if state.active_stream:
            label = state.active_stream.get("entity_id") or state.active_stream.get("rtsp_url") or ""
        await _stop_active_stream(state)
        await _stop_active_video(state)
        if had_stream:
            return f"Stopped streaming {label}" if label else "Stopped video stream"
        return "Stopped any active video on the orb"

    tools.register(
        "stop_streaming",
        "Stop the live camera stream currently playing inside HAL's orb. Call this when the user says any of: 'stop streaming', 'stop the video', 'stop the camera', 'don't show the video anymore', 'hide the live view', 'turn off the camera feed', or anything else that asks to dismiss the live feed.",
        {"type": "object", "properties": {}},
        stop_streaming,
    )

    async def show_calendar_tool(args: dict) -> str:
        view = (args.get("view") or "month").lower().strip()
        if view not in _VALID_CAL_VIEWS:
            return f"view must be one of {_VALID_CAL_VIEWS}"
        calendar_name = (args.get("calendar_name") or "").strip()
        anchor_date = (args.get("anchor_date") or "").strip()
        duration_s = args.get("duration_s")
        try:
            duration_s = int(duration_s) if duration_s is not None else None
        except (TypeError, ValueError):
            duration_s = None
        payload = await _show_calendar(
            state,
            view=view,
            calendar_name=calendar_name,
            duration_s=duration_s,
            anchor_date=anchor_date,
        )
        n = len(payload.get("events") or [])
        src = payload.get("source_label") or "all calendars"
        return f"Showing {view} calendar from {src} for {payload.get('title','?')} ({n} events, {payload['duration_s']}s)"

    tools.register(
        "show_calendar",
        (
            "Display a full-screen calendar overlay on the kiosk (month, week, or "
            "day view) and pull events from Home Assistant. "
            "Use when the user asks 'show me my <name> calendar', 'show the calendar', "
            "'what's on this week', 'show me tomorrow's calendar', "
            "'show me my Family calendar for next Friday', 'what does May 25 look "
            "like', 'show last Monday', etc. "
            "Pass `calendar_name` with the source name (e.g. 'Family', 'Work') — "
            "partial / case-insensitive match against HA calendar entities. "
            "Empty `calendar_name` shows all calendars merged. "
            "Pass `anchor_date` as an ISO date string (YYYY-MM-DD) when the user "
            "names a specific day, week, or month other than today — e.g. for "
            "'tomorrow' compute tomorrow's date from the current date in your "
            "system prompt; for 'next Friday' or 'May 25' compute the absolute "
            "date. With view=day, the overlay shows that single day; with "
            "view=week, the Mon-Sun week containing that date; with view=month, "
            "the whole month containing that date. Omit `anchor_date` for "
            "today / this week / this month. "
            "The overlay rotates back to the orb after duration_s (default from "
            "runtime_config) and is preempted by show_camera / show_image / "
            "play_video / stream_camera."
        ),
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": list(_VALID_CAL_VIEWS), "default": "month", "description": "Calendar view"},
                "calendar_name": {"type": "string", "description": "Optional source calendar name. Empty = merge all HA calendars."},
                "anchor_date": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD) to anchor a specific day/week/month instead of today. Use this for 'tomorrow', 'next Friday', 'May 25', 'last week', etc."},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 600, "description": "How long to show before auto-dismissing"},
            },
        },
        show_calendar_tool,
    )

    async def hide_calendar_tool(_args: dict) -> str:
        await _hide_calendar(state)
        return "Calendar hidden"

    tools.register(
        "hide_calendar",
        "Dismiss the calendar overlay if it is currently shown on the kiosk. Use when the user says 'hide the calendar', 'close the calendar', 'go back to the orb', etc.",
        {"type": "object", "properties": {}},
        hide_calendar_tool,
    )

    async def show_photo_frame_tool(args: dict) -> str:
        from .photo_frame import start_photo_frame
        entity_id = (args.get("entity_id") or "").strip()
        result = await start_photo_frame(state, entity_id=entity_id)
        status = result.get("status")
        if status == "not_configured":
            return (
                "Photo frame entity is not configured. Set photo_frame_entity in "
                "the HAL device's Configuration (or pass entity_id) to use this."
            )
        if status == "invalid_entity":
            return f"{entity_id!r} is not an image.* / camera.* entity — photo frame skipped."
        if status == "fetch_failed":
            return "I couldn't fetch that image from Home Assistant."
        if status == "already_active":
            return "Photo frame was already showing — refreshed the image."
        return "Photo frame shown on the kiosk."

    tools.register(
        "show_photo_frame",
        (
            "Display an ambient full-screen photo frame on the kiosk: a slow "
            "Ken-Burns image from a configurable Home Assistant image.* entity "
            "with the wall clock overlaid in white. Use when the user asks to "
            "'show the photo frame', 'start the photo display', 'show my "
            "pictures', 'put up the slideshow'. Pass `entity_id` only if the "
            "user names a specific image entity; otherwise omit it and the "
            "server uses the configured default (text.<id>_config_photo_frame_entity). "
            "If neither is set, the tool no-ops and tells you so — do NOT "
            "treat that as an error to the user, just explain the entity isn't "
            "configured. The photo frame dismisses itself automatically the "
            "moment ANY kiosk action happens (wake word, PTT, volume, touch, "
            "another overlay), so it's safe to leave running indefinitely."
        ),
        {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Optional HA image.* or camera.* entity_id. Omit to use the configured default.",
                },
            },
        },
        show_photo_frame_tool,
    )

    async def hide_photo_frame_tool(_args: dict) -> str:
        from .photo_frame import stop_photo_frame
        result = await stop_photo_frame(state, reason="explicit")
        if result.get("status") == "not_active":
            return "Photo frame wasn't showing — nothing to hide."
        return "Photo frame hidden."

    tools.register(
        "hide_photo_frame",
        "Dismiss the photo frame if it is currently shown on the kiosk. Use when the user says 'hide the photo frame', 'stop the slideshow', 'close the pictures', etc.",
        {"type": "object", "properties": {}},
        hide_photo_frame_tool,
    )

    async def set_display_power_tool(args: dict) -> str:
        target = (args.get("state") or "").strip().lower()
        if target not in ("on", "off"):
            return "state must be 'on' or 'off'"
        if not state.display_available:
            return "Display power control is not supported on this kiosk (no DPMS backend)."
        ok = await _set_display(state, target)
        if not ok:
            return "Kiosk is not connected; could not change the display power."
        return f"Display turned {target}."

    tools.register(
        "set_display_power",
        (
            "Turn the kiosk display (the screen) on or off using real DPMS. "
            "Use when the user says 'turn off the screen', 'blank the display', "
            "'wake the screen', 'go dark', 'screen on/off', etc. "
            "Argument 'state' must be 'on' or 'off'."
        ),
        {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["on", "off"]},
            },
            "required": ["state"],
        },
        set_display_power_tool,
    )

    async def set_photo_frame_idle_minutes_tool(args: dict) -> str:
        try:
            n = int(args.get("minutes", 0))
        except (TypeError, ValueError):
            return "minutes must be an integer between 0 and 720"
        n = max(0, min(720, n))
        cb = getattr(state.mqtt_bridge, "on_config_photo_frame_idle_minutes", None) if state.mqtt_bridge else None
        if cb is None:
            # No MQTT bridge running — still apply locally + persist so
            # the loop picks it up.
            state.photo_frame_idle_minutes = n
            state.user_last_activity = time.time()
            try:
                state.runtime_config.set("photo_frame_idle_minutes", n)
            except Exception as e:
                log.warning(f"set_photo_frame_idle_minutes: persist failed: {e}")
        else:
            await cb(n)
        if n == 0:
            return "Photo-frame idle auto-activation disabled."
        return f"Photo frame will auto-activate after {n} minutes of inactivity."

    tools.register(
        "set_photo_frame_idle_minutes",
        (
            "Set how many minutes of inactivity must pass before the kiosk "
            "auto-activates the photo frame. 0 disables the feature. "
            "Use when the user says 'auto-show the photo frame after N minutes', "
            "'enable the screensaver after an hour', 'turn off the auto photo frame', etc."
        ),
        {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 0, "maximum": 720},
            },
            "required": ["minutes"],
        },
        set_photo_frame_idle_minutes_tool,
    )

    return tools
