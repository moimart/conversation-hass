"""Shared fixtures for HAL test suite."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Fake MCP client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_mcp_client():
    """An MCPClient with pre-populated tools but no real server connection."""
    from server.app.mcp_client import MCPClient

    client = MCPClient(server_url="http://fake-mcp:9999")
    client._tools = [
        {"name": "ha_call_service", "description": "Call a Home Assistant service", "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
            },
        }},
        {"name": "ha_get_state", "description": "Get entity state", "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
        }},
    ]
    client._tools_for_llm = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in client._tools
    ]
    # Mock session so call_tool works
    client._session = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Fake TTS engine
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_tts():
    """A TTSEngine that returns deterministic fake WAV audio."""
    from server.app.tts import TTSEngine

    engine = TTSEngine(host="fake-tts", port=10200)

    # Produce a minimal valid WAV (44-byte header + 100 bytes of silence)
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00" * 200)
    fake_wav = buf.getvalue()

    engine.synthesize = AsyncMock(return_value=fake_wav)
    return engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_audio_bytes(duration_s: float = 0.5, sample_rate: int = 16000, freq: float = 440.0) -> bytes:
    """Generate a sine-wave PCM 16-bit LE audio buffer."""
    n_samples = int(sample_rate * duration_s)
    t = np.linspace(0, duration_s, n_samples, dtype=np.float32)
    signal = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)
    return signal.tobytes()


def make_silence_bytes(duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Generate silence as PCM 16-bit LE."""
    n_samples = int(sample_rate * duration_s)
    return np.zeros(n_samples, dtype=np.int16).tobytes()
