"""Timer alarm audio: the beep pattern is prepended to the TTS WAV
server-side so kiosk + satellites hear it with no client changes."""
import io
import wave

from server.app.alarm import alarm_pcm, prepend_alarm, _DEFAULT_RATE


def _wav(rate=22050, channels=1, seconds=0.5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds) * channels)
    return buf.getvalue()


def _params(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


def test_prepends_alarm_keeping_format():
    tts = _wav(rate=22050, channels=1)
    out = prepend_alarm(tts)
    rate, ch, width, frames = _params(out)
    assert (rate, ch, width) == (22050, 1, 2)
    _, _, _, tts_frames = _params(tts)
    assert frames > tts_frames                      # alarm + pause added
    # The original speech is intact at the tail.
    with wave.open(io.BytesIO(out)) as w:
        w.readframes(frames - tts_frames)
        assert w.readframes(tts_frames) == b"\x01\x00" * tts_frames


def test_matches_tts_sample_rate():
    out = prepend_alarm(_wav(rate=16000))
    rate, _, _, _ = _params(out)
    assert rate == 16000


def test_stereo_tts_gets_stereo_alarm():
    out = prepend_alarm(_wav(channels=2))
    _, ch, _, _ = _params(out)
    assert ch == 2


def test_no_tts_returns_alarm_only():
    """A finished timer must make noise even when TTS failed."""
    out = prepend_alarm(None)
    rate, ch, width, frames = _params(out)
    assert (rate, ch, width) == (_DEFAULT_RATE, 1, 2)
    assert frames > 0


def test_unparseable_audio_passes_through():
    junk = b"not a wav at all"
    assert prepend_alarm(junk) == junk


def test_alarm_pcm_is_nontrivial_and_16bit_aligned():
    pcm = alarm_pcm(22050)
    assert len(pcm) % 2 == 0
    assert len(pcm) > 22050  # > 0.5s of 16-bit samples
    assert any(b != 0 for b in pcm)  # actually contains tone, not silence
