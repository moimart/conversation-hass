"""pal-llm-control pure logic: llm.env round-trip, OpenClaw config patching
both ways (remembering the previous Ollama model), and atomic writes that keep
the original owner/mode (the service runs as root; openclaw.json must stay
readable by the gateway's user)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "llm-control" / "app.py"


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("llm_control_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OPENCLAW_CONFIG", str(tmp_path / "openclaw.json"))
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "state" / "s.json"))
    return mod


def test_env_roundtrip_and_change_detection(ctl, tmp_path):
    p = str(tmp_path / "llm.env")
    env = ctl.read_env(p)
    assert env["VLLM_MODEL"] == "Qwen/Qwen3.8-27B-FP8"       # defaults when absent
    assert ctl.write_env(p, env) is True                      # first write
    assert ctl.write_env(p, dict(env)) is False               # unchanged
    env["VLLM_MAX_MODEL_LEN"] = "65536"
    assert ctl.write_env(p, env) is True
    assert ctl.read_env(p)["VLLM_MAX_MODEL_LEN"] == "65536"


def test_openclaw_switches_and_restores_previous_model(ctl):
    cfg = {"agents": {"defaults": {"model": {"primary": "ollama/muse-glimmer:30b",
                                              "fallbacks": ["ollama/gemma4:e4b"]}}},
           "models": {"providers": {"ollama": {"api": "ollama"}}},
           "channels": {"hal": {"enabled": True}}}
    Path(ctl.OPENCLAW_CONFIG).write_text(json.dumps(cfg))
    state = {}
    assert ctl.openclaw_to_vllm("qwen3.8-27b", 131072, state)
    c = json.loads(Path(ctl.OPENCLAW_CONFIG).read_text())
    assert c["agents"]["defaults"]["model"] == {"primary": "vllm/qwen3.8-27b",
                                                "fallbacks": ["ollama/gemma4:e4b"]}
    v = c["models"]["providers"]["vllm"]
    assert v["api"] == "openai-completions" and v["models"][0]["contextWindow"] == 131072
    assert c["models"]["providers"]["ollama"] == {"api": "ollama"}   # untouched
    assert c["channels"] == {"hal": {"enabled": True}}
    assert state["openclaw_previous_primary"] == "ollama/muse-glimmer:30b"
    # Re-applying vLLM must not overwrite the remembered Ollama model.
    ctl.openclaw_to_vllm("qwen3.8-27b", 65536, state)
    assert state["openclaw_previous_primary"] == "ollama/muse-glimmer:30b"
    ctl.openclaw_to_ollama(state, "fallback-model")
    c = json.loads(Path(ctl.OPENCLAW_CONFIG).read_text())
    assert c["agents"]["defaults"]["model"]["primary"] == "ollama/muse-glimmer:30b"


def test_openclaw_to_ollama_noop_when_already_on_ollama(ctl):
    Path(ctl.OPENCLAW_CONFIG).write_text(json.dumps(
        {"agents": {"defaults": {"model": "ollama/x"}}}))
    before = Path(ctl.OPENCLAW_CONFIG).read_text()
    ctl.openclaw_to_ollama({}, "")
    assert Path(ctl.OPENCLAW_CONFIG).read_text() == before


def test_openclaw_absent_is_skipped(ctl):
    assert ctl.openclaw_to_vllm("m", 1000, {}) is False


def test_atomic_write_keeps_mode(ctl, tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{}")
    os.chmod(p, 0o600)
    ctl.atomic_write(str(p), '{"a": 1}')
    assert json.loads(p.read_text()) == {"a": 1}
    assert (os.stat(p).st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".llmctl.*"))
