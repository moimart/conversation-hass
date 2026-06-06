"""Timer alarm sound: a classic kitchen-timer beep pattern, generated
programmatically (like main._generate_chime) and PREPENDED to the finish
announcement's TTS audio server-side. Every device — kiosk speaker and each
satellite's Web-Audio player — receives one combined WAV, so no client or
mobile-app changes are needed to hear the alarm.

The alarm is synthesized at the TTS clip's own sample rate / channel count
(parsed from its WAV header) so the concatenation never resamples. If TTS
failed (no audio), the alarm alone is returned — a finished timer must make
*some* noise.
"""
from __future__ import annotations

import io
import logging
import math
import struct
import wave

log = logging.getLogger("hal.alarm")

_DEFAULT_RATE = 22050

# Pattern shape: two groups of three short dual-tone beeps —
# "beep-beep-beep … beep-beep-beep" (~1.6 s total).
_BEEP_S = 0.14
_GAP_S = 0.09
_GROUP_PAUSE_S = 0.30
_AFTER_ALARM_PAUSE_S = 0.35     # breath between alarm and the spoken line
_BEEP_FREQ = 880.0
_AMP = 0.45


def _beep_pcm(rate: int) -> bytes:
    """One beep: fundamental + octave, with a short attack/decay envelope
    so it doesn't click."""
    n = int(rate * _BEEP_S)
    edge = max(1, int(rate * 0.008))
    out = bytearray()
    for i in range(n):
        env = 1.0
        if i < edge:
            env = i / edge
        elif i > n - edge:
            env = max(0.0, (n - i) / edge)
        v = _AMP * env * (
            0.65 * math.sin(2 * math.pi * _BEEP_FREQ * i / rate)
            + 0.35 * math.sin(2 * math.pi * _BEEP_FREQ * 2 * i / rate)
        )
        out += struct.pack("<h", int(v * 32767))
    return bytes(out)


def _silence_pcm(rate: int, seconds: float) -> bytes:
    return b"\x00\x00" * int(rate * seconds)


def alarm_pcm(rate: int) -> bytes:
    """Mono 16-bit PCM of the full alarm pattern at the given rate."""
    beep = _beep_pcm(rate)
    gap = _silence_pcm(rate, _GAP_S)
    group = (beep + gap) * 3
    return group + _silence_pcm(rate, _GROUP_PAUSE_S) + group


def prepend_alarm(tts_wav: bytes | None) -> bytes | None:
    """Combined WAV: alarm pattern + pause + the TTS clip.

    Matches the TTS clip's sample rate and channel count. Returns the input
    unchanged if it isn't a 16-bit WAV we can safely splice; returns an
    alarm-only WAV when there is no TTS audio at all.
    """
    rate, channels, width = _DEFAULT_RATE, 1, 2
    frames = b""
    if tts_wav:
        try:
            with wave.open(io.BytesIO(tts_wav)) as w:
                rate = w.getframerate()
                channels = w.getnchannels()
                width = w.getsampwidth()
                frames = w.readframes(w.getnframes())
        except Exception as e:
            log.warning(f"alarm: could not parse TTS WAV ({e}) — skipping alarm")
            return tts_wav
        if width != 2 or channels not in (1, 2):
            log.warning(f"alarm: unexpected TTS format (width={width}, "
                        f"channels={channels}) — skipping alarm")
            return tts_wav

    pcm = alarm_pcm(rate) + _silence_pcm(rate, _AFTER_ALARM_PAUSE_S)
    if channels == 2:
        # Duplicate the mono alarm into both channels.
        pcm = b"".join(pcm[i:i + 2] * 2 for i in range(0, len(pcm), 2))

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm + frames)
    return out.getvalue()
