"""Tests for the cloud LLM override: provider registry (hot-reload, env
fallback, key hygiene), the OpenAI<->Ollama adapter (tool_call_id round-trip),
and the client (auth headers, model listing, payload mapping)."""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.cloud_llm import (
    CloudLLMClient,
    Provider,
    ProviderRegistry,
    _from_openai_response,
    _to_openai_messages,
    _ID_STASH,
)


# --- adapter: inbound (Ollama-shaped messages -> OpenAI) ---------------------

def test_inbound_simple_messages_pass_through():
    msgs = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = _to_openai_messages(msgs)
    assert out == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_inbound_assistant_toolcall_translation():
    msgs = [{
        "role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "get_state", "arguments": {"entity_id": "light.x"}}}],
    }]
    out = _to_openai_messages(msgs)
    tc = out[0]["tool_calls"][0]
    assert out[0]["content"] is None                       # "" -> None when tool-calling
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_state"
    assert json.loads(tc["function"]["arguments"]) == {"entity_id": "light.x"}  # dict -> JSON string
    assert tc["id"]                                        # synthesized id present


def test_inbound_tool_results_pair_positionally():
    msgs = [
        {"role": "assistant", "content": "", _ID_STASH: ["id_a", "id_b"],
         "tool_calls": [
             {"function": {"name": "t1", "arguments": {}}},
             {"function": {"name": "t2", "arguments": {}}},
         ]},
        {"role": "tool", "content": "result-1"},
        {"role": "tool", "content": "result-2"},
    ]
    out = _to_openai_messages(msgs)
    assert out[1]["tool_call_id"] == "id_a"   # first result -> first call
    assert out[2]["tool_call_id"] == "id_b"   # second result -> second call
    assert out[1]["content"] == "result-1"


def test_inbound_reuses_stashed_provider_ids():
    msgs = [{
        "role": "assistant", "content": "", _ID_STASH: ["call_xyz"],
        "tool_calls": [{"function": {"name": "t", "arguments": {}}}],
    }]
    out = _to_openai_messages(msgs)
    assert out[0]["tool_calls"][0]["id"] == "call_xyz"


def test_inbound_orphan_tool_result_gets_fallback_id():
    msgs = [{"role": "tool", "content": "orphan"}]  # cross-engine history edge
    out = _to_openai_messages(msgs)
    assert out[0]["tool_call_id"].startswith("call_orphan_")


# --- adapter: outbound (OpenAI response -> Ollama shape) ---------------------

def test_outbound_text_response_none_content_becomes_empty():
    data = {"choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}]}
    out = _from_openai_response(data)
    assert out["message"]["content"] == ""
    assert "tool_calls" not in out["message"]


def test_outbound_toolcall_response_parses_arguments_and_stashes_ids():
    data = {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "ha_call_service", "arguments": '{"domain": "light"}'},
        }],
    }, "finish_reason": "tool_calls"}]}
    out = _from_openai_response(data)
    tc = out["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "ha_call_service"
    assert tc["function"]["arguments"] == {"domain": "light"}   # JSON string -> dict
    assert out["message"][_ID_STASH] == ["call_abc"]


def test_round_trip_two_round_tool_loop():
    """Round 1 response (with provider ids) is appended verbatim by the loop;
    round 2 inbound must replay the provider's exact ids."""
    r1 = _from_openai_response({"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "prov_1", "type": "function",
                        "function": {"name": "t", "arguments": "{}"}}],
    }}]})
    messages = [
        {"role": "user", "content": "do it"},
        r1["message"],                       # appended verbatim, carries stash
        {"role": "tool", "content": "done"},
    ]
    out = _to_openai_messages(messages)
    assert out[1]["tool_calls"][0]["id"] == "prov_1"
    assert out[2]["tool_call_id"] == "prov_1"


# --- provider registry --------------------------------------------------------

def test_registry_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reg = ProviderRegistry(str(tmp_path / "missing.json"))
    names = [p.name for p in reg.providers()]
    assert names == ["openai"]
    assert reg.get("openai").base_url == "https://api.openai.com/v1"


def test_registry_file_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    f = tmp_path / "cloud_providers.json"
    f.write_text(json.dumps({"providers": [
        {"name": "openai", "base_url": "https://proxy.example/v1", "api_key": "sk-file"},
    ]}))
    reg = ProviderRegistry(str(f))
    p = reg.get("openai")
    assert p.base_url == "https://proxy.example/v1"
    assert p.api_key == "sk-file"


def test_registry_mtime_hot_reload(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / "cloud_providers.json"
    f.write_text(json.dumps({"providers": [
        {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-1"},
    ]}))
    reg = ProviderRegistry(str(f))
    assert [p.name for p in reg.providers()] == ["deepseek"]
    # add kimi; bump mtime explicitly (same-second writes)
    f.write_text(json.dumps({"providers": [
        {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-1"},
        {"name": "kimi", "base_url": "https://api.moonshot.ai/v1", "api_key": "sk-2"},
    ]}))
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert sorted(p.name for p in reg.providers()) == ["deepseek", "kimi"]


def test_registry_skips_placeholder_and_malformed(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / "cloud_providers.json"
    f.write_text(json.dumps({"providers": [
        {"name": "openai", "base_url": "https://api.openai.com/v1", "api_key": "REPLACE_ME"},
    ]}))
    assert ProviderRegistry(str(f)).providers() == []   # placeholder ignored
    f2 = tmp_path / "bad.json"
    f2.write_text("{not json")
    reg = ProviderRegistry(str(f2))
    assert reg.providers() == []                         # malformed -> env-only (none)
    assert "unreadable" in caplog.text


def test_keys_never_in_logs_or_repr(tmp_path, caplog):
    f = tmp_path / "bad.json"
    f.write_text('{"providers": [BROKEN "api_key": "sk-SECRET-BYTES"')
    ProviderRegistry(str(f))
    assert "sk-SECRET-BYTES" not in caplog.text
    p = Provider("x", "https://x/v1", "sk-SECRET-BYTES")
    assert "sk-SECRET-BYTES" not in repr(p)


# --- client -------------------------------------------------------------------

def _registry_with(*providers: Provider) -> ProviderRegistry:
    reg = ProviderRegistry.__new__(ProviderRegistry)
    reg.path = "/nonexistent"
    reg._mtime = None
    reg._providers = list(providers)
    reg._load_if_changed = lambda force=False: None
    return reg


def _http_response(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.raise_for_status = MagicMock()
    if status >= 400:
        import httpx
        r.raise_for_status.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=r)
    return r


@pytest.mark.asyncio
async def test_list_models_merges_and_prefixes():
    reg = _registry_with(
        Provider("openai", "https://api.openai.com/v1", "sk-1"),
        Provider("deepseek", "https://api.deepseek.com/v1", "sk-2"),
    )
    http = MagicMock()
    http.get = AsyncMock(side_effect=[
        _http_response(body={"data": [{"id": "gpt-5"}, {"id": "gpt-4o"}]}),
        _http_response(body={"data": [{"id": "deepseek-chat"}]}),
    ])
    client = CloudLLMClient(reg, http)
    models = await client.list_models()
    assert models == ["deepseek/deepseek-chat", "openai/gpt-4o", "openai/gpt-5"]


@pytest.mark.asyncio
async def test_list_models_one_provider_down():
    reg = _registry_with(
        Provider("openai", "https://api.openai.com/v1", "sk-1"),
        Provider("anthropic", "https://api.anthropic.com/v1", "sk-2"),
    )
    http = MagicMock()
    http.get = AsyncMock(side_effect=[
        _http_response(status=401),
        _http_response(body={"data": [{"id": "claude-opus-4-8"}]}),
    ])
    models = await CloudLLMClient(reg, http).list_models()
    assert models == ["anthropic/claude-opus-4-8"]       # dead provider skipped


@pytest.mark.asyncio
async def test_anthropic_models_use_xapikey_headers():
    reg = _registry_with(Provider("anthropic", "https://api.anthropic.com/v1", "sk-ant"))
    http = MagicMock()
    http.get = AsyncMock(return_value=_http_response(body={"data": []}))
    await CloudLLMClient(reg, http).list_models()
    headers = http.get.call_args.kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant"
    assert "anthropic-version" in headers
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_chat_posts_bearer_and_maps_params():
    reg = _registry_with(Provider("openai", "https://api.openai.com/v1", "sk-1"))
    http = MagicMock()
    http.post = AsyncMock(return_value=_http_response(
        body={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    ))
    client = CloudLLMClient(reg, http)
    out = await client.chat(
        "openai/gpt-5", [{"role": "user", "content": "hello"}],
        [{"type": "function", "function": {"name": "t", "parameters": {}}}],
        max_tokens=256,
    )
    assert out["message"]["content"] == "hi"
    kwargs = http.post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer sk-1"
    body = kwargs["json"]
    assert body["model"] == "gpt-5"
    assert body["max_tokens"] == 256
    assert body["stream"] is False
    assert body["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_chat_unknown_provider_raises():
    reg = _registry_with(Provider("openai", "https://api.openai.com/v1", "sk-1"))
    with pytest.raises(RuntimeError):
        await CloudLLMClient(reg, MagicMock()).chat("mistral/large", [], None)


@pytest.mark.asyncio
async def test_chat_retries_400_with_conservative_params():
    """gpt-5/o-series reject max_tokens + non-default temperature — the client
    retries once with max_completion_tokens and no temperature."""
    reg = _registry_with(Provider("openai", "https://api.openai.com/v1", "sk-1"))
    bad = _http_response(status=400, body={"error": {"message": "Unsupported parameter: 'max_tokens'"}})
    bad.raise_for_status = MagicMock()  # not reached — retried before raising
    good = _http_response(body={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    http = MagicMock()
    http.post = AsyncMock(side_effect=[bad, good])
    out = await CloudLLMClient(reg, http).chat(
        "openai/gpt-5-mini", [{"role": "user", "content": "x"}], None, max_tokens=512)
    assert out["message"]["content"] == "ok"
    retry_body = http.post.call_args_list[1].kwargs["json"]
    assert "max_tokens" not in retry_body
    assert retry_body["max_completion_tokens"] == 512
    assert "temperature" not in retry_body


@pytest.mark.asyncio
async def test_list_models_filters_non_chat_models():
    """Responses-API-only tiers (-pro, codex) and media/embedding models 404 on
    /chat/completions — they must not appear in the dropdown."""
    reg = _registry_with(Provider("openai", "https://api.openai.com/v1", "sk-1"))
    http = MagicMock()
    http.get = AsyncMock(return_value=_http_response(body={"data": [
        {"id": "gpt-5.5"}, {"id": "gpt-5.5-pro"}, {"id": "whisper-1"},
        {"id": "tts-1-hd"}, {"id": "text-embedding-3-large"}, {"id": "sora-2"},
        {"id": "gpt-5.1-codex"}, {"id": "o3-deep-research"}, {"id": "gpt-4o"},
    ]}))
    models = await CloudLLMClient(reg, http).list_models()
    assert models == ["openai/gpt-4o", "openai/gpt-5.5"]
