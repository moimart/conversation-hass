"""TTS watchdog (server/scripts/tts_watchdog.py): restarts the wedged TTS
container with the host's container CLI — podman when installed (185 runs
rootless Podman), docker otherwise, TTS_WATCHDOG_ENGINE overriding both."""
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "server" / "scripts" / "tts_watchdog.py"


def _load(monkeypatch, which=None, engine=None):
    monkeypatch.setattr("shutil.which", lambda name: which if name == "podman" else None)
    if engine is None:
        monkeypatch.delenv("TTS_WATCHDOG_ENGINE", raising=False)
    else:
        monkeypatch.setenv("TTS_WATCHDOG_ENGINE", engine)
    spec = importlib.util.spec_from_file_location("tts_watchdog_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("which,engine,expected", [
    ("/usr/bin/podman", None, "podman"),
    (None, None, "docker"),
    ("/usr/bin/podman", "docker", "docker"),
])
def test_engine_selection(monkeypatch, which, engine, expected):
    assert _load(monkeypatch, which, engine).ENGINE == expected


def test_restart_uses_engine(monkeypatch):
    mod = _load(monkeypatch, "/usr/bin/podman")
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: calls.append(args))
    mod.restart()
    assert calls == [["podman", "restart", "-t", "5", "wyoming-omnivoice"]]
