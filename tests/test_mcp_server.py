"""Regression: HAL's MCP server must expose the kiosk display tools.

OpenClaw reaches the kiosk ONLY through this MCP server — a local tool that
isn't re-declared here is invisible to the agentic path, so a routed turn
("show me the conversation log" → openclaw) degrades into a chatty text
answer instead of opening the view. Pin the display tools' presence and that
they dispatch into state.local_tools."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.app.mcp_server import build_mcp_server


def _tools(mcp):
    return set(mcp._tool_manager._tools)


def test_exposes_kiosk_display_tools():
    mcp = build_mcp_server(SimpleNamespace(local_tools=None))
    names = _tools(mcp)
    assert {
        "show_calendar", "hide_calendar",
        "show_conversation_log", "hide_conversation_log",
        "show_photo_frame", "hide_photo_frame",
        "start_timer", "cancel_timer", "list_timers",
    } <= names


@pytest.mark.asyncio
async def test_conversation_log_tools_dispatch_to_local_tools():
    local = SimpleNamespace(
        tool_names={"show_conversation_log", "hide_conversation_log"},
        call_tool=AsyncMock(return_value="Conversation log shown"),
    )
    mcp = build_mcp_server(SimpleNamespace(local_tools=local))
    out = await mcp._tool_manager._tools["show_conversation_log"].fn()
    assert out == "Conversation log shown"
    local.call_tool.assert_awaited_once_with("show_conversation_log", {})
