"""Tests for speaker identification filter."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from server.app.speaker_filter import SpeakerFilter


class TestSpeakerFilterInit:
    def test_default_threshold(self):
        sf = SpeakerFilter()
        assert sf.threshold == 0.75

    def test_custom_threshold(self):
        sf = SpeakerFilter(similarity_threshold=0.5)
        assert sf.threshold == 0.5

    def test_no_ai_enrolled(self):
        sf = SpeakerFilter()
        assert sf._ai_embedding is None


class TestIdentifyWithoutEnrollment:
    def test_returns_human_when_no_enrollment(self):
        sf = SpeakerFilter()
        audio = np.random.randn(16000).astype(np.float32)
        assert sf.identify(audio, 16000) == "human"


class TestEnrollAndIdentify:
    def test_enroll_sets_embedding(self):
        sf = SpeakerFilter()
        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.ones(256)

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            sf.enroll_ai_voice(np.random.randn(16000).astype(np.float32), 16000)

        assert sf._ai_embedding is not None
        assert sf._ai_embedding.shape == (256,)

    def test_enroll_with_no_encoder(self):
        sf = SpeakerFilter()
        with patch.object(sf, "_get_encoder", return_value=None):
            sf.enroll_ai_voice(np.random.randn(16000).astype(np.float32), 16000)
        assert sf._ai_embedding is None

    def test_identify_ai_voice(self):
        sf = SpeakerFilter(similarity_threshold=0.7)
        sf._ai_embedding = np.array([1.0, 0.0, 0.0, 0.0])

        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.array([1.0, 0.0, 0.0, 0.0])

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            result = sf.identify(np.random.randn(16000).astype(np.float32), 16000)
        assert result == "ai"

    def test_identify_human_voice(self):
        sf = SpeakerFilter(similarity_threshold=0.7)
        sf._ai_embedding = np.array([1.0, 0.0, 0.0, 0.0])

        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.array([0.0, 1.0, 0.0, 0.0])

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            result = sf.identify(np.random.randn(16000).astype(np.float32), 16000)
        assert result == "human"

    def test_identify_borderline(self):
        sf = SpeakerFilter(similarity_threshold=0.5)
        sf._ai_embedding = np.array([1.0, 1.0, 0.0, 0.0])

        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.array([1.0, 0.0, 0.0, 0.0])

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            result = sf.identify(np.random.randn(16000).astype(np.float32), 16000)
        # cos similarity of [1,1,0,0] and [1,0,0,0] = 1/sqrt(2) ~ 0.707 > 0.5
        assert result == "ai"

    def test_identify_encoder_failure(self):
        sf = SpeakerFilter()
        sf._ai_embedding = np.ones(256)

        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.side_effect = RuntimeError("model crash")

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            result = sf.identify(np.random.randn(16000).astype(np.float32), 16000)
        assert result == "human"

    def test_identify_no_encoder_returns_human(self):
        sf = SpeakerFilter()
        sf._ai_embedding = np.ones(256)

        with patch.object(sf, "_get_encoder", return_value=None):
            result = sf.identify(np.random.randn(16000).astype(np.float32), 16000)
        assert result == "human"


class TestResampling:
    def test_enroll_resamples_non_16k(self):
        sf = SpeakerFilter()
        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.ones(256)

        mock_librosa = MagicMock()
        mock_librosa.resample.return_value = np.random.randn(16000).astype(np.float32)

        with patch.object(sf, "_get_encoder", return_value=fake_encoder), \
             patch.dict("sys.modules", {"librosa": mock_librosa}):
            sf.enroll_ai_voice(np.random.randn(22050).astype(np.float32), 22050)
            mock_librosa.resample.assert_called_once()

    def test_enroll_skips_resample_at_16k(self):
        sf = SpeakerFilter()
        fake_encoder = MagicMock()
        fake_encoder.embed_utterance.return_value = np.ones(256)

        with patch.object(sf, "_get_encoder", return_value=fake_encoder):
            sf.enroll_ai_voice(np.random.randn(16000).astype(np.float32), 16000)
            fake_encoder.embed_utterance.assert_called_once()
