"""Native WebRTC endpoint for kiosk intercom calls (aiortc).

NAMING: in the code the PAL display unit is the "kiosk" (KIOSK_ID, the RPi +
audio_streamer + Chromium); to USERS and in the intercom directory it's the
"hub" ("call the hub"). Same device — internal name "kiosk", external name "hub".

The kiosk's *call audio* can't go through Chromium — Chromium's WebRTC audio
can't share the Anker speaker/mic with this RPi's audio stack (confirmed: audio
arrives but never reaches the speaker, and the mic never goes out). So the
audio_streamer — which already owns the Anker correctly (PyAudio) for TTS and
wake-word — becomes the kiosk's native WebRTC peer for calls.

Audio-only for v1 (the phone's video on the orb is a later layer): on a call we
   - capture the mic and send it to the caller (MicTrack, fed by the streamer's
     existing single input stream via a queue → NO second device open), and
   - play the caller's received audio to the speaker (a dedicated PyAudio output
     stream), resampled to the device rate.
The streamer pauses its wake-word capture while `in_call` so the one mic stream
feeds the call instead of the server.

Signaling mirrors the JS client (intercom_invite/accept/offer/answer/candidate/
hangup). aiortc bundles ICE candidates into the offer/answer (non-trickle) and
accepts the phone's trickle candidates via addIceCandidate. The kiosk auto-answers
as callee (no touch) and can also originate (voice "call X" → intercom_call_start).
"""
from __future__ import annotations

import asyncio
import fractions
import io
import logging

import av
import pyaudio
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceCandidate,
    MediaStreamTrack,
)
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger("hal.intercom_peer")

CALL_RATE = 48000          # WebRTC/Opus works at 48 kHz; the Anker supports it


class _MicTrack(MediaStreamTrack):
    """Outbound mic: pulls raw s16-mono frames (at the streamer's capture rate)
    from a queue the streamer fills; aiortc resamples to 48 kHz Opus."""
    kind = "audio"

    def __init__(self, queue: asyncio.Queue, rate: int):
        super().__init__()
        self._queue = queue
        self._rate = rate
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        data = await self._queue.get()           # paced by real-time mic capture
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
        frame.planes[0].update(data)
        frame.sample_rate = self._rate
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, self._rate)
        self._pts += frame.samples
        return frame


class IntercomPeer:
    """One active kiosk call. Owned by the AudioStreamer; driven by intercom_*
    signaling from the server and a `send` callback that ships signaling back up."""

    def __init__(self, streamer, send_up, show_frame=None):
        self._streamer = streamer
        self._send_up = send_up                  # async fn(dict) → server (/ws/audio)
        self._show_frame = show_frame            # async fn(jpeg_bytes) → kiosk orb
        self.session_id: str | None = None
        self.role: str | None = None             # "caller" | "callee"
        self._pc: RTCPeerConnection | None = None
        self._mic_queue: asyncio.Queue | None = None
        self._out_stream = None
        self._play_task: asyncio.Task | None = None
        self._video_task: asyncio.Task | None = None
        self._closing = False

    # --- streamer hook: feed a captured mic frame (called while in_call) -------
    def feed_mic(self, pcm_s16_mono: bytes) -> None:
        q = self._mic_queue
        if q is None:
            return
        if q.qsize() > 25:                       # ~0.5s — drop stale to bound latency
            try: q.get_nowait()
            except Exception: pass
        try: q.put_nowait(pcm_s16_mono)
        except Exception: pass

    @property
    def active(self) -> bool:
        return self._pc is not None

    # --- signaling entrypoint --------------------------------------------------
    async def handle(self, msg: dict) -> None:
        t = msg.get("type")
        try:
            if t == "intercom_call_start":
                await self._start_outgoing(msg.get("to"), msg.get("to_name"))
            elif t == "intercom_invite":
                await self._on_invite(msg)
            elif t == "intercom_accept":
                await self._on_accept()
            elif t == "intercom_offer":
                await self._on_offer(msg)
            elif t == "intercom_answer":
                await self._on_answer(msg)
            elif t == "intercom_candidate":
                await self._on_candidate(msg)
            elif t in ("intercom_decline", "intercom_busy", "intercom_unavailable",
                       "intercom_hangup", "intercom_voice_hangup"):
                await self.close()
        except Exception as e:
            log.error(f"intercom peer error on {t}: {e}", exc_info=True)
            await self.close()

    # --- caller ----------------------------------------------------------------
    async def _start_outgoing(self, to_id, to_name) -> None:
        if self.active:
            return
        self.role = "caller"
        self.session_id = _rand_id()
        await self._build_pc()
        await self._send_up({
            "type": "intercom_invite", "to": to_id, "session_id": self.session_id,
            "media": {"audio": True, "video": False},
        })

    async def _on_accept(self) -> None:
        if self.role != "caller" or not self._pc:
            return
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)        # gathers candidates
        await self._send_up({"type": "intercom_offer", "session_id": self.session_id,
                             "sdp": self._pc.localDescription.sdp})

    async def _on_answer(self, msg) -> None:
        if not self._pc or not msg.get("sdp"):
            return
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=msg["sdp"], type="answer"))

    # --- callee ----------------------------------------------------------------
    async def _on_invite(self, msg) -> None:
        if self.active:                          # busy
            await self._send_up({"type": "intercom_decline",
                                 "session_id": msg.get("session_id")})
            return
        self.role = "callee"
        self.session_id = msg.get("session_id")
        await self._build_pc()
        # No touch → auto-answer immediately.
        await self._send_up({"type": "intercom_accept", "session_id": self.session_id})

    async def _on_offer(self, msg) -> None:
        if not self._pc or not msg.get("sdp"):
            return
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=msg["sdp"], type="offer"))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)       # gathers candidates
        await self._send_up({"type": "intercom_answer", "session_id": self.session_id,
                             "sdp": self._pc.localDescription.sdp})

    async def _on_candidate(self, msg) -> None:
        if not self._pc or not msg.get("candidate"):
            return
        try:
            cand_str = msg["candidate"]
            if cand_str.startswith("candidate:"):
                cand_str = cand_str[len("candidate:"):]
            c = candidate_from_sdp(cand_str)
            c.sdpMid = msg.get("sdpMid")
            c.sdpMLineIndex = msg.get("sdpMLineIndex")
            await self._pc.addIceCandidate(c)
        except Exception as e:
            log.debug(f"addIceCandidate ignored: {e}")

    # --- peer connection + media ----------------------------------------------
    async def _build_pc(self) -> None:
        self._pc = RTCPeerConnection()
        self._mic_queue = asyncio.Queue()
        self._pc.addTrack(_MicTrack(self._mic_queue, self._streamer.device_rate))

        @self._pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                self._play_task = asyncio.ensure_future(self._play(track))
            elif track.kind == "video" and self._show_frame is not None:
                self._video_task = asyncio.ensure_future(self._pipe_video(track))

        @self._pc.on("connectionstatechange")
        async def _on_state():
            st = self._pc.connectionState if self._pc else "closed"
            log.info(f"intercom peer connection: {st}")
            if st in ("failed", "closed"):
                await self.close()

        # Tell the streamer to route its mic to us (pause wake-word capture).
        self._streamer.enter_call(self)
        log.info(f"intercom peer up (role={self.role}, session={self.session_id})")

    async def _play(self, track) -> None:
        """Play the remote audio track to the speaker via a dedicated PyAudio
        output stream, resampled to the device rate. The blocking device writes
        run on a DEDICATED thread so they're never queued behind video/JPEG work
        (which would underrun the speaker and chop the call)."""
        import concurrent.futures
        out_rate = CALL_RATE
        resampler = av.AudioResampler(format="s16", layout="mono", rate=out_rate)
        loop = asyncio.get_event_loop()
        writer = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            self._out_stream = self._streamer.open_call_output(out_rate)
        except Exception as e:
            log.error(f"intercom: could not open call output: {e}")
            self._out_stream = None
        try:
            while not self._closing:
                frame = await track.recv()
                for rf in resampler.resample(frame):
                    if self._out_stream is None:
                        break
                    data = bytes(rf.planes[0])
                    await loop.run_in_executor(writer, self._safe_write, data)
        except Exception as e:
            log.debug(f"intercom play ended: {e}")
        finally:
            writer.shutdown(wait=False)

    async def _pipe_video(self, track) -> None:
        """Decode the caller's video and push a few JPEG frames/sec to the kiosk
        orb (the existing show_camera path). Throttled + downscaled to keep RPi
        CPU low; aiortc still decodes every frame so the pipeline keeps flowing.
        JPEG encoding runs on a DEDICATED thread so it can't starve the audio
        playback thread (which would glitch the call)."""
        import concurrent.futures
        loop = asyncio.get_event_loop()
        enc = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        n = 0
        try:
            while not self._closing:
                try:
                    frame = await track.recv()
                except Exception:
                    break
                n += 1
                if n % 6:                   # ~4 fps from a 24 fps sender
                    continue
                try:
                    jpeg = await loop.run_in_executor(enc, _frame_to_jpeg, frame)
                    if jpeg:
                        await self._show_frame(jpeg)
                except Exception as e:
                    log.debug(f"video frame failed: {e}")
        finally:
            enc.shutdown(wait=False)

    def _safe_write(self, data: bytes) -> None:
        try:
            if self._out_stream is not None:
                self._out_stream.write(data)
        except Exception as e:
            log.debug(f"call output write failed: {e}")

    # --- teardown --------------------------------------------------------------
    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        sid = self.session_id
        try:
            if self._send_up and sid:
                await self._send_up({"type": "intercom_hangup", "session_id": sid})
        except Exception:
            pass
        if self._play_task:
            self._play_task.cancel()
        if self._video_task:
            self._video_task.cancel()
        if self._out_stream is not None:
            try:
                self._out_stream.stop_stream(); self._out_stream.close()
            except Exception:
                pass
            self._out_stream = None
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
        self._mic_queue = None
        try:
            self._streamer.leave_call(self)      # resume wake-word capture
        except Exception:
            pass
        log.info(f"intercom peer closed (session={sid})")


def _frame_to_jpeg(frame, max_w: int = 360, quality: int = 70) -> bytes | None:
    """av.VideoFrame → downscaled JPEG bytes (via Pillow). None on failure."""
    try:
        img = frame.to_image()                  # PIL Image (RGB)
        if img.width > max_w:
            img = img.resize((max_w, max(1, round(max_w * img.height / img.width))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception as e:
        log.debug(f"jpeg encode failed: {e}")
        return None


def _rand_id() -> str:
    import time
    return format(int(time.monotonic() * 1000) & 0xffffffffff, "x")
