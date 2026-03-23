"""MCP client for Home Assistant integration.

Connects to an existing HA MCP server over HTTP (Streamable HTTP / SSE transport),
discovers available tools, and executes tool calls on behalf of the LLM.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("hal.mcp")


class MCPClient:
    """Manages a persistent connection to the HA MCP server."""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._tools_for_llm: list[dict] = []
        self._context_manager = None
        self._read = None
        self._write = None

    async def connect(self):
        """Connect to the MCP server and discover tools."""
        log.info(f"Connecting to MCP server at {self.server_url}...")

        self._context_manager = streamablehttp_client(self.server_url)
        self._read, self._write, _ = await self._context_manager.__aenter__()

        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()

        # Discover available tools
        tools_result = await self._session.list_tools()
        self._tools = []
        self._tools_for_llm = []

        for tool in tools_result.tools:
            tool_info = {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
            }
            self._tools.append(tool_info)

            # Format for Ollama tool-calling (OpenAI-compatible function format)
            self._tools_for_llm.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema if hasattr(tool, "inputSchema") else {"type": "object", "properties": {}},
                },
            })

        log.info(f"Discovered {len(self._tools)} MCP tools: {[t['name'] for t in self._tools]}")

    async def disconnect(self):
        """Clean up the MCP connection."""
        if self._session:
            await self._session.__aexit__(None, None, None)
        if self._context_manager:
            await self._context_manager.__aexit__(None, None, None)

    @property
    def tools_for_llm(self) -> list[dict]:
        """Return tool definitions formatted for Ollama's tool-calling API."""
        return self._tools_for_llm

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self._tools]

    def get_tool_descriptions_text(self) -> str:
        """Return a plain-text summary of available tools for the system prompt."""
        lines = []
        for t in self._tools:
            desc = t["description"][:120] if t["description"] else "No description"
            lines.append(f"- {t['name']}: {desc}")
        return "\n".join(lines)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """
        Execute an MCP tool call and return the result as a string.

        Returns the text content of the tool result, or an error message.
        """
        if not self._session:
            return "Error: MCP client not connected"

        if name not in self.tool_names:
            return f"Error: Unknown tool '{name}'"

        log.info(f"Calling MCP tool: {name}({json.dumps(arguments or {})})")

        try:
            result = await self._session.call_tool(name, arguments or {})

            # Extract text from result content
            text_parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    text_parts.append(content.text)
                else:
                    text_parts.append(str(content))

            result_text = "\n".join(text_parts)
            log.info(f"MCP tool result ({name}): {result_text[:200]}...")
            return result_text

        except Exception as e:
            error_msg = f"Error calling tool {name}: {e}"
            log.error(error_msg)
            return error_msg
