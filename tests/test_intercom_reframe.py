"""MicReframer: re-slice the kiosk call mic into evenly-paced 20 ms frames.

Pure logic (no aiortc/av/pyaudio), so it runs in the slim test env. Pins the
slicing sizes, the pts progression, and the wall-clock pacing — including the
realign-after-stall guard that stops a backlog from machine-gunning out."""

import pytest

from rpi.audio_streamer.intercom_reframe import MicReframer


def test_frame_sizing_16k_and_48k():
    rf = MicReframer(16000)
    assert rf.frame_samples == 320 and rf.frame_bytes == 640    # 20 ms @ 16 kHz
    rf48 = MicReframer(48000)
    assert rf48.frame_samples == 960 and rf48.frame_bytes == 1920


def test_rejects_bad_rate():
    with pytest.raises(ValueError):
        MicReframer(0)


def test_slices_a_big_blob_into_20ms_frames():
    rf = MicReframer(16000)
    # One ~256 ms capture chunk: 4096 samples = 8192 bytes.
    rf.push(b"\x01\x02" * 4096)
    frames = []
    while rf.ready():
        frames.append(rf.pop_frame())
        rf.advance()
    # 4096 / 320 = 12 full frames; 256 samples (512 bytes) remain buffered.
    assert len(frames) == 12
    assert all(len(f) == 640 for f in frames)
    assert not rf.ready()


def test_pts_advances_by_one_frame_each():
    rf = MicReframer(16000)
    assert rf.pts == 0
    rf.advance(); assert rf.pts == 320
    rf.advance(); assert rf.pts == 640


def test_pacing_spaces_frames_20ms_apart():
    rf = MicReframer(16000)
    # First frame at t=100.0 is due immediately (clock anchors here).
    assert rf.delay_for(100.0) == 0.0
    rf.advance()                                   # pts -> 320 (20 ms)
    # Next frame, asked at the same instant, must wait ~20 ms.
    assert rf.delay_for(100.0) == pytest.approx(0.02, abs=1e-6)
    rf.advance()                                   # pts -> 640 (40 ms)
    assert rf.delay_for(100.0) == pytest.approx(0.04, abs=1e-6)


def test_behind_schedule_returns_zero_no_negative_sleep():
    rf = MicReframer(16000)
    rf.delay_for(100.0)                            # anchor at 100.0
    rf.advance()                                   # frame due at 100.02
    # Asked late (but within max_behind) -> due now, no negative delay.
    assert rf.delay_for(100.40) == 0.0


def test_realigns_after_a_long_stall():
    rf = MicReframer(16000, max_behind_s=0.5)
    rf.delay_for(100.0)                            # anchor at 100.0
    rf.advance()                                   # frame due at 100.02
    # >0.5 s behind -> realign the clock and emit now (0 delay)...
    assert rf.delay_for(100.60) == 0.0
    # ...and the NEXT frame is paced 20 ms from the realigned point, not catch-up.
    rf.advance()
    assert rf.delay_for(100.60) == pytest.approx(0.02, abs=1e-6)
