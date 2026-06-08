"""Push-notification provider credentials: APNs (.p8 auth key) + FCM (service
account), hot-reloaded by mtime — mirrors cloud_llm.ProviderRegistry exactly.

SECRETS: the file holds the APNs private key and the FCM service-account key.
Its contents are NEVER logged, never published, never returned by any API. Add
or rotate a provider by editing the file (default
/app/runtime/push_providers.json); the change is picked up on the next access
(mtime check), no restart needed.

Shape:
    {
      "apns": {"key_p8": "-----BEGIN PRIVATE KEY-----\\n...",
               "key_id": "ABC123", "team_id": "39E4GQM8LC",
               "bundle_id": "sh.martinez.pal.companion", "production": false},
      "fcm":  {"project_id": "pal-xxxx",
               "service_account": { ...the downloaded service-account json... }}
    }
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("hal.push_providers")

DEFAULT_PUSH_PROVIDERS_PATH = "/app/runtime/push_providers.json"


@dataclass
class ApnsConfig:
    key_p8: str        # the .p8 private-key PEM contents — NEVER log
    key_id: str
    team_id: str
    bundle_id: str
    production: bool = False

    def __repr__(self) -> str:  # keep the key out of any repr/log
        return (f"ApnsConfig(key_id={self.key_id!r}, team_id={self.team_id!r}, "
                f"bundle_id={self.bundle_id!r}, production={self.production}, key_p8=***)")


@dataclass
class FcmConfig:
    project_id: str
    service_account: dict      # the full service-account dict — NEVER log

    def __repr__(self) -> str:
        return f"FcmConfig(project_id={self.project_id!r}, service_account=***)"


class PushProviderRegistry:
    """Hot-reloadable APNs + FCM credentials. configured() is False until the
    file holds at least one usable provider, so the push layer no-ops on a
    fresh/unconfigured install."""

    def __init__(self, path: str = DEFAULT_PUSH_PROVIDERS_PATH):
        self.path = path
        self._mtime: float | None = None
        self._apns: ApnsConfig | None = None
        self._fcm: FcmConfig | None = None
        self._load_if_changed(force=True)

    def _load_if_changed(self, force: bool = False) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime

        apns: ApnsConfig | None = None
        fcm: FcmConfig | None = None
        if mtime is not None:
            try:
                with open(self.path) as f:
                    data = json.load(f)
                a = data.get("apns") or {}
                key_p8 = str(a.get("key_p8", "")).strip()
                key_id = str(a.get("key_id", "")).strip()
                team_id = str(a.get("team_id", "")).strip()
                bundle_id = str(a.get("bundle_id", "")).strip()
                if (key_p8 and key_p8 != "REPLACE_ME"
                        and key_id and team_id and bundle_id):
                    apns = ApnsConfig(key_p8, key_id, team_id, bundle_id,
                                      bool(a.get("production", False)))
                fc = data.get("fcm") or {}
                sa = fc.get("service_account")
                pid = str(fc.get("project_id", "")).strip()
                if isinstance(sa, dict) and not pid:
                    pid = str(sa.get("project_id", "")).strip()
                if (isinstance(sa, dict) and sa.get("private_key")
                        and sa.get("private_key") != "REPLACE_ME" and pid):
                    fcm = FcmConfig(pid, sa)
            except Exception as e:
                # Never include file contents (holds key bytes).
                log.warning(f"push providers file unreadable ({type(e).__name__}) — push disabled")

        self._apns, self._fcm = apns, fcm

    def apns(self) -> ApnsConfig | None:
        self._load_if_changed()
        return self._apns

    def fcm(self) -> FcmConfig | None:
        self._load_if_changed()
        return self._fcm

    def configured(self) -> bool:
        self._load_if_changed()
        return bool(self._apns or self._fcm)
