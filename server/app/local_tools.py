"""Local tools — server-side actions exposed to the LLM as MCP-shaped tools.

These let the LLM control the RPi UI/audio (theme, volume, mute) and
query HA-derived data (sun times) using the same tool-calling pipeline
used for external MCP servers. Implements the same public interface as
MCPClient so it can be registered with MultiMCPClient.
"""

import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("hal.local_tools")


class LocalToolsClient:
    """A pseudo-MCP client whose tools are local Python functions."""

    def __init__(self):
        # tool_name -> (schema_dict, async handler)
        self._tools: dict[str, tuple[dict, Callable[[dict], Awaitable[str]]]] = {}

    def register(self, name: str, description: str, parameters: dict, handler: Callable[[dict], Awaitable[str]]):
        """Register a tool. parameters is a JSON schema (object type)."""
        schema = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        self._tools[name] = (schema, handler)
        log.info(f"Registered local tool: {name}")

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def tools_for_llm(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["input_schema"],
                },
            }
            for s, _ in self._tools.values()
        ]

    def get_tool_descriptions_text(self) -> str:
        lines = []
        for s, _ in self._tools.values():
            desc = s["description"][:120] if s["description"] else "No description"
            lines.append(f"- {s['name']}: {desc}")
        return "\n".join(lines)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if name not in self._tools:
            return f"Error: Unknown local tool '{name}'"
        _, handler = self._tools[name]
        try:
            return await handler(arguments or {})
        except Exception as e:
            log.error(f"Local tool '{name}' failed: {e}")
            return f"Error calling {name}: {e}"

    async def disconnect(self):
        # Nothing to clean up
        pass
