"""Context diagnostics: the rolling-context gauge + Clear Context reset.

context_stats() reports PAL's LOCAL LLM context (history + running summary +
persona, with a ~chars/4 token estimate vs num_ctx) and the compaction count;
clear_context() drops history + summary (NOT Shodh long-term memory)."""
from unittest.mock import AsyncMock

import pytest

from server.app.conversation import ConversationManager


def _cm(mock_mcp_client, mock_tts, num_ctx=1000):
    return ConversationManager(
        wake_word="hey homie",
        ollama_host="http://fake:11434",
        ollama_model="m",
        mcp_client=mock_mcp_client,
        tts_engine=mock_tts,
        system_prompt="P" * 400,   # 400-char persona
        num_ctx=num_ctx,
    )


def test_context_stats_shape_and_estimate(mock_mcp_client, mock_tts):
    cm = _cm(mock_mcp_client, mock_tts, num_ctx=1000)
    cm.history = [{"role": "user", "content": "x" * 200},
                  {"role": "assistant", "content": "y" * 200}]
    cm._context_summary = "s" * 200
    s = cm.context_stats()
    # 400 history + 200 summary + 400 persona = 1000 chars -> ~250 tokens
    assert s["history_messages"] == 2
    assert s["history_chars"] == 400
    assert s["summary_chars"] == 200
    assert s["est_tokens"] == 250
    assert s["num_ctx"] == 1000
    assert s["pct_used"] == 25.0
    assert s["compactions"] == 0
    assert s["last_compaction"] is None


@pytest.mark.asyncio
async def test_clear_context_drops_history_and_summary(mock_mcp_client, mock_tts):
    cm = _cm(mock_mcp_client, mock_tts)
    cm.history = [{"role": "user", "content": "hi"}] * 5
    cm._context_summary = "long running summary"
    cm._awaiting_followup = True
    out = await cm.clear_context()
    assert cm.history == []
    assert cm._context_summary == ""
    assert cm._awaiting_followup is False
    assert out["history_messages"] == 0 and out["summary_chars"] == 0


@pytest.mark.asyncio
async def test_clear_context_keeps_longterm_memory(mock_mcp_client, mock_tts):
    """The reset must not call into Shodh — long-term memory is separate."""
    cm = _cm(mock_mcp_client, mock_tts)
    cm.memory = AsyncMock()
    cm.history = [{"role": "user", "content": "hi"}]
    await cm.clear_context()
    cm.memory.remember.assert_not_called()
    cm.memory.forget.assert_not_called() if hasattr(cm.memory, "forget") else None


@pytest.mark.asyncio
async def test_compaction_count_increments(mock_mcp_client, mock_tts, monkeypatch):
    cm = _cm(mock_mcp_client, mock_tts)
    cm.max_history = 4
    cm.compact_keep = 2
    cm.history = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    monkeypatch.setattr(cm, "_summarize_history", AsyncMock(return_value="a summary"))
    cm.memory = None
    await cm._compact_history()
    assert cm._compaction_count == 1
    assert cm._last_compaction_iso is not None
    assert cm.context_stats()["compactions"] == 1
