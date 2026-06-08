"""Media dispatch helpers.

Extracted from main.py. Owns:
- The image/audio/generic-media fetchers (`_fetch_image_url`, `_resolve_media`,
  `_probe_mime`, `_guess_mime_from_url`, `_detect_image_mime`).
- The audio transcoder (`_transcode_to_wav` — ffmpeg).
- The dispatchers that ship media to the kiosk (`_dispatch_show_image`,
  `_dispatch_play_video`, `_dispatch_play_audio`, `_push_image_payload`).
- The OpenClaw-side glue (`_handle_openclaw_media`, `_speak_proactively`).

Cross-references to main.py helpers (``_record_user_activity``,
``_dismiss_photo_frame_async``, ``_stop_active_stream``, ``_stop_active_video``,
``broadcast_to_ui``) are imported *lazily inside function bodies* to avoid the
circular import that would otherwise happen (main.py imports this module at
load time). This mirrors the existing pattern in ``ptt.py`` /
``photo_frame.py`` / ``mcp_server.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import tempfile
from urllib.parse import urlparse

import httpx

log = logging.getLogger("hal.media")

_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB
_IMAGE_FETCH_TIMEOUT = 10.0
_MEDIA_MAX_BYTES = 20 * 1024 * 1024  # 20 MB cap for fetched audio/generic media
_MEDIA_FETCH_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Pure utilities — no AppState, no network back-references.
# ---------------------------------------------------------------------------


def _detect_image_mime(data: bytes) -> str | None:
    """Return the MIME type for a JPEG/PNG/GIF/WebP byte payload, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _guess_mime_from_url(url: str) -> str:
    """Best-effort MIME from a data: URL header or a path extension. "" if unknown."""
    if url.startswith("data:"):
        try:
            return url[5:].split(",", 1)[0].split(";")[0].strip().lower()
        except Exception:
            return ""
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    return (guessed or "").lower()


async def _probe_mime(url: str) -> str:
    """HEAD-probe an HTTP URL for its content-type. Returns "" on failure."""
    headers: dict[str, str] = {}
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if ha_url and ha_token and url.startswith(ha_url):
        headers["Authorization"] = f"Bearer {ha_token}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_IMAGE_FETCH_TIMEOUT) as c:
            r = await c.head(url, headers=headers)
            return (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    except Exception:
        return ""


async def _fetch_image_url(url: str) -> tuple[bytes, str] | None:
    """Fetch an image URL with sane caps. Returns (bytes, mime) or None.

    Auto-attaches the HA bearer token when the URL points at the configured
    HA instance, so /local/ and /api/camera_proxy/ paths work without extra
    config. Refuses non-image content types and anything over 8 MB.
    """
    headers: dict[str, str] = {}
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if ha_url and ha_token and url.startswith(ha_url):
        headers["Authorization"] = f"Bearer {ha_token}"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_IMAGE_FETCH_TIMEOUT,
            max_redirects=3,
        ) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not mime.startswith("image/"):
                log.warning(f"refusing non-image content-type {mime!r} from {url}")
                return None
            data = r.content
            if len(data) > _IMAGE_MAX_BYTES:
                log.warning(f"image at {url} is {len(data)} bytes, cap is {_IMAGE_MAX_BYTES}")
                return None
            return data, mime
    except Exception as e:
        log.warning(f"image fetch failed for {url}: {e}")
        return None


async def _resolve_media(url: str) -> tuple[bytes, str] | None:
    """Resolve a media reference to (bytes, mime).

    Handles data: URIs (decoded inline) and http(s) URLs (fetched, capped at
    _MEDIA_MAX_BYTES). Local file paths are intentionally unsupported — the
    channel plugin inlines those as data: URLs before they reach the server.
    """
    if url.startswith("data:"):
        try:
            header, b64 = url.split(",", 1)
            mime = header[5:].split(";")[0].strip().lower() or "application/octet-stream"
            return base64.b64decode(b64), mime
        except Exception as e:
            log.warning(f"bad data URL: {e}")
            return None

    if url.startswith("http://") or url.startswith("https://"):
        headers: dict[str, str] = {}
        ha_url = os.environ.get("HA_URL", "").rstrip("/")
        ha_token = os.environ.get("HA_TOKEN", "")
        if ha_url and ha_token and url.startswith(ha_url):
            headers["Authorization"] = f"Bearer {ha_token}"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_MEDIA_FETCH_TIMEOUT, max_redirects=3
            ) as client:
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                data = r.content
                if len(data) > _MEDIA_MAX_BYTES:
                    log.warning(f"media at {url[:80]} is {len(data)}B, cap {_MEDIA_MAX_BYTES}")
                    return None
                if not mime:
                    mime = _guess_mime_from_url(url) or "application/octet-stream"
                return data, mime
        except Exception as e:
            log.warning(f"media fetch failed for {url[:80]}: {e}")
            return None

    log.warning(f"unsupported media reference (not http/data): {url[:80]}")
    return None


async def _transcode_to_wav(data: bytes) -> bytes | None:
    """Transcode arbitrary audio bytes to 16-bit PCM WAV via ffmpeg.

    Uses a temp file for output so the WAV header sizes are finalized
    correctly (pipe output leaves placeholder sizes that wave.open mis-reads).
    """
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        outp = os.path.join(d, "out.wav")
        with open(inp, "wb") as f:
            f.write(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", inp, "-acodec", "pcm_s16le", outp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.warning(f"ffmpeg transcode failed: {err.decode('utf-8', 'replace')[:200]}")
                return None
            with open(outp, "rb") as f:
                return f.read()
        except FileNotFoundError:
            log.warning("ffmpeg not found — cannot transcode audio")
            return None
        except Exception as e:
            log.warning(f"audio transcode error: {e}")
            return None


# ---------------------------------------------------------------------------
# Dispatchers — ship media to the kiosk over the audio websocket / broadcast.
# These need a few main.py helpers; imported lazily to avoid the circular
# import that would otherwise occur (main → media → main).
# ---------------------------------------------------------------------------


async def _dispatch_show_image(
    state,
    image_b64: str,
    mime: str,
    duration_s: int,
    *,
    entity_id: str = "external",
    push: bool = True,
) -> bool:
    """Push an image to the kiosk via the existing show_camera WS message.

    Stops any active live stream and any playing video first (mutual
    exclusion). Returns True if we got the message out — even if the kiosk
    websocket is closed (the UI broadcast is best-effort).
    """
    from .main import _record_user_activity, _stop_active_stream, _stop_active_video, broadcast_force_action

    _record_user_activity(state)
    await _stop_active_stream(state)
    await _stop_active_video(state)
    msg = {
        "type": "show_camera",
        "image": image_b64,
        "mime": mime,
        "duration_s": duration_s,
        "entity_id": entity_id,
    }
    ws = state.audio_websocket
    sent = False
    if ws:
        try:
            await ws.send_json(msg)
            sent = True
        except Exception as e:
            log.warning(f"show_image ws send failed: {e}")
    # web mirrors + satellites (force action — image is carried inline, so it
    # renders on phones regardless of network reachability); clear any idle
    # photo frame so the image is visible.
    await broadcast_force_action(state, msg, dismiss_photo=True)
    # Remember it so a satellite that (re)connects mid-display can replay it.
    import time as _time
    state.active_visual = {"msg": msg, "expires": _time.monotonic() + duration_s}
    # Conversation log: persist a thumbnail of what was shown (kind='image',
    # origin = the entity/source label). Fire-and-forget — the PIL downscale
    # runs in a thread and a failure must never affect the display. When push
    # is enabled, the same background task also notifies offline paired devices
    # with an inline thumbnail (needs the log row id, so it logs then pushes);
    # QR codes / internal frames pass push=False to opt out.
    if push and getattr(state, "push", None) is not None:
        asyncio.create_task(_log_and_push_image(state, image_b64, mime, entity_id))
    else:
        asyncio.create_task(_log_orb_image(state, image_b64, mime, entity_id))
    return sent


async def _log_and_push_image(state, image_b64: str, mime: str, entity_id: str) -> None:
    """Log the orb image (capturing its row id) then push an inline-thumbnail
    notification to offline paired devices. If the log is disabled (no id) the
    push degrades to text-only."""
    row_id = await _log_orb_image(state, image_b64, mime, entity_id)
    try:
        await state.push.dispatch(state, "image", "📷 Image shown on the orb",
                                  image_row_id=row_id, category="image")
    except Exception as e:
        log.debug(f"image push failed: {type(e).__name__}: {e}")


_LOG_THUMB_MAX = 512        # px, longest edge of the stored thumbnail


def _make_thumbnail(image_bytes: bytes) -> tuple[bytes, str] | None:
    """Downscale to a JPEG thumbnail (≤ _LOG_THUMB_MAX px on the longest
    edge) so forever-retention stays cheap. Returns None when the bytes
    aren't a decodable image."""
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail((_LOG_THUMB_MAX, _LOG_THUMB_MAX))
        if img.mode not in ("RGB", "L"):
            # JPEG can't carry alpha — flatten onto white (QR codes etc.).
            background = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])
            img = background
        out = BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue(), "image/jpeg"
    except Exception as e:
        log.debug(f"log thumbnail failed: {type(e).__name__}: {e}")
        return None


async def _log_orb_image(state, image_b64: str, mime: str, entity_id: str) -> int | None:
    """Append an image entry to the persistent conversation log. Returns the new
    row id (so a push notification can reference the thumbnail), or None."""
    clog = getattr(state, "conversation_log", None)
    if clog is None or not getattr(clog, "enabled", False):
        return None
    try:
        raw = base64.b64decode(image_b64)
        thumb = await asyncio.to_thread(_make_thumbnail, raw)
        if thumb is None:
            return None
        return await clog.log(
            "image",
            "Image shown on the orb",
            origin=entity_id or None,
            meta={"source_mime": mime, "bytes": len(raw)},
            image=thumb[0],
            image_mime=thumb[1],
        )
    except Exception as e:
        log.debug(f"orb image log failed: {type(e).__name__}: {e}")
        return None


async def _dispatch_play_video(
    state,
    url: str,
    *,
    duration_s: int | None = None,
    loop: bool = False,
    muted: bool = False,
) -> bool:
    """Push a play_video message to the kiosk. Clears any active stream first."""
    from .main import _record_user_activity, _stop_active_stream, broadcast_force_action

    _record_user_activity(state)
    await _stop_active_stream(state)  # mutual exclusion with WebRTC streams
    msg = {
        "type": "play_video",
        "url": url,
        "loop": bool(loop),
        "muted": bool(muted),
    }
    if duration_s is not None:
        msg["duration_s"] = max(1, min(7200, int(duration_s)))
    ws = state.audio_websocket
    sent = False
    if ws:
        try:
            await ws.send_json(msg)
            sent = True
        except Exception as e:
            log.warning(f"play_video ws send failed: {e}")
    # web mirrors + satellites (force action — video URL fetched by each surface);
    # clear any idle photo frame so the video is visible.
    await broadcast_force_action(state, msg, dismiss_photo=True)
    # Remember it so a satellite that (re)connects mid-playback can replay it
    # (no duration → stays replayable until video_stop clears it).
    import time as _time
    state.active_visual = {
        "msg": msg,
        "expires": (_time.monotonic() + msg["duration_s"]) if "duration_s" in msg else None,
    }
    return sent


async def _dispatch_play_audio(state, data: bytes, mime: str) -> bool:
    """Play an arbitrary audio payload through the kiosk speaker.

    Transcodes to WAV and streams it over the existing tts_start/tts_end
    protocol (the same path TTS uses). Returns True if audio was sent.
    """
    from .main import _record_user_activity, _dismiss_photo_frame_async

    wav = await _transcode_to_wav(data)
    if not wav:
        return False

    ws = state.audio_websocket
    if not ws:
        log.warning("audio play requested but no RPi connected")
        return False

    _record_user_activity(state)
    _dismiss_photo_frame_async(state, reason="openclaw_audio")
    try:
        await ws.send_json({"type": "tts_start", "size": len(wav)})
        if state.pipeline:
            state.pipeline.set_ai_speaking(True)
        chunk_size = 8192
        for i in range(0, len(wav), chunk_size):
            await ws.send_bytes(wav[i : i + chunk_size])
        await ws.send_json({"type": "tts_end"})
        log.info(f"OpenClaw audio played ({len(data)}B {mime} -> {len(wav)}B WAV)")
        return True
    except Exception as e:
        if state.pipeline:
            state.pipeline.set_ai_speaking(False)
        log.warning(f"audio send failed: {e}")
        return False


async def _push_image_payload(state, raw: bytes | str, default_duration: int = 60) -> str:
    """Handle a payload from the MQTT image/set topic or text helper.

    Detects the payload form (binary image / URL / JSON wrapper) and
    dispatches it. Returns a short status string for logging.
    """
    duration_s = default_duration
    image_bytes: bytes | None = None
    mime: str | None = None
    image_b64_inline: str | None = None
    url: str | None = None

    if isinstance(raw, (bytes, bytearray)):
        # Binary: image bytes if it looks like one, otherwise try as text.
        detected = _detect_image_mime(bytes(raw))
        if detected:
            image_bytes = bytes(raw)
            mime = detected
        else:
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "unrecognized binary payload"

    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("{"):
            try:
                data = json.loads(s)
            except json.JSONDecodeError as e:
                return f"invalid JSON: {e}"
            try:
                duration_s = int(data.get("duration_s", default_duration))
            except (TypeError, ValueError):
                duration_s = default_duration
            if data.get("url"):
                url = str(data["url"])
            elif data.get("image"):
                image_b64_inline = str(data["image"])
                mime = str(data.get("mime") or "image/jpeg")
        elif s.startswith("http://") or s.startswith("https://"):
            url = s
        else:
            return "payload is neither image bytes, URL, nor JSON"

    duration_s = max(5, min(600, duration_s))

    if url:
        fetched = await _fetch_image_url(url)
        if not fetched:
            return f"fetch failed for {url}"
        image_bytes, mime = fetched

    if image_b64_inline:
        await _dispatch_show_image(state, image_b64_inline, mime or "image/jpeg", duration_s)
        return f"pushed inline base64 ({len(image_b64_inline)} chars) for {duration_s}s"

    if image_bytes and mime:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        await _dispatch_show_image(state, b64, mime, duration_s)
        return f"pushed {len(image_bytes)} bytes ({mime}) for {duration_s}s"

    return "nothing to display"


async def _handle_openclaw_media(state, response) -> bool:
    """Dispatch media from an OpenClaw response.

    Images/video go to the orb; audio plays through the speaker. Media type
    is determined by content-type (with an extension/data-URL fallback) so
    extensionless URLs and inlined data: URLs route correctly. Returns True
    if any audio was played, so the caller can suppress its own text TTS.
    """
    audio_played = False
    shown_visual = False

    for url in (response.media_urls or []):
        try:
            mime = _guess_mime_from_url(url)
            if not mime and url.startswith(("http://", "https://")):
                mime = await _probe_mime(url)

            # Video streams by reference (URL or data: URI) — no download.
            if mime.startswith("video/"):
                if not shown_visual:
                    if await _dispatch_play_video(state, url, duration_s=300):
                        shown_visual = True
                        log.info(f"OpenClaw video started ({mime})")
                continue

            # Everything else needs the bytes.
            resolved = await _resolve_media(url)
            if not resolved:
                continue
            data, resolved_mime = resolved
            mime = (resolved_mime or mime).lower()

            if mime.startswith("image/") and not shown_visual:
                b64 = base64.b64encode(data).decode("ascii")
                if await _dispatch_show_image(state, b64, mime, 60):
                    shown_visual = True
                    log.info(f"OpenClaw image displayed ({len(data)}B {mime})")
            elif mime.startswith("audio/"):
                if await _dispatch_play_audio(state, data, mime):
                    audio_played = True
            elif url.startswith(("http://", "https://")) and not shown_visual:
                # Unknown type with a real link — fall back to a QR code.
                from .qr_code import generate_qr_png
                qr_bytes = generate_qr_png(url)
                if qr_bytes:
                    b64 = base64.b64encode(qr_bytes).decode("ascii")
                    await _dispatch_show_image(
                        state, b64, "image/png", 30, entity_id="qr-code"
                    )
                    shown_visual = True
                    log.info(f"OpenClaw QR code displayed for: {url[:80]}")
        except Exception as e:
            log.warning(f"OpenClaw media dispatch failed for {url[:80]}: {e}")

    return audio_played


async def _speak_proactively(
    state, text: str, media_urls: list[str] | None = None
) -> bool:
    """Speak agent-initiated text through the kiosk without a user turn.

    Backs the OpenClaw channel's outbound path so the agent can push
    reminders/notifications and have HAL say them out loud. Mirrors the
    on_response delivery (text to UI + MQTT, TTS audio to the RPi) and
    relies on tts_start/tts_end to drive the orb's speaking animation.
    """
    from .main import _record_user_activity, _dismiss_photo_frame_async, broadcast_to_ui

    text = (text or "").strip()
    media_urls = media_urls or []

    # Wake the display and clear the photo frame so the message is visible.
    _record_user_activity(state)
    _dismiss_photo_frame_async(state, reason="openclaw_say")

    audio_media_played = False
    if media_urls:
        class _MediaOnly:
            pass
        m = _MediaOnly()
        m.media_urls = media_urls
        try:
            audio_media_played = await _handle_openclaw_media(state, m)
        except Exception as e:
            log.warning(f"proactive media dispatch failed: {e}")

    if not text:
        return True  # media-only push

    # Option A: an attached audio file is the spoken output — show the text
    # but don't also synthesize TTS for it (avoids overlapping audio).
    if audio_media_played:
        msg = {"type": "response", "text": text}
        if state.audio_websocket:
            try:
                await state.audio_websocket.send_json(msg)
            except Exception:
                pass
        await broadcast_to_ui(state, msg)
        # Proactive announcements reach satellites too (text-only here — the
        # spoken output was the attached audio, played on the kiosk).
        from .main import speak_to_satellites
        await speak_to_satellites(state, text, None)
        if state.mqtt_bridge:
            try:
                await state.mqtt_bridge.publish_last_response(text)
            except Exception:
                pass
        if getattr(state, "conversation_event_logger", None):
            await state.conversation_event_logger("announcement", text, source="openclaw")
        return True

    ws = state.audio_websocket

    audio_bytes = None
    if state.tts_engine:
        try:
            audio_bytes = await state.tts_engine.synthesize(text)
        except Exception as e:
            log.warning(f"proactive TTS synthesis failed: {e}")

    msg = {"type": "response", "text": text}
    if ws:
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    await broadcast_to_ui(state, msg)
    # Proactive announcements (OpenClaw /api/openclaw/say) reach satellites:
    # text + HAL's voice on each phone, photo frame dismissed first.
    from .main import speak_to_satellites
    await speak_to_satellites(state, text, audio_bytes)
    if state.mqtt_bridge:
        try:
            await state.mqtt_bridge.publish_last_response(text)
        except Exception:
            pass
    if getattr(state, "conversation_event_logger", None):
        await state.conversation_event_logger("announcement", text, source="openclaw")

    if ws and audio_bytes:
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
            log.warning(f"proactive audio send failed: {e}")

    return True
