"""Ollama ⇄ vLLM backend switching: the vLLM transport (request shape, thinking
off, tool calls), the manager that follows pal-llm-control (PAL flips only once
the controller reports ready; an error reverts the HA select), and the
conversation engine's vLLM branch with per-call fallback to Ollama."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from server.app import vllm_backend
from server.app.vllm_backend import LLMBackendManager, VLLMClient


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://x"))


@pytest.mark.asyncio
async def test_vllm_request_shape_and_tool_call_translation():
    seen = {}

    async def handler(request: httpx.Request):
        seen.update(json.loads(request.content))
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "set_theme", "arguments": '{"name": "dark"}'}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 9}})

    c = VLLMClient("http://vllm:8000/", "qwen3.8-27b",
                   http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tools = [{"type": "function", "function": {"name": "set_theme", "parameters": {}}}]
    resp, usage = await c.chat([{"role": "user", "content": "go dark"}], tools, max_tokens=512)
    assert seen["url"] == "http://vllm:8000/v1/chat/completions"
    assert seen["model"] == "qwen3.8-27b" and seen["max_tokens"] == 512
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["tools"] == tools and seen["tool_choice"] == "auto"
    tc = resp["message"]["tool_calls"][0]["function"]
    assert tc == {"name": "set_theme", "arguments": {"name": "dark"}}
    assert usage["completion_tokens"] == 9


# --- manager ----------------------------------------------------------------------

class Bridge:
    def __init__(self):
        self.status, self.config = [], []

    async def publish_llm_backend_status(self, phase, detail=""):
        self.status.append(phase)

    async def publish_config(self, key, value):
        self.config.append((key, value))


def _state(backend="ollama"):
    cfg = {"llm_backend": backend, "llm_concurrency": 4, "num_ctx": 65536, "ollama_model": "m"}
    return SimpleNamespace(
        runtime_config=SimpleNamespace(get=lambda k, d=None: cfg.get(k, d),
                                       set=lambda k, v: cfg.__setitem__(k, v)),
        conversation=SimpleNamespace(num_ctx=65536, llm_backend="ollama", vllm=None),
        mqtt_bridge=Bridge(), _cfg=cfg)


@pytest.mark.asyncio
async def test_manager_flips_only_when_ready(monkeypatch):
    monkeypatch.setattr(vllm_backend, "POLL_S", 0.01)
    st = _state()
    m = LLMBackendManager(st, "http://ctl:8790", "http://vllm:8000")
    statuses = iter([
        {"target": "vllm", "phase": "starting", "detail": "loading"},
        {"target": "vllm", "phase": "starting", "detail": "loading"},
        {"target": "vllm", "phase": "ready", "detail": "", "served_model": "qwen3.8-27b"},
    ])
    posted = {}

    async def post(url, json=None):
        posted.update(json)
        return _resp({})

    async def get(url):
        return _resp(next(statuses))

    m._http = SimpleNamespace(post=post, get=get, aclose=AsyncMock())
    await m.apply("vllm")
    assert posted == {"backend": "vllm", "num_ctx": 65536, "concurrency": 4, "ollama_model": "m"}
    assert st.conversation.llm_backend == "ollama"          # not yet
    await asyncio.wait_for(m._task, 1.0)
    assert st.conversation.llm_backend == "vllm"
    assert st.conversation.vllm.model == "qwen3.8-27b"
    assert st.mqtt_bridge.status[-1] == "ready"


@pytest.mark.asyncio
async def test_manager_error_reverts_select(monkeypatch):
    monkeypatch.setattr(vllm_backend, "POLL_S", 0.01)
    st = _state(backend="vllm")                             # user picked vllm …
    m = LLMBackendManager(st, "http://ctl:8790", "http://vllm:8000")

    async def post(url, json=None):
        return _resp({})

    async def get(url):
        return _resp({"target": "vllm", "phase": "error", "error": "OOM"})

    m._http = SimpleNamespace(post=post, get=get, aclose=AsyncMock())
    await m.apply("vllm")
    await asyncio.wait_for(m._task, 1.0)
    assert st.conversation.llm_backend == "ollama"          # … but it failed
    assert st._cfg["llm_backend"] == "ollama"
    assert ("llm_backend", "ollama") in st.mqtt_bridge.config
    assert "error" in st.mqtt_bridge.status


@pytest.mark.asyncio
async def test_manager_disabled_without_controller():
    st = _state()
    m = LLMBackendManager(st, "", "http://vllm:8000")
    await m.apply("vllm")
    assert st.mqtt_bridge.status == ["unavailable"]
    assert st.conversation.llm_backend == "ollama"


# --- conversation engine ----------------------------------------------------------

def _conversation():
    from server.app.conversation import ConversationManager
    mcp = SimpleNamespace(tools_for_llm=[{"type": "function", "function": {"name": "t"}}])
    return ConversationManager(wake_word="hey", ollama_host="http://ollama", ollama_model="m",
                               mcp_client=mcp, tts_engine=None)


@pytest.mark.asyncio
async def test_conversation_uses_vllm_when_active():
    conv = _conversation()
    conv.llm_backend = "vllm"
    conv.vllm = SimpleNamespace(model="qwen3.8-27b", chat=AsyncMock(return_value=(
        {"message": {"role": "assistant", "content": "hi"}}, {"completion_tokens": 3})))
    conv._http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("ollama called")))
    out = await conv._chat_completion([{"role": "user", "content": "x"}])
    assert out["message"]["content"] == "hi"
    assert conv._last_ollama_metrics["model"] == "vllm/qwen3.8-27b"
    args, kwargs = conv.vllm.chat.call_args
    assert args[1] == conv.mcp.tools_for_llm and kwargs["max_tokens"] == conv.num_predict


@pytest.mark.asyncio
async def test_conversation_falls_back_to_ollama_when_vllm_fails():
    conv = _conversation()
    conv.llm_backend = "vllm"
    conv.vllm = SimpleNamespace(model="qwen3.8-27b",
                                chat=AsyncMock(side_effect=httpx.ConnectError("down")))
    ollama = MagicMock()
    ollama.json.return_value = {"message": {"role": "assistant", "content": "from ollama"}}
    ollama.raise_for_status = MagicMock()
    conv._http = SimpleNamespace(post=AsyncMock(return_value=ollama))
    out = await conv._chat_completion([{"role": "user", "content": "x"}])
    assert out["message"]["content"] == "from ollama"


@pytest.mark.asyncio
async def test_collapse_fallback_model_stays_on_ollama():
    """model_override is an Ollama model: it must not be sent to vLLM."""
    conv = _conversation()
    conv.llm_backend = "vllm"
    conv.vllm = SimpleNamespace(model="q", chat=AsyncMock(side_effect=AssertionError("vllm called")))
    ollama = MagicMock()
    ollama.json.return_value = {"message": {"role": "assistant", "content": "fb"}}
    ollama.raise_for_status = MagicMock()
    conv._http = SimpleNamespace(post=AsyncMock(return_value=ollama))
    out = await conv._chat_completion([{"role": "user", "content": "x"}], model_override="gemma")
    assert out["message"]["content"] == "fb"
