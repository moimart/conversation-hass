"""MCP server exposing HAL's tools to external MCP clients.

Mounted on the main FastAPI app at /mcp as a Starlette sub-application
using the Streamable HTTP transport. External MCP clients connect to
http://<hal>:8765/mcp to discover and call tools natively.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal.mcp_server")


def build_mcp_server(state: "AppState") -> FastMCP:
    """Create the MCP server with HAL's tools registered via decorators."""
    mcp = FastMCP(
        "PAL Voice Assistant",
        instructions=(
            "PAL is a voice assistant kiosk with a display (the orb). "
            "Use these tools to control volume, mute, display power, "
            "show images/videos/cameras on the orb, open the photo frame "
            "or calendar, and speak text aloud. When you execute an action, "
            "always respond with a brief spoken confirmation."
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    async def _call(name: str, args: dict) -> str:
        if state.local_tools and name in state.local_tools.tool_names:
            return await state.local_tools.call_tool(name, args)
        return f"Tool {name} not available"

    @mcp.tool(name="set_volume", description="Set the kiosk speaker volume. Use 'direction' (up/down) and optional 'step' (0.0-1.0, default 0.1).")
    async def set_volume(direction: str, step: float = 0.1) -> str:
        return await _call("audio_set_volume", {"direction": direction, "step": step})

    @mcp.tool(name="adjust_volume", description="Adjust volume to a specific level (0.0-1.0).")
    async def adjust_volume(level: float) -> str:
        return await _call("audio_adjust_volume", {"level": level})

    @mcp.tool(name="toggle_mute", description="Toggle the microphone mute on the kiosk.")
    async def toggle_mute() -> str:
        return await _call("audio_toggle_mute", {})

    @mcp.tool(name="set_theme", description="Switch the kiosk UI theme. Pass a theme name.")
    async def set_theme(name: str) -> str:
        return await _call("ui_set_theme", {"name": name})

    @mcp.tool(name="get_theme", description="Get the current kiosk UI theme name.")
    async def get_theme() -> str:
        return await _call("ui_get_theme", {})

    @mcp.tool(name="speak_verbatim", description="Make HAL speak text exactly as given through the kiosk speaker. Bypasses the LLM.")
    async def speak_verbatim(text: str) -> str:
        return await _call("speak_verbatim", {"text": text})

    @mcp.tool(name="show_camera", description="Show a snapshot from a HA camera entity on the kiosk orb.")
    async def show_camera(entity_id: str, duration_s: int = 150) -> str:
        return await _call("show_camera", {"entity_id": entity_id, "duration_s": duration_s})

    @mcp.tool(name="stream_camera", description="Open a live WebRTC stream from a HA camera entity on the kiosk orb.")
    async def stream_camera(entity_id: str, duration_s: int = 300) -> str:
        return await _call("stream_camera", {"entity_id": entity_id, "duration_s": duration_s})

    @mcp.tool(name="stream_rtsp", description="Stream an RTSP URL on the kiosk orb via go2rtc.")
    async def stream_rtsp(rtsp_url: str, duration_s: int = 300) -> str:
        return await _call("stream_rtsp", {"rtsp_url": rtsp_url, "duration_s": duration_s})

    @mcp.tool(name="show_image", description="Show an image (by URL) on the kiosk orb.")
    async def show_image(url: str, duration_s: int = 60) -> str:
        from .media import _push_image_payload
        try:
            return await _push_image_payload(state, url, default_duration=duration_s)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool(name="play_video", description="Play a video URL (MP4/WebM/HLS) on the kiosk orb.")
    async def play_video(url: str, duration_s: int = 300, loop: bool = False, muted: bool = False) -> str:
        return await _call("play_video", {"url": url, "duration_s": duration_s, "loop": loop, "muted": muted})

    @mcp.tool(name="stop_streaming", description="Stop any active video/camera stream on the kiosk orb.")
    async def stop_streaming() -> str:
        return await _call("stop_streaming", {})

    @mcp.tool(name="show_calendar", description="Pop up a calendar overlay on the kiosk. Views: month, week, day.")
    async def show_calendar(view: str = "month", calendar_name: str = "", anchor_date: str = "") -> str:
        return await _call("show_calendar", {"view": view, "calendar_name": calendar_name, "anchor_date": anchor_date})

    @mcp.tool(name="hide_calendar", description="Dismiss the calendar overlay.")
    async def hide_calendar() -> str:
        return await _call("hide_calendar", {})

    @mcp.tool(name="show_conversation_log", description="Open the full-screen conversation log view (the persistent history of requests, answers, and announcements) on the kiosk display.")
    async def show_conversation_log() -> str:
        return await _call("show_conversation_log", {})

    @mcp.tool(name="hide_conversation_log", description="Dismiss the conversation log view.")
    async def hide_conversation_log() -> str:
        return await _call("hide_conversation_log", {})

    @mcp.tool(name="start_timer", description="Start a countdown timer (kitchen-timer style). Pass duration_s as total seconds (e.g. '5 minutes' -> 300). Auto-named Timer 1, Timer 2, ...; every device announces when it finishes.")
    async def start_timer(duration_s: int) -> str:
        return await _call("start_timer", {"duration_s": duration_s})

    @mcp.tool(name="cancel_timer", description="Cancel a running timer by name ('Timer 2') or number ('2'); empty name cancels all timers.")
    async def cancel_timer(name: str = "") -> str:
        return await _call("cancel_timer", {"name": name})

    @mcp.tool(name="list_timers", description="List active timers and how much time each has left.")
    async def list_timers() -> str:
        return await _call("list_timers", {})

    @mcp.tool(name="show_photo_frame", description="Open the photo frame on the kiosk (full-screen ambient image with clock).")
    async def show_photo_frame(entity_id: str = "") -> str:
        return await _call("show_photo_frame", {"entity_id": entity_id})

    @mcp.tool(name="hide_photo_frame", description="Dismiss the photo frame.")
    async def hide_photo_frame() -> str:
        return await _call("hide_photo_frame", {})

    @mcp.tool(name="set_display_power", description="Turn the kiosk display on or off (real DPMS). State: 'on' or 'off'.")
    async def set_display_power(state_val: str) -> str:
        return await _call("set_display_power", {"state": state_val})

    @mcp.tool(name="set_photo_frame_idle_minutes", description="Set minutes of inactivity before auto-activating the photo frame. 0 disables.")
    async def set_photo_frame_idle_minutes(minutes: int) -> str:
        return await _call("set_photo_frame_idle_minutes", {"minutes": minutes})

    @mcp.tool(name="get_sun_times", description="Get sunrise/sunset times from Home Assistant.")
    async def get_sun_times() -> str:
        return await _call("get_sun_times", {})

    @mcp.tool(name="show_qr_code", description="Display a QR code on the kiosk orb for a URL or text.")
    async def show_qr_code(data: str, duration_s: int = 30) -> str:
        from .qr_code import generate_qr_png
        from .media import _dispatch_show_image
        qr_bytes = generate_qr_png(data)
        if not qr_bytes:
            return "QR generation failed"
        b64 = base64.b64encode(qr_bytes).decode("ascii")
        ok = await _dispatch_show_image(state, b64, "image/png", duration_s, entity_id="qr-code")
        return "QR code displayed" if ok else "Kiosk not connected"

    log.info(f"MCP server built with {len(mcp._tool_manager._tools)} tools")
    return mcp
