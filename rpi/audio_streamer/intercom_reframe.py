"""Dependency-free pacing/reframing for the kiosk (hub) call mic.

The streamer captures big (~256 ms) chunks sized for the wake-word/STT path.
Sending one of those whole makes aiortc burst ~13 Opus packets and then go
silent for 256 ms, which the receiving phone's jitter buffer can't smooth out
→ choppy audio. ``MicReframer`` re-slices the byte stream into fixed 20 ms
frames (Opus's packetization unit) and computes a wall-clock pacing delay so a
buffered burst is emitted EVENLY instead of all at once.

Kept separate from ``intercom_peer.py`` (which imports aiortc/av/pyaudio) so the
pure logic is unit-testable in the slim test env that has none of those.
"""
from __future__ import annotations


class MicReframer:
    """Accumulate arbitrary-size PCM s16-mono byte chunks; emit fixed-size 20 ms
    frames on a real-time schedule.

    Usage (see ``_MicTrack.recv``):
        while not rf.ready(): rf.push(await queue.get())
        chunk = rf.pop_frame()
        delay = rf.delay_for(monotonic_now)   # >= 0 seconds to sleep
        ... build the frame with pts=rf.pts ...
        rf.advance()
    """

    def __init__(self, rate: int, frame_ms: int = 20, max_behind_s: float = 0.5):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.frame_samples = max(1, rate * frame_ms // 1000)
        self.frame_bytes = self.frame_samples * 2      # s16 mono = 2 bytes/sample
        self.pts = 0                                   # samples emitted so far
        self._buf = bytearray()
        self._start: float | None = None
        self._max_behind = max_behind_s

    def push(self, data: bytes) -> None:
        self._buf += data

    def ready(self) -> bool:
        return len(self._buf) >= self.frame_bytes

    def pop_frame(self) -> bytes:
        """Exactly one 20 ms frame of bytes. Caller must ensure ``ready()``."""
        out = bytes(self._buf[:self.frame_bytes])
        del self._buf[:self.frame_bytes]
        return out

    def delay_for(self, now: float) -> float:
        """Seconds to wait before emitting the NEXT frame (the one at ``self.pts``),
        given the current monotonic time. Returns 0 when the frame is due or we're
        behind. If we've fallen more than ``max_behind_s`` behind (a capture stall
        / CPU spike), realign the clock so we don't machine-gun the backlog."""
        if self._start is None:
            self._start = now
        due = self._start + self.pts / self.rate
        delay = due - now
        if delay < -self._max_behind:
            self._start = now - self.pts / self.rate
            return 0.0
        return delay if delay > 0 else 0.0

    def advance(self) -> None:
        self.pts += self.frame_samples
