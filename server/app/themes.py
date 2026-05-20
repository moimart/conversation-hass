"""Plug-in theme registry.

Each theme is a directory under THEMES_DIR with:
    manifest.json   (required)
    theme.css       (required) — body.theme-<name> { CSS variable overrides }
    effect.js       (optional, ES module) — see kiosk loader for the API

manifest.json shape:
    {
      "name": "matrix",                       # must match directory name
      "display_name": "Matrix — Phosphor green",
      "description": "Phosphor green on pitch black",
      "version": "1.0.0",
      "kind": "dark" | "light",
      "effect": "effect.js"                   # optional; presence implies dynamic effect
    }

The registry polls the themes directory on a schedule. When the set of
themes (or any manifest) changes, the on_change callback fires. Server
code uses that to republish MQTT discovery and notify kiosks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("hal.themes")


# Well-known state-video keys. Only these are passed through to the
# kiosk; unknown keys in the manifest are dropped silently. The kiosk
# falls back to the static orb for any state that has no entry.
_STATE_VIDEO_KEYS = ("idle", "listening", "processing", "speaking")


@dataclass(frozen=True)
class Theme:
    name: str
    display_name: str
    description: str
    version: str
    kind: str               # "dark" or "light"
    has_effect: bool
    dir_path: str
    # Optional map of state -> filename inside the theme dir. Filenames
    # are validated to exist at load time; missing entries are dropped.
    state_videos: tuple[tuple[str, str], ...]
    # Hash of (manifest contents + theme.css mtime + effect.js mtime +
    # state-video mtimes) for diffing.
    fingerprint: str

    def to_public(self) -> dict[str, Any]:
        """JSON-serializable summary for /api/themes."""
        d: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "kind": self.kind,
            "has_effect": self.has_effect,
        }
        if self.state_videos:
            d["state_videos"] = dict(self.state_videos)
        return d


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_theme(dir_path: str, name: str) -> Theme | None:
    manifest_path = os.path.join(dir_path, "manifest.json")
    css_path = os.path.join(dir_path, "theme.css")
    if not os.path.isfile(manifest_path) or not os.path.isfile(css_path):
        return None
    try:
        with open(manifest_path) as f:
            m = json.load(f)
    except Exception as e:
        log.warning(f"theme {name!r}: cannot read manifest.json: {e}")
        return None
    if not isinstance(m, dict):
        log.warning(f"theme {name!r}: manifest.json must be a JSON object")
        return None
    declared_name = str(m.get("name") or name).strip()
    if declared_name != name:
        log.warning(
            f"theme {name!r}: manifest.name {declared_name!r} doesn't match "
            f"directory name; using directory name"
        )
    display_name = str(m.get("display_name") or name)
    description = str(m.get("description") or "")
    version = str(m.get("version") or "0.0.0")
    kind = str(m.get("kind") or "dark").lower()
    if kind not in ("dark", "light"):
        kind = "dark"
    effect_filename = m.get("effect")
    effect_path = os.path.join(dir_path, effect_filename) if effect_filename else ""
    has_effect = bool(effect_filename) and os.path.isfile(effect_path)

    # Parse optional state_videos. Only the four well-known keys are
    # accepted; each entry's filename must exist inside the theme dir.
    # Result is stored as a tuple-of-pairs so the dataclass stays
    # hashable/frozen-friendly.
    state_videos_pairs: list[tuple[str, str]] = []
    raw_sv = m.get("state_videos")
    if isinstance(raw_sv, dict):
        for key in _STATE_VIDEO_KEYS:
            val = raw_sv.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            fn = val.strip()
            # No path traversal — the kiosk fetches via /themes/<name>/<file>
            if "/" in fn or "\\" in fn or fn in ("", ".", ".."):
                log.debug(f"theme {name!r}: state_videos[{key!r}]={fn!r} rejected (path traversal)")
                continue
            if not os.path.isfile(os.path.join(dir_path, fn)):
                log.debug(f"theme {name!r}: state_videos[{key!r}]={fn!r} missing on disk; dropped")
                continue
            state_videos_pairs.append((key, fn))

    fp_parts = [
        json.dumps(m, sort_keys=True),
        str(_file_mtime(css_path)),
        str(_file_mtime(effect_path)) if has_effect else "",
    ]
    # Include video mtimes in the fingerprint so re-encoding a clip
    # triggers a themes_changed broadcast.
    for _, fn in state_videos_pairs:
        fp_parts.append(str(_file_mtime(os.path.join(dir_path, fn))))
    fingerprint = "|".join(fp_parts)
    return Theme(
        name=name,
        display_name=display_name,
        description=description,
        version=version,
        kind=kind,
        has_effect=has_effect,
        dir_path=dir_path,
        state_videos=tuple(state_videos_pairs),
        fingerprint=fingerprint,
    )


class ThemeRegistry:
    """Scans a directory of theme plug-ins and notifies on changes."""

    def __init__(self, root: str):
        self.root = root
        self._themes: dict[str, Theme] = {}
        self._on_change: list[Callable[[list[Theme]], Awaitable[None]]] = []
        self._poll_task: asyncio.Task | None = None

    def add_listener(self, cb: Callable[[list[Theme]], Awaitable[None]]) -> None:
        self._on_change.append(cb)

    @property
    def themes(self) -> list[Theme]:
        return sorted(self._themes.values(), key=lambda t: t.name)

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.themes]

    def get(self, name: str) -> Theme | None:
        return self._themes.get(name)

    def static_path(self, name: str, filename: str) -> str | None:
        """Absolute path to a file inside a theme's directory (or None if
        traversal-suspect / outside the registry root)."""
        theme = self._themes.get(name)
        if not theme:
            return None
        # Reject anything that tries to escape the theme dir.
        if filename in ("", ".", "..") or "/" in filename or "\\" in filename:
            return None
        target = os.path.realpath(os.path.join(theme.dir_path, filename))
        if not target.startswith(os.path.realpath(theme.dir_path) + os.sep) and target != os.path.realpath(theme.dir_path):
            return None
        if not os.path.isfile(target):
            return None
        return target

    def scan(self) -> bool:
        """Re-read the themes directory. Returns True if anything changed."""
        new: dict[str, Theme] = {}
        if os.path.isdir(self.root):
            for entry in sorted(os.listdir(self.root)):
                if entry.startswith("."):
                    continue
                sub = os.path.join(self.root, entry)
                if not os.path.isdir(sub):
                    continue
                t = _load_theme(sub, entry)
                if t is not None:
                    new[entry] = t
        # Diff by fingerprint.
        changed = (
            set(new.keys()) != set(self._themes.keys())
            or any(new[k].fingerprint != self._themes[k].fingerprint for k in new)
        )
        self._themes = new
        return changed

    async def start_polling(self, interval_s: float = 10.0) -> None:
        """Spin up a background task that re-scans every interval_s."""
        if self._poll_task and not self._poll_task.done():
            return

        async def loop():
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        if self.scan():
                            log.info(f"themes: change detected, now have {self.names}")
                            for cb in list(self._on_change):
                                try:
                                    await cb(self.themes)
                                except Exception as e:
                                    log.warning(f"themes: listener raised: {e}")
                    except Exception as e:
                        log.debug(f"themes: poll iteration failed: {e}")
            except asyncio.CancelledError:
                raise

        self._poll_task = asyncio.create_task(loop())

    async def stop_polling(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
