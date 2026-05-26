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
import threading
import time
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

    def set_transform(self, transform: str) -> bool:
        return False


class WlrRandrBackend(DisplayBackend):
    """wlr-randr — wlroots Wayland compositor (labwc, sway, hyprland).

    `--off` disables the output entirely; when we later re-enable it
    with `--on` the compositor forgets the previously-applied rotation
    / scale / position (because the output disappeared from the layout).
    So before every `--off` we snapshot the current Transform / Scale /
    Position, then re-apply them on `--on`. Initial snapshot happens at
    backend detection time so we have a baseline even if the very first
    call is `--off`.
    """

    name = "wlr-randr"

    # Values we mirror back on `--on`. None means "don't pass this flag".
    # wlr-randr accepts: normal | 90 | 180 | 270 | flipped | flipped-90 …
    def __init__(self, output: str):
        self.output = output
        self._transform: Optional[str] = None
        self._scale: Optional[str] = None
        self._position: Optional[str] = None
        self._snapshot_layout()

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

    def _snapshot_layout(self) -> None:
        """Capture the current Transform / Scale / Position so we can
        re-apply them after a future `--off` / `--on` cycle. Silent on
        failure — if we can't read it, we just won't restore it."""
        try:
            out = subprocess.run(
                ["wlr-randr"], capture_output=True, text=True, timeout=4.0,
            ).stdout
        except Exception:
            return
        in_block = False
        new_transform = self._transform
        new_scale = self._scale
        new_position = self._position
        for line in out.splitlines():
            if re.match(rf"^{re.escape(self.output)}\b", line):
                in_block = True
                continue
            if not in_block:
                continue
            if line and not line.startswith(" ") and not line.startswith("\t"):
                break  # next output's block
            m = re.search(r"Transform:\s*(\S+)", line)
            if m:
                new_transform = m.group(1)
                continue
            m = re.search(r"Scale:\s*([\d.]+)", line)
            if m:
                new_scale = m.group(1)
                continue
            m = re.search(r"Position:\s*(\d+),(\d+)", line)
            if m:
                new_position = f"{m.group(1)},{m.group(2)}"
                continue
        # Only update fields we actually read this time.
        self._transform = new_transform
        self._scale = new_scale
        self._position = new_position

    def set(self, on: bool) -> None:
        if not on:
            # Snapshot right before blanking so any rotation/scale the
            # user changed at runtime survives the next wake.
            self._snapshot_layout()
            log.info(
                f"wlr-randr OFF — captured "
                f"transform={self._transform!r} scale={self._scale!r} "
                f"position={self._position!r} for next wake"
            )
            subprocess.run(
                ["wlr-randr", "--output", self.output, "--off"],
                check=False, timeout=6.0,
            )
            return
        # Restore layout along with --on. wlr-randr lets us batch flags;
        # it applies them all in one configure_done.
        cmd = ["wlr-randr", "--output", self.output, "--on"]
        if self._transform:
            cmd += ["--transform", self._transform]
        if self._scale:
            cmd += ["--scale", self._scale]
        if self._position:
            cmd += ["--pos", self._position]
        log.info(f"wlr-randr ON — cmd={cmd!r}")
        r = subprocess.run(cmd, check=False, timeout=6.0,
                           capture_output=True, text=True)
        if r.returncode != 0 or (r.stderr or "").strip():
            log.warning(f"wlr-randr ON rc={r.returncode} stderr={r.stderr!r}")
        # When the panel goes deep into standby, the next HDMI hot-plug
        # detect can race the compositor: our --on --transform fires
        # before the EDID has been re-read, then labwc reapplies its
        # default rotation when the output re-attaches. Worker thread
        # polls the live state for the next ~15 s; if it doesn't match
        # what we saved, we re-apply. Bounded retries, then give up.
        threading.Thread(
            target=self._reconcile_after_wake,
            daemon=True,
            name="wlr-randr-reconcile",
        ).start()

    def set_transform(self, transform: str) -> bool:
        cmd = ["wlr-randr", "--output", self.output, "--transform", transform]
        log.info(f"wlr-randr set_transform — cmd={cmd!r}")
        r = subprocess.run(cmd, check=False, timeout=6.0,
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.warning(f"wlr-randr set_transform rc={r.returncode} stderr={r.stderr!r}")
            return False
        self._transform = transform
        return True

    # Reconciler window large enough to outlast a deep-standby HDMI
    # re-handshake (labwc has been seen resetting rotation up to ~30 s
    # after --on when the panel had been off all night).
    _RECONCILE_WINDOW_S = 90.0
    # Require the rotation to hold across this many consecutive polls
    # (≈ this many seconds) before we declare victory. Matching on the
    # very first poll right after --on is meaningless — labwc may
    # still re-clobber it once the EDID is re-read.
    _RECONCILE_STABLE_POLLS = 8

    def _reconcile_after_wake(self) -> None:
        """Re-apply the saved transform/scale/position until the live
        state has held for `_RECONCILE_STABLE_POLLS` consecutive polls
        (or up to `_RECONCILE_WINDOW_S` have passed)."""
        deadline = time.monotonic() + self._RECONCILE_WINDOW_S
        attempts = 0
        stable_streak = 0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                out = subprocess.run(
                    ["wlr-randr"], capture_output=True, text=True, timeout=4.0,
                ).stdout
            except Exception:
                continue
            live_transform = None
            live_scale = None
            live_enabled = None
            in_block = False
            for line in out.splitlines():
                if re.match(rf"^{re.escape(self.output)}\b", line):
                    in_block = True
                    continue
                if not in_block:
                    continue
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    break
                m = re.search(r"Enabled:\s*(yes|no)", line)
                if m:
                    live_enabled = m.group(1) == "yes"
                m = re.search(r"Transform:\s*(\S+)", line)
                if m:
                    live_transform = m.group(1)
                m = re.search(r"Scale:\s*([\d.]+)", line)
                if m:
                    live_scale = m.group(1)
            if live_enabled is False:
                # The panel is still off (deep standby) — wait more.
                stable_streak = 0
                continue
            want_t = self._transform
            want_s = self._scale
            t_ok = (not want_t) or live_transform == want_t
            s_ok = (not want_s) or live_scale == want_s
            if t_ok and s_ok:
                stable_streak += 1
                if stable_streak >= self._RECONCILE_STABLE_POLLS:
                    log.info(
                        f"wlr-randr reconcile: settled after {attempts} re-apply(s), "
                        f"stable for {stable_streak}s (transform={live_transform})"
                    )
                    return
                continue
            # Drift detected — re-apply.
            stable_streak = 0
            attempts += 1
            cmd = ["wlr-randr", "--output", self.output]
            if want_t:
                cmd += ["--transform", want_t]
            if want_s:
                cmd += ["--scale", want_s]
            if self._position:
                cmd += ["--pos", self._position]
            log.warning(
                f"wlr-randr reconcile: live transform={live_transform!r} "
                f"!= want={want_t!r}; reapplying ({attempts})"
            )
            subprocess.run(cmd, check=False, timeout=6.0)
        log.warning(
            f"wlr-randr reconcile: gave up after {self._RECONCILE_WINDOW_S:.0f}s "
            f"({attempts} re-apply(s), final stable_streak={stable_streak})"
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
        # Firmware IPC device. On older Pis it's /dev/vchiq; on Pi 5 /
        # Bookworm it's /dev/vcio. Either is good enough for vcgencmd.
        if not (os.path.exists("/dev/vchiq") or os.path.exists("/dev/vcio")):
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
