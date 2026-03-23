"""Speaker identification to distinguish humans from AI voice."""

import logging

import numpy as np

log = logging.getLogger("hal.speaker")


class SpeakerFilter:
    """
    Simple speaker identification using voice embeddings.

    Enrolls the AI's TTS voice and compares incoming speech against it.
    If similarity is high, labels as 'ai'; otherwise 'human'.
    """

    def __init__(self, similarity_threshold: float = 0.75):
        self.threshold = similarity_threshold
        self._encoder = None
        self._ai_embedding: np.ndarray | None = None

    def _get_encoder(self):
        if self._encoder is None:
            try:
                from resemblyzer import VoiceEncoder
                self._encoder = VoiceEncoder()
                log.info("Speaker encoder loaded")
            except Exception as e:
                log.warning(f"Could not load speaker encoder: {e}")
        return self._encoder

    def enroll_ai_voice(self, audio: np.ndarray, sample_rate: int):
        """
        Enroll the AI's TTS voice so we can filter it out later.

        Call this with a sample of TTS output audio.
        """
        encoder = self._get_encoder()
        if encoder is None:
            return

        # Resample to 16kHz if needed
        if sample_rate != 16000:
            import librosa
            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)

        try:
            self._ai_embedding = encoder.embed_utterance(audio)
            log.info("AI voice enrolled for speaker filtering")
        except Exception as e:
            log.warning(f"Failed to enroll AI voice: {e}")

    def identify(self, audio: np.ndarray, sample_rate: int) -> str:
        """
        Identify whether audio is from AI or a human.

        Returns 'ai' or 'human'.
        """
        # If no AI voice enrolled, assume human
        if self._ai_embedding is None:
            return "human"

        encoder = self._get_encoder()
        if encoder is None:
            return "human"

        try:
            # Resample if needed
            if sample_rate != 16000:
                import librosa
                audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)

            embedding = encoder.embed_utterance(audio)
            similarity = np.dot(self._ai_embedding, embedding) / (
                np.linalg.norm(self._ai_embedding) * np.linalg.norm(embedding)
            )

            if similarity > self.threshold:
                log.debug(f"Speaker identified as AI (similarity: {similarity:.3f})")
                return "ai"
            else:
                log.debug(f"Speaker identified as human (similarity: {similarity:.3f})")
                return "human"

        except Exception as e:
            log.warning(f"Speaker identification failed: {e}")
            return "human"
