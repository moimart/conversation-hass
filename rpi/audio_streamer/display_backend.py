"""Display power (DPMS) backend dispatcher.

The PAL kiosk runs a browser session on the host (RPi with labwc /
Wayland; x86 Arch with X11 or wlroots) — and we want to actually power
down the panel when no one's looking, not just paint a black overlay.

This module abstracts over three different binaries the host might
have:

  - wlr-randr   — Wayland (wlroots compositors: labwc, sway, hyprland)
  - xset        — X11 (any traditional Linux desktop)
  - vcgencmd    — Raspberry Pi firmware command, fallback when the
                  Wayland route somehow isn't available but we still
                  want real DPMS

At process start `detect_backend()` picks the first one that's
both installed AND has its required env/sockets reachable. Calling
`.set(True|False)` flips the display; `.state()` returns the live
state when the backend can query it (None when it can't — most
useful for diagnostics, not the control loop).

Why a backend dispatcher and not just "wlr-randr": the goal is to
keep the same audio_streamer container image working unchanged on
RPi (Wayland today) and on a future x86 Arch box that might be
X11. Detection at startup is one-shot and cheap.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Optional

log = logging.getLogger("hal.display")


class DisplayBackend:
    """Abstract base. Subclasses must implement set + name."""

    name: str = "?"

    def set(self, on: bool) -> None:
        raise NotImplementedError

    def state(self) -> Optional[bool]:
        """Return True/False or None when unknown. Best-effort only.
        The server tracks the authoritative desired state; this is for
        sanity-check / debug routes."""
        return None


class WlrRandrBackend(DisplayBackend):
    """wlr-randr — wlroots Wayland compositor (labwc, sway, hyprland)."""

    name = "wlr-randr"

    def __init__(self, output: str):
        self.output = output

    @classmethod
    def detect(cls) -> Optional["WlrRandrBackend"]:
        if not shutil.which("wlr-randr"):
            return None
        wd = os.environ.get("WAYLAND_DISPLAY")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not wd or not runtime:
            return None
        # Confirm the wayland socket is actually reachable from in here.
        sock_path = os.path.join(runtime, wd) if not wd.startswith("/") else wd
        if not os.path.exists(sock_path):
            return None
        # Pick the output: env override wins, else first non-Writeback line
        # in `wlr-randr` output that looks like "NAME \"…\"".
        forced = (os.environ.get("PAL_DISPLAY_OUTPUT") or "").strip()
        if forced:
            return cls(forced)
        try:
            out = subprocess.run(
                ["wlr-randr"], capture_output=True, text=True, timeout=4.0,
            ).stdout
        except Exception as e:
            log.warning(f"wlr-randr probe failed: {e}")
            return None
        for line in out.splitlines():
            m = re.match(r"^([A-Za-z][A-Za-z0-9-]+)\s+\"", line)
            if not m:
                continue
            name = m.group(1)
            if name.lower().startswith("writeback"):
                continue
            return cls(name)
        log.warning("wlr-randr installed but no output found in its listing")
        return None

    def set(self, on: bool) -> None:
        arg = "--on" if on else "--off"
        subprocess.run(
            ["wlr-randr", "--output", self.output, arg],
            check=False, timeout=6.0,
        )

    def state(self) -> Optional[bool]:
        try:
            out = subprocess.run(
                ["wlr-randr"], capture_output=True, text=True, timeout=4.0,
            ).stdout
        except Exception:
            return None
        # Find the block for our output and look for "Enabled: yes|no".
        in_block = False
        for line in out.splitlines():
            if re.match(rf"^{re.escape(self.output)}\b", line):
                in_block = True
                continue
            if in_block:
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    break  # next output's block
                m = re.search(r"Enabled:\s*(yes|no)", line)
                if m:
                    return m.group(1) == "yes"
        return None


class XsetBackend(DisplayBackend):
    """xset dpms — generic X11."""

    name = "xset"

    @classmethod
    def detect(cls) -> Optional["XsetBackend"]:
        if not shutil.which("xset"):
            return None
        if not os.environ.get("DISPLAY"):
            return None
        # The X11 socket lives at /tmp/.X11-unix; check it's present.
        if not os.path.isdir("/tmp/.X11-unix"):
            return None
        return cls()

    def set(self, on: bool) -> None:
        arg = "on" if on else "off"
        # `xset dpms force` immediately enters the requested DPMS state.
        # When forcing 'on' it also resets the idle timer so DPMS won't
        # blank again instantly.
        subprocess.run(["xset", "dpms", "force", arg], check=False, timeout=4.0)

    def state(self) -> Optional[bool]:
        try:
            out = subprocess.run(
                ["xset", "q"], capture_output=True, text=True, timeout=4.0,
            ).stdout
        except Exception:
            return None
        # `Monitor is On` / `Monitor is Off` / `Monitor is in Standby`/`Suspend`
        m = re.search(r"Monitor is\s+(\w+)", out)
        if not m:
            return None
        return m.group(1).lower() == "on"


class VcgencmdBackend(DisplayBackend):
    """Raspberry Pi firmware DPMS — fallback when Wayland isn't usable."""

    name = "vcgencmd"

    @classmethod
    def detect(cls) -> Optional["VcgencmdBackend"]:
        if not shutil.which("vcgencmd"):
            return None
        # /dev/vchiq is the firmware-IPC device. If it's not bind-mounted
        # in, vcgencmd will silently no-op or error.
        if not os.path.exists("/dev/vchiq"):
            return None
        return cls()

    def set(self, on: bool) -> None:
        arg = "1" if on else "0"
        subprocess.run(
            ["vcgencmd", "display_power", arg], check=False, timeout=4.0,
        )

    def state(self) -> Optional[bool]:
        try:
            out = subprocess.run(
                ["vcgencmd", "display_power"],
                capture_output=True, text=True, timeout=4.0,
            ).stdout
        except Exception:
            return None
        m = re.search(r"display_power=(\d+)", out)
        if not m:
            return None
        return m.group(1) != "0"


def detect_backend() -> Optional[DisplayBackend]:
    """Pick the first usable backend. Order matters: Wayland first
    (modern stack, works on RPi + x86 wlroots), then X11 (legacy/x86
    desktops), then RPi-firmware as the last-ditch fallback."""
    for cls in (WlrRandrBackend, XsetBackend, VcgencmdBackend):
        b = cls.detect()
        if b is not None:
            log.info(f"display backend selected: {b.name} ({type(b).__name__})")
            return b
    log.warning(
        "no display backend available — "
        "install wlr-randr/xset/vcgencmd and ensure the corresponding "
        "socket/device is bind-mounted into this container"
    )
    return None
