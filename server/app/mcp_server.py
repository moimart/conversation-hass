"""MCP server exposing HAL's local tools to external agents (e.g. OpenClaw).

Mounted on the main FastAPI app at /mcp as a Starlette sub-application
using the Streamable HTTP transport. External MCP clients (like
OpenClaw's mcporter) connect to http://<hal>:8765/mcp to discover and
call HAL's tools natively — no curl or SKILL.md needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal.mcp_server")

_mcp: FastMCP | None = None


def build_mcp_server(state: "AppState") -> FastMCP:
    """Create the MCP server and register HAL's tools on it.

    Called once during lifespan startup, after local_tools are built.
    The returned FastMCP instance's .streamable_http_app() is mounted
    on the FastAPI app.
    """
    global _mcp
    mcp = FastMCP(
        "HAL Voice Assistant",
        instructions=(
            "HAL is a voice assistant kiosk with a display (the orb). "
            "Use these tools to control volume, mute, display power, "
            "show images/videos/cameras on the orb, open the photo frame "
            "or calendar, and speak text aloud."
        ),
    )

    # Bridge each local tool into the MCP server. The local_tools
    # registry has (schema, async_handler) pairs. We wrap each handler
    # into an MCP tool with the same name, description, and JSON schema.
    if state.local_tools:
        for name, (schema, handler) in state.local_tools._tools.items():
            _register_tool(mcp, name, schema, handler, state)

    # Also register a few direct-action tools that aren't in local_tools
    # but are useful for external agents.

    @mcp.tool(
        name="speak",
        description=(
            "Make HAL speak text verbatim through the kiosk speaker. "
            "Use for announcements, notifications, or when the exact "
            "wording matters. The text goes straight to TTS — no LLM."
        ),
    )
    async def speak(text: str) -> str:
        if not text.strip():
            return "Error: empty text"
        if state.tts_engine and state.audio_websocket:
            audio = await state.tts_engine.synthesize(text)
            if audio:
                try:
                    ws = state.audio_websocket
                    await ws.send_json({"type": "response", "text": text})
                    await ws.send_json({"type": "tts_start", "size": len(audio)})
                    chunk_size = 8192
                    for i in range(0, len(audio), chunk_size):
                        await ws.send_bytes(audio[i : i + chunk_size])
                    await ws.send_json({"type": "tts_end"})
                    return f"Speaking: {text[:80]}"
                except Exception as e:
                    return f"TTS send failed: {e}"
            return "TTS synthesis returned no audio"
        return "TTS or kiosk not available"

    @mcp.tool(
        name="show_image_url",
        description=(
            "Display an image from a URL on the kiosk orb for a given "
            "duration. Use for showing pictures, diagrams, photos."
        ),
    )
    async def show_image_url(url: str, duration_s: int = 60) -> str:
        from .main import _push_image_payload
        try:
            result = await _push_image_payload(state, url, default_duration=duration_s)
            return result
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool(
        name="play_video_url",
        description=(
            "Play a video from a URL on the kiosk orb. Supports MP4, "
            "WebM, HLS. Audio ducks when HAL speaks."
        ),
    )
    async def play_video_url(
        url: str,
        duration_s: int = 300,
        loop: bool = False,
        muted: bool = False,
    ) -> str:
        from .main import _dispatch_play_video
        ok = await _dispatch_play_video(
            state, url, duration_s=duration_s, loop=loop, muted=muted,
        )
        return "Playing video" if ok else "Kiosk not connected"

    @mcp.tool(
        name="show_qr_code",
        description=(
            "Display a QR code on the kiosk orb for a URL or text. "
            "Use when the user needs to open a link on their phone."
        ),
    )
    async def show_qr_code(data: str, duration_s: int = 30) -> str:
        import base64
        from .qr_code import generate_qr_png
        from .main import _dispatch_show_image
        qr_bytes = generate_qr_png(data)
        if not qr_bytes:
            return "QR generation failed"
        b64 = base64.b64encode(qr_bytes).decode("ascii")
        ok = await _dispatch_show_image(
            state, b64, "image/png", duration_s, entity_id="qr-code",
        )
        return "QR code displayed" if ok else "Kiosk not connected"

    _mcp = mcp
    tool_count = len(mcp.list_tools())
    log.info(f"MCP server built with {tool_count} tools")
    return mcp


def _register_tool(
    mcp: FastMCP,
    name: str,
    schema: dict,
    handler,
    state: "AppState",
) -> None:
    """Bridge a local_tools entry into the FastMCP server."""
    description = schema.get("description", "")
    input_schema = schema.get("input_schema", {})
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    # FastMCP's @mcp.tool expects a function with typed params.
    # We create a wrapper that accepts **kwargs and routes to the handler.
    async def wrapper(**kwargs) -> str:
        return await handler(kwargs)

    # Register with the raw add_tool API for dynamic schemas
    from mcp.server.fastmcp.tools import Tool

    tool = Tool(
        fn=wrapper,
        name=name,
        description=description,
        parameters=input_schema,
    )
    mcp._tool_manager._tools[name] = tool
