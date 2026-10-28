"""Custom orbs: named videos/images shown inside the orb instead of the theme's.

The orb's in-eye media normally comes from the active theme's `state_videos`
(one clip per PAL state). A custom orb is the same idea with the user's own
media, created from a satellite: a required `idle` media plus optional
`listening` / `processing` / `speaking` ones (the display falls back
processing → listening → idle and speaking → idle). The selection is global
(hub + every satellite, also exposed as an HA select); a satellite can opt
out locally. "default" is reserved and means "the theme's own orb".

Every upload is normalised with ffmpeg so the hub's Chromium AND iOS can play
it (phones record HEVC/.mov at 4K): videos → centre-cropped 480×480 H.264,
no audio, ≤ 20 s; images → centre-cropped 720×720 JPEG.

Layout: runtime/orbs/index.json + runtime/orbs/<id>/<state>.<mp4|jpg>.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time

log = logging.getLogger("hal.orbs")

DEFAULT_ID = "default"
STATES = ("idle", "listening", "processing", "speaking")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ORBS = 50
VIDEO_PX = 480
IMAGE_PX = 720
MAX_VIDEO_S = 20
INGEST_TIMEOUT_S = 180
_ID_RE = re.compile(r"^[a-z0-9-]{1,48}$")
_FILE_RE = re.compile(r"^(idle|listening|processing|speaking)\.(mp4|jpg)$")


class OrbError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _root() -> str:
    return os.environ.get("ORBS_DIR", "/app/runtime/orbs")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32]
    return s or "orb"


def clean_name(name) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not name:
        raise OrbError("a name is required")
    if name.lower() == DEFAULT_ID:
        raise OrbError('"default" is reserved for the theme\'s own orb')
    if name.startswith("("):
        raise OrbError("names can't start with '('")
    return name


def _kind_for(content_type: str, head: bytes) -> str | None:
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("image/"):
        return "image"
    # Sniff when the picker didn't set a type.
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[8:12] in (b"WEBP", b"heic", b"heix"):
        return "image"
    if head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3":
        return "video"
    return None


async def _ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=INGEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise OrbError("processing the media took too long", 422)
    if proc.returncode != 0:
        log.warning(f"orb ingest ffmpeg failed: {err.decode(errors='replace')[-400:]}")
        raise OrbError("couldn't read that file as a video or image", 422)


async def transcode(data: bytes, content_type: str, out_dir: str, state: str) -> dict:
    """Normalise one upload into out_dir/<state>.<mp4|jpg>. Returns
    {"file", "kind"}. Raises OrbError for unreadable or oversized media."""
    if not data:
        raise OrbError("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise OrbError("file too large (max 100 MB)", 413)
    kind = _kind_for(content_type, data[:16])
    if kind is None:
        raise OrbError("upload a video or an image", 415)
    square = "crop='min(iw,ih)':'min(iw,ih)'"
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        with open(inp, "wb") as f:
            f.write(data)
        if kind == "video":
            out = os.path.join(d, "out.mp4")
            await _ffmpeg([
                "-i", inp, "-t", str(MAX_VIDEO_S), "-an",
                "-vf", f"{square},scale={VIDEO_PX}:{VIDEO_PX}:flags=lanczos,fps=30,format=yuv420p",
                "-c:v", "libx264", "-profile:v", "main", "-preset", "veryfast", "-crf", "24",
                "-movflags", "+faststart", out,
            ])
            name = f"{state}.mp4"
        else:
            out = os.path.join(d, "out.jpg")
            await _ffmpeg([
                "-i", inp, "-frames:v", "1",
                "-vf", f"{square},scale={IMAGE_PX}:{IMAGE_PX}:flags=lanczos",
                "-q:v", "3", out,
            ])
            name = f"{state}.jpg"
        if not os.path.getsize(out):
            raise OrbError("couldn't read that file as a video or image", 422)
        os.makedirs(out_dir, exist_ok=True)
        # Drop a previous file for this state (it may have the other extension).
        for ext in ("mp4", "jpg"):
            old = os.path.join(out_dir, f"{state}.{ext}")
            if os.path.exists(old):
                os.unlink(old)
        shutil.move(out, os.path.join(out_dir, name))
    return {"file": name, "kind": kind}


class OrbStore:
    def __init__(self, root: str | None = None):
        self.root = root or _root()
        self._index: dict[str, dict] = {}
        self._load()

    # --- persistence ---------------------------------------------------------
    @property
    def _index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def _load(self) -> None:
        try:
            with open(self._index_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._index = {k: v for k, v in data.items() if _ID_RE.match(k) and isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"orbs: could not load index: {e}")

    def _save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".index.", dir=self.root)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._index, f, indent=1)
            os.replace(tmp, self._index_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- queries -------------------------------------------------------------
    def get(self, orb_id: str) -> dict | None:
        return self._index.get(orb_id)

    def list(self) -> list[dict]:
        return sorted(({"id": k, **v} for k, v in self._index.items()),
                      key=lambda o: o.get("created_at", 0))

    def names(self) -> list[str]:
        """Selectable names, "default" first (the HA select's options)."""
        return [DEFAULT_ID] + [o["name"] for o in self.list() if o.get("media", {}).get("idle")]

    def id_for_name(self, name: str) -> str | None:
        n = (name or "").strip().lower()
        if n == DEFAULT_ID:
            return DEFAULT_ID
        for k, v in self._index.items():
            if v.get("name", "").lower() == n:
                return k
        return None

    def media_path(self, orb_id: str, filename: str) -> str | None:
        """Filesystem path of a stored media file, or None (validated names only)."""
        if not _ID_RE.match(orb_id or "") or not _FILE_RE.match(filename or ""):
            return None
        orb = self._index.get(orb_id)
        if not orb or filename not in {m.get("file") for m in orb.get("media", {}).values()}:
            return None
        path = os.path.join(self.root, orb_id, filename)
        return path if os.path.isfile(path) else None

    # --- mutations -----------------------------------------------------------
    def _check_unique(self, name: str, exclude: str | None = None) -> None:
        for k, v in self._index.items():
            if k != exclude and v.get("name", "").lower() == name.lower():
                raise OrbError(f'an orb called "{name}" already exists', 409)

    def create(self, name: str) -> dict:
        name = clean_name(name)
        self._check_unique(name)
        if len(self._index) >= MAX_ORBS:
            raise OrbError(f"at most {MAX_ORBS} orbs", 409)
        orb_id = f"{_slug(name)}-{secrets.token_hex(3)}"
        self._index[orb_id] = {"name": name, "media": {}, "created_at": time.time()}
        self._save()
        return {"id": orb_id, **self._index[orb_id]}

    def rename(self, orb_id: str, name: str) -> dict:
        orb = self._require(orb_id)
        name = clean_name(name)
        self._check_unique(name, exclude=orb_id)
        orb["name"] = name
        self._save()
        return {"id": orb_id, **orb}

    async def set_media(self, orb_id: str, state: str, data: bytes, content_type: str) -> dict:
        orb = self._require(orb_id)
        if state not in STATES:
            raise OrbError(f"state must be one of {', '.join(STATES)}")
        entry = await transcode(data, content_type, os.path.join(self.root, orb_id), state)
        orb.setdefault("media", {})[state] = entry
        orb["updated_at"] = time.time()
        self._save()
        return {"id": orb_id, **orb}

    def remove_media(self, orb_id: str, state: str) -> dict:
        orb = self._require(orb_id)
        if state == "idle":
            raise OrbError("the idle media is required — replace it instead")
        entry = orb.get("media", {}).pop(state, None)
        if entry:
            try:
                os.unlink(os.path.join(self.root, orb_id, entry["file"]))
            except OSError:
                pass
            self._save()
        return {"id": orb_id, **orb}

    def delete(self, orb_id: str) -> bool:
        if self._index.pop(orb_id, None) is None:
            return False
        shutil.rmtree(os.path.join(self.root, orb_id), ignore_errors=True)
        self._save()
        return True

    def _require(self, orb_id: str) -> dict:
        orb = self._index.get(orb_id)
        if orb is None:
            raise OrbError("no such orb", 404)
        return orb


def orb_message(store: OrbStore | None, orb_id: str) -> dict:
    """The set_orb display message for a selection. Media URLs are relative
    (/orbs/<id>/<file>): the hub resolves them through its RPi proxy, satellites
    prefix their server base (+ ?token= through the gateway)."""
    orb = store.get(orb_id) if (store and orb_id != DEFAULT_ID) else None
    if not orb or not orb.get("media", {}).get("idle"):
        return {"type": "set_orb", "id": DEFAULT_ID, "name": DEFAULT_ID, "media": {}}
    version = int(orb.get("updated_at") or orb.get("created_at") or 0)
    return {
        "type": "set_orb",
        "id": orb_id,
        "name": orb["name"],
        "media": {
            st: {"url": f"/orbs/{orb_id}/{m['file']}?v={version}", "kind": m["kind"]}
            for st, m in orb["media"].items() if st in STATES
        },
    }
