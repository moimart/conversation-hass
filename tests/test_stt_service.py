"""Tests for the decoupled stt-service FastAPI app.

The service lives in a dash-named directory (`stt-service/`), which Python
can't import normally, so we load `app.py` by file path with importlib. Its
sibling `engines.py` (the ASR engines, owned by this image) is made importable
by adding the `stt-service/` dir to sys.path before loading.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient


REPO = Path(__file__).resolve().parent.parent
STT_DIR = REPO / "stt-service"
STT_APP_PATH = STT_DIR / "app.py"


def _load_stt_app(*, env=None):
    """Import stt-service/app.py with its `engines` sibling resolvable.

    Reloads on each call so module-level env (STT_ENGINE/STT_MODEL) is
    re-evaluated under any `env=` overrides the test wants.
    """
    if str(STT_DIR) not in sys.path:
        sys.path.insert(0, str(STT_DIR))
    sys.modules.pop("stt_service_app", None)
    if env:
        for k, v in env.items():
            patch.dict("os.environ", {k: v}).start()
    spec = importlib.util.spec_from_file_location("stt_service_app", STT_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load once at import time for the bulk of the tests; individual tests can
# re-load with custom env when they need the module-level fallback re-checked.
stt = _load_stt_app()


# --- module-level config ------------------------------------------------

class TestModuleConfig:
    def test_default_engine_is_whisper(self):
        # With STT_ENGINE unset, the default is whisper.
        with patch.dict("os.environ", {}, clear=False):
            m = _load_stt_app()
            assert m.STT_ENGINE == "whisper"

    def test_engine_remote_falls_back_to_whisper(self):
        """A pointer back at ourselves ('remote') would deadlock — fall back."""
        with patch.dict("os.environ", {"STT_ENGINE": "remote"}):
            m = _load_stt_app()
            assert m.STT_ENGINE == "whisper"

    def test_engine_unknown_falls_back_to_whisper(self):
        with patch.dict("os.environ", {"STT_ENGINE": "bogus-engine"}):
            m = _load_stt_app()
            assert m.STT_ENGINE == "whisper"

    def test_engine_nemotron_kept(self):
        with patch.dict("os.environ", {"STT_ENGINE": "nemotron"}):
            m = _load_stt_app()
            assert m.STT_ENGINE == "nemotron"

    def test_stt_model_from_env(self):
        with patch.dict("os.environ", {"STT_MODEL": "large-v3"}):
            m = _load_stt_app()
            assert m.STT_MODEL == "large-v3"


def _fake_transcriber(text: str = "") -> MagicMock:
    """A drop-in mock that satisfies the BaseTranscriber surface used by lifespan + endpoints."""
    fake = MagicMock()
    fake.initialize = AsyncMock()
    fake.warm_up = AsyncMock()
    fake.transcribe = AsyncMock(return_value=text)
    return fake


def _client_with_fake(fake: MagicMock):
    """Build a TestClient whose lifespan uses the supplied fake transcriber.

    TestClient(__enter__) triggers the FastAPI lifespan, which calls
    create_transcriber() + initialize() + warm_up(). Without this patch the
    real WhisperTranscriber would try to `import faster_whisper`, which the
    test env doesn't have.
    """
    return patch.object(stt, "create_transcriber", return_value=fake)


# --- /health endpoint ---------------------------------------------------

class TestHealth:
    def test_200_when_ready(self):
        fake = _fake_transcriber()
        with _client_with_fake(fake):
            with TestClient(stt.app) as client:
                r = client.get("/health")
                assert r.status_code == 200
                assert r.json() == {"status": "ok"}

    def test_503_before_lifespan(self):
        """Without going through TestClient's context manager, lifespan
        never runs so _ready stays False and /health returns 503."""
        stt._ready = False
        stt._transcriber = None
        client = TestClient(stt.app)  # NOT a context manager — no startup
        r = client.get("/health")
        assert r.status_code == 503


# --- /transcribe endpoint -----------------------------------------------

class TestTranscribe:
    def test_503_when_not_ready(self):
        stt._ready = False
        stt._transcriber = None
        client = TestClient(stt.app)  # no lifespan → still not ready
        r = client.post("/transcribe", content=b"\x00\x00", headers={"X-Sample-Rate": "16000"})
        assert r.status_code == 503

    def test_decodes_body_as_float32_and_calls_transcribe(self):
        # Hand the route a known float32 buffer and assert the transcriber
        # receives the SAME values (after np.frombuffer + .copy()).
        audio_in = np.array([0.1, -0.2, 0.3, -0.4], dtype=np.float32)
        fake = _fake_transcriber(text="hello world")
        with _client_with_fake(fake):
            with TestClient(stt.app) as client:
                r = client.post(
                    "/transcribe",
                    content=audio_in.tobytes(),
                    headers={"X-Sample-Rate": "16000"},
                )
        assert r.status_code == 200
        assert r.json() == {"text": "hello world"}

        fake.transcribe.assert_awaited_once()
        sent = fake.transcribe.call_args.args[0]
        assert isinstance(sent, np.ndarray)
        assert sent.dtype == np.float32
        np.testing.assert_array_equal(sent, audio_in)

    def test_writable_array_passed_to_transcriber(self):
        """np.frombuffer is read-only; .copy() in the route makes it writable."""
        fake = _fake_transcriber()
        with _client_with_fake(fake):
            with TestClient(stt.app) as client:
                client.post(
                    "/transcribe",
                    content=np.zeros(800, dtype=np.float32).tobytes(),
                    headers={"X-Sample-Rate": "16000"},
                )
        sent = fake.transcribe.call_args.args[0]
        assert sent.flags.writeable is True


# --- lifespan -----------------------------------------------------------

class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_initializes_and_warms_transcriber(self):
        # Reset module flags so we observe lifespan toggling them.
        stt._ready = False
        stt._transcriber = None

        fake = _fake_transcriber()
        with patch.object(stt, "create_transcriber", return_value=fake) as factory:
            async with stt.lifespan(stt.app):
                # Inside the lifespan window: model loaded, warmed, ready.
                factory.assert_called_once_with(stt.STT_ENGINE, stt.STT_MODEL)
                fake.initialize.assert_awaited_once()
                fake.warm_up.assert_awaited_once()
                assert stt._ready is True
                assert stt._transcriber is fake
