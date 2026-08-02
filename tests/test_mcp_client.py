"""Tests for the MCP client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.mcp_client import MCPClient


class TestMCPClientInit:
    def test_initial_state(self):
        client = MCPClient(server_url="http://example.com/mcp")
        assert client.server_url == "http://example.com/mcp"
        assert client._session is None
        assert client._tools == []
        assert client._tools_for_llm == []

    def test_tool_names_empty(self):
        client = MCPClient(server_url="http://x")
        assert client.tool_names == []

    def test_tools_for_llm_empty(self):
        client = MCPClient(server_url="http://x")
        assert client.tools_for_llm == []


class TestMCPClientToolDiscovery:
    def test_tool_names(self, mock_mcp_client):
        assert "ha_call_service" in mock_mcp_client.tool_names
        assert "ha_get_state" in mock_mcp_client.tool_names

    def test_tools_for_llm_format(self, mock_mcp_client):
        tools = mock_mcp_client.tools_for_llm
        assert len(tools) == 2
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_hidden_tool_excluded_from_llm_but_still_callable(self):
        """ha_get_camera_image is hidden from the LLM's tool list (so the model
        uses show_camera) but stays in tool_names so show_camera can invoke it."""
        client = MCPClient(server_url="http://x")
        client._tools = [
            {"name": "ha_get_camera_image", "description": "d", "input_schema": {}},
            {"name": "ha_call_service", "description": "d", "input_schema": {}},
        ]
        client._tools_for_llm = [
            {"type": "function", "function": {"name": "ha_get_camera_image", "description": "d", "parameters": {}}},
            {"type": "function", "function": {"name": "ha_call_service", "description": "d", "parameters": {}}},
        ]
        llm_names = [t["function"]["name"] for t in client.tools_for_llm]
        assert "ha_get_camera_image" not in llm_names      # hidden from the model
        assert "ha_call_service" in llm_names
        assert "ha_get_camera_image" in client.tool_names  # still callable internally

    def test_get_tool_descriptions_text(self, mock_mcp_client):
        text = mock_mcp_client.get_tool_descriptions_text()
        assert "ha_call_service" in text
        assert "ha_get_state" in text
        # Each tool on its own line
        assert text.count("\n") == 1  # 2 tools = 1 newline between them

    def test_descriptions_truncated_at_120(self):
        client = MCPClient(server_url="http://x")
        client._tools = [{"name": "tool1", "description": "A" * 200, "input_schema": {}}]
        text = client.get_tool_descriptions_text()
        # "- tool1: " + 120 chars
        desc_part = text.split(": ", 1)[1]
        assert len(desc_part) == 120


class TestMCPClientCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        client = MCPClient(server_url="http://x")
        result = await client.call_tool("anything", {})
        assert "not connected" in result

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self, mock_mcp_client):
        result = await mock_mcp_client.call_tool("nonexistent_tool", {})
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_call_tool_success(self, mock_mcp_client):
        # Mock the session.call_tool return value
        mock_content = MagicMock()
        mock_content.text = "Light turned on"
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_mcp_client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await mock_mcp_client.call_tool("ha_call_service", {"domain": "light", "service": "turn_on"})
        assert result == "Light turned on"
        mock_mcp_client._session.call_tool.assert_awaited_once_with("ha_call_service", {"domain": "light", "service": "turn_on"})

    @pytest.mark.asyncio
    async def test_call_tool_with_no_arguments(self, mock_mcp_client):
        mock_content = MagicMock()
        mock_content.text = "OK"
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_mcp_client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await mock_mcp_client.call_tool("ha_get_state")
        # Should pass {} as default arguments
        mock_mcp_client._session.call_tool.assert_awaited_once_with("ha_get_state", {})

    @pytest.mark.asyncio
    async def test_call_tool_multiple_content_blocks(self, mock_mcp_client):
        block1 = MagicMock()
        block1.text = "Line 1"
        block2 = MagicMock()
        block2.text = "Line 2"
        mock_result = MagicMock()
        mock_result.content = [block1, block2]
        mock_mcp_client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await mock_mcp_client.call_tool("ha_call_service", {})
        assert result == "Line 1\nLine 2"

    @pytest.mark.asyncio
    async def test_call_tool_non_text_content(self, mock_mcp_client):
        # Create object without .text attribute using a simple class
        class NoTextContent:
            def __str__(self):
                return "raw-content"

        block = NoTextContent()
        mock_result = MagicMock()
        mock_result.content = [block]
        mock_mcp_client._session.call_tool = AsyncMock(return_value=mock_result)

        result = await mock_mcp_client.call_tool("ha_call_service", {})
        assert "raw-content" in result

    @pytest.mark.asyncio
    async def test_call_tool_exception(self, mock_mcp_client):
        mock_mcp_client._session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))

        result = await mock_mcp_client.call_tool("ha_call_service", {})
        assert "Error calling tool" in result
        assert "connection lost" in result


class TestMCPClientConnect:
    @pytest.mark.asyncio
    async def test_connect_discovers_tools(self):
        """Test that connect() calls list_tools and populates internal state."""
        mock_tool = MagicMock()
        mock_tool.name = "ha_test_tool"
        mock_tool.description = "A test tool"
        mock_tool.inputSchema = {"type": "object", "properties": {"x": {"type": "string"}}}

        mock_tools_result = MagicMock()
        mock_tools_result.tools = [mock_tool]

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_tools_result)
        # `async with ClientSession(...) as session` binds the __aenter__ result,
        # so the mock must hand back itself.
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)

        with patch("server.app.mcp_client.streamablehttp_client") as mock_transport, \
             patch("server.app.mcp_client.ClientSession", return_value=mock_session):

            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock(), None))
            mock_transport.return_value = mock_cm

            client = MCPClient(server_url="http://fake")
            await client.connect()

            assert "ha_test_tool" in client.tool_names
            assert len(client.tools_for_llm) == 1
            assert client.tools_for_llm[0]["function"]["name"] == "ha_test_tool"

            await client.disconnect()


class _RecordingCM:
    """Async CM that records which asyncio task entered and exited it.

    Stands in for `streamablehttp_client` / `ClientSession`, whose anyio cancel
    scopes must be entered and exited in the SAME task.
    """

    def __init__(self, aenter_result=None):
        self.enter_task = None
        self.exit_task = None
        self._aenter_result = aenter_result

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self._aenter_result if self._aenter_result is not None else self

    async def __aexit__(self, *exc):
        self.exit_task = asyncio.current_task()
        return False


class _FakeSession(_RecordingCM):
    def __init__(self, tools_result):
        super().__init__()
        self._tools_result = tools_result
        self.initialize = AsyncMock()

    async def list_tools(self):
        return self._tools_result


def _tools_result(name="ha_test_tool"):
    tool = MagicMock()
    tool.name = name
    tool.description = "A test tool"
    tool.inputSchema = {"type": "object", "properties": {}}
    res = MagicMock()
    res.tools = [tool]
    return res


class TestMCPClientDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_no_session(self):
        client = MCPClient(server_url="http://x")
        # Should not raise
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_scopes_enter_and_exit_in_the_same_task(self):
        """Regression: the transport/session cancel scopes must be entered and
        exited by ONE task (the owner), never by whoever calls connect/disconnect.

        Driving `__aexit__` from a different task raises anyio's "Attempted to
        exit cancel scope in a different task than it was entered in", which
        leaves the scope re-delivering cancellation forever — a livelock that
        pegged a whole CPU core in production until the container was restarted.
        """
        transport = _RecordingCM(aenter_result=(AsyncMock(), AsyncMock(), None))
        session = _FakeSession(_tools_result())

        with patch("server.app.mcp_client.streamablehttp_client", return_value=transport), \
             patch("server.app.mcp_client.ClientSession", return_value=session):
            client = MCPClient(server_url="http://fake")
            await client.connect()
            assert "ha_test_tool" in client.tool_names
            caller = asyncio.current_task()
            await client.disconnect()

        # entered and exited by the same task ...
        assert transport.enter_task is not None
        assert transport.enter_task is transport.exit_task
        assert session.enter_task is session.exit_task
        # ... and that task is the owner, NOT the one calling connect/disconnect
        assert transport.enter_task is not caller
        assert client._session is None

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent(self):
        transport = _RecordingCM(aenter_result=(AsyncMock(), AsyncMock(), None))
        session = _FakeSession(_tools_result())
        with patch("server.app.mcp_client.streamablehttp_client", return_value=transport), \
             patch("server.app.mcp_client.ClientSession", return_value=session):
            client = MCPClient(server_url="http://fake")
            await client.connect()
            await client.disconnect()
            await client.disconnect()   # must not raise
        assert client._session is None

    @pytest.mark.asyncio
    async def test_connect_raises_when_transport_fails(self):
        """A failing transport must surface as an exception (add_server catches
        it) rather than leaving a half-open session behind."""
        with patch("server.app.mcp_client.streamablehttp_client",
                   side_effect=RuntimeError("502 Bad Gateway")):
            client = MCPClient(server_url="http://fake")
            with pytest.raises(RuntimeError):
                await client.connect()
        assert client._session is None
        assert client.tool_names == []
