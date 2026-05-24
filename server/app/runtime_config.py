"""Live runtime config — values that can be changed from HA without a restart.

Bootstraps from environment variables on first run, then becomes the
source of truth. Atomic writes (tmp + rename) so a partial write or
power loss never leaves a corrupt JSON file. Schema is intentionally
flat and dict-shaped so adding new keys later is a one-line change in
the DEFAULT_KEYS dict.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("hal.runtime_config")

# Keys we manage and the env var each falls back to on first boot.
# Add new entries here when more settings move from .env to live config.
DEFAULT_KEYS: dict[str, tuple[str, Any]] = {
    "theme_day":    ("THEME_DAY", "birch"),
    "theme_night":  ("THEME_NIGHT", "dark"),
    "tts_voice":    ("WYOMING_TTS_VOICE", ""),
    "wake_word":    ("WAKE_WORD", "hey hal"),
    "ollama_model": ("OLLAMA_MODEL", "llama3.2"),
    "fallback_ollama_model": ("FALLBACK_OLLAMA_MODEL", ""),
    "num_ctx":      ("OLLAMA_NUM_CTX", 32768),
    "auto_theme":   ("AUTO_THEME", True),
    "calendar_dismiss_seconds":  ("CALENDAR_DISMISS_SECONDS", 30),
    "calendar_default_source":   ("CALENDAR_DEFAULT_SOURCE", ""),
    "start_muted":               ("START_MUTED", False),
    "photo_frame_entity":        ("PHOTO_FRAME_ENTITY", ""),
    "display_auto_off_seconds":  ("DISPLAY_AUTO_OFF_SECONDS", 0),
    "photo_frame_idle_minutes":  ("PHOTO_FRAME_IDLE_MINUTES", 0),
    "openclaw_enabled":          ("OPENCLAW_ENABLED", False),
    "openclaw_gateway_url":      ("OPENCLAW_GATEWAY_URL", ""),
    "openclaw_gateway_password": ("OPENCLAW_GATEWAY_PASSWORD", ""),
    "openclaw_webhook_url":      ("OPENCLAW_WEBHOOK_URL", ""),
}


def _coerce_default(env_var: str, fallback: Any) -> Any:
    """Read env_var; if absent, use fallback. Coerce booleans/ints from strings."""
    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return fallback
    if isinstance(fallback, bool):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(fallback, int):
        try:
            return int(raw.strip())
        except ValueError:
            log.warning(f"{env_var}={raw!r} is not an int, using fallback {fallback!r}")
            return fallback
    return raw


class RuntimeConfig:
    """File-backed live config. Reads/writes a single flat JSON object."""

    def __init__(self, path: str):
        self.path = path
        self._values: dict[str, Any] = {}

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def load(self) -> dict[str, Any]:
        """Load from disk; bootstrap from env if file is missing/malformed.

        Returns the merged dict. Always populates self._values with every
        DEFAULT_KEYS entry (file values take precedence over env defaults).
        """
        env_defaults = {
            key: _coerce_default(env_var, fallback)
            for key, (env_var, fallback) in DEFAULT_KEYS.items()
        }

        file_values: dict[str, Any] = {}
        if os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    file_values = loaded
                else:
                    log.warning(f"runtime_config: {self.path} is not a JSON object — ignoring")
            except json.JSONDecodeError as e:
                log.warning(f"runtime_config: malformed JSON in {self.path}: {e} — falling back to env")
            except Exception as e:
                log.warning(f"runtime_config: failed to read {self.path}: {e} — falling back to env")

        # File values win for keys we know; env defaults fill in missing ones.
        # Unknown keys in the file are preserved (forward-compat for newer keys).
        merged: dict[str, Any] = dict(env_defaults)
        merged.update(file_values)
        self._values = merged

        # On first run (no file), persist the bootstrap so HA selects always
        # have a stable file to write back to.
        if not os.path.isfile(self.path):
            try:
                self.save()
                log.info(f"runtime_config: bootstrapped {self.path} from env")
            except Exception as e:
                log.warning(f"runtime_config: could not write initial {self.path}: {e}")
        return self._values

    def set(self, key: str, value: Any) -> None:
        """Update one key and persist atomically."""
        self._values[key] = value
        self.save()

    def update(self, **kv: Any) -> None:
        """Update multiple keys and persist once."""
        self._values.update(kv)
        self.save()

    def save(self) -> None:
        """Atomic write: tmp + rename in the same directory."""
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".runtime_config.", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._values, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
