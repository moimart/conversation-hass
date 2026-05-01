"""MCP client supporting multiple MCP servers.

Supports two transport types:
  - HTTP (Streamable HTTP): {"name": "...", "url": "https://..."}
  - stdio (subprocess):     {"name": "...", "command": "uvx", "args": [...], "env": {...}}

Discovers all available tools and routes tool calls to the correct server.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import stdio_client

log = logging.getLogger("hal.mcp")


class MCPClient:
    """Manages a persistent connection to an MCP server (HTTP or stdio)."""

    def __init__(self, server_url: str = "", command: str = "", args: list[str] | None = None, env: dict[str, str] | None = None):
        self.server_url = server_url
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._tools_for_llm: list[dict] = []
        self._context_manager = None

    @property
    def transport_type(self) -> str:
        return "stdio" if self.command else "http"

    async def connect(self):
        """Connect to the MCP server and discover tools."""
        if self.command:
            log.info(f"Starting stdio MCP server: {self.command} {' '.join(self.args)}")
            # Resolve env vars (replace $VAR with actual values from host env)
            resolved_env = {}
            for k, v in self.env.items():
                if isinstance(v, str) and v.startswith("$"):
                    resolved_val = os.environ.get(v[1:], "")
                    resolved_env[k] = resolved_val
                    log.info(f"  env {k}: ${v[1:]} → {'(set)' if resolved_val else '(empty)'}")
                else:
                    resolved_env[k] = v
                    log.info(f"  env {k}: (literal, {len(v)} chars)")

            full_env = {**os.environ, **resolved_env}
            # Verify the keys we care about are in the final env
            for k in self.env:
                val = full_env.get(k, "")
                log.info(f"  final env {k}: {'(set, ' + str(len(val)) + ' chars)' if val else '(MISSING)'}")

            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=full_env,
            )
            self._context_manager = stdio_client(server_params)
        else:
            log.info(f"Connecting to HTTP MCP server at {self.server_url}...")
            self._context_manager = streamablehttp_client(self.server_url)

        read, write = (await self._context_manager.__aenter__())[:2]

        self._session = ClientSession(read, write)
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

    async def call_tool_content(self, name: str, arguments: dict[str, Any] | None = None) -> list:
        """Like call_tool, but returns the raw MCP content list.

        Use this when a tool returns non-text content (e.g. ImageContent
        from ha_get_camera_image). Returns an empty list on error.
        """
        if not self._session or name not in self.tool_names:
            return []
        try:
            result = await self._session.call_tool(name, arguments or {})
            return list(result.content)
        except Exception as e:
            log.error(f"Error calling tool {name} for content: {e}")
            return []


class MultiMCPClient:
    """Wraps multiple MCPClient instances, merging tools and routing calls."""

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}  # name → client
        self._tool_to_server: dict[str, str] = {}  # tool_name → server_name

    async def add_server(self, name: str, url: str = "", command: str = "", args: list[str] | None = None, env: dict[str, str] | None = None):
        """Connect to an MCP server (HTTP or stdio) and register its tools."""
        client = MCPClient(server_url=url, command=command, args=args, env=env)
        desc = url or f"{command} {' '.join(args or [])}"
        try:
            await client.connect()
            self._clients[name] = client
            for tool_name in client.tool_names:
                if tool_name in self._tool_to_server:
                    existing = self._tool_to_server[tool_name]
                    log.warning(f"Tool '{tool_name}' from '{name}' shadows same tool from '{existing}'")
                self._tool_to_server[tool_name] = name
            log.info(f"MCP server '{name}' ({client.transport_type}): {len(client.tool_names)} tools from {desc}")
        except Exception as e:
            log.error(f"Failed to connect to MCP server '{name}' ({desc}): {e}")

    def register_client(self, name: str, client) -> None:
        """Register an already-initialized client (e.g. LocalToolsClient).

        The client must implement: tool_names, tools_for_llm,
        get_tool_descriptions_text(), call_tool(name, args), disconnect().
        """
        self._clients[name] = client
        for tool_name in client.tool_names:
            if tool_name in self._tool_to_server:
                existing = self._tool_to_server[tool_name]
                log.warning(f"Tool '{tool_name}' from '{name}' shadows same tool from '{existing}'")
            self._tool_to_server[tool_name] = name
        log.info(f"Registered client '{name}': {len(client.tool_names)} tools")

    async def disconnect_all(self):
        for name, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                log.warning(f"Error disconnecting MCP server '{name}': {e}")

    @property
    def tools_for_llm(self) -> list[dict]:
        tools = []
        for client in self._clients.values():
            tools.extend(client.tools_for_llm)
        return tools

    @property
    def tool_names(self) -> list[str]:
        return list(self._tool_to_server.keys())

    def get_tool_descriptions_text(self) -> str:
        lines = []
        for client in self._clients.values():
            text = client.get_tool_descriptions_text()
            if text:
                lines.append(text)
        return "\n".join(lines)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        server_name = self._tool_to_server.get(name)
        if not server_name:
            return f"Error: Unknown tool '{name}'"
        client = self._clients.get(server_name)
        if not client:
            return f"Error: Server '{server_name}' not connected"
        return await client.call_tool(name, arguments)

    async def call_tool_content(self, name: str, arguments: dict[str, Any] | None = None) -> list:
        """Route to the owning client's call_tool_content if available."""
        server_name = self._tool_to_server.get(name)
        if not server_name:
            return []
        client = self._clients.get(server_name)
        if not client or not hasattr(client, "call_tool_content"):
            return []
        return await client.call_tool_content(name, arguments)
