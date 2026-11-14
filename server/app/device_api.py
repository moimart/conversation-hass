"""Helpers for native (microcontroller) satellites — PAL Round (ESP32-S3 +
466×466 round AMOLED, firmware in firmware/pal-round).

A browser satellite can play H.264 orb clips, run Lovelace cards and parse
theme CSS; an ESP32 can't. Everything here pre-digests those for the device:

- **orb clips** → a ``.palm`` file: a tiny container of baseline JPEG frames
  (centre-cropped square, circular black mask so the AMOLED's black renders
  the orb round), sized + frame-rate-capped for a software JPEG decoder;
- **HA dashboard** → swipeable *pages* of native widgets (one Lovelace view
  → one or more pages; tiles packed 4 to a page, big widgets get their own);
- **theme** → a handful of colours; **time zone** → a POSIX TZ string;
- **TTS** → resampled to the codec's rate (the device runs its I2S at 16 kHz).

Everything is pure/sync except the ffmpeg step, so it unit-tests without a
device. Routes live in routes_device.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import struct
import tempfile
import wave

from .device_icons import MDI

# --- time zone -------------------------------------------------------------------

_TZ_FALLBACK = {
    "Europe/Berlin": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Madrid": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Paris": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/London": "GMT0BST,M3.5.0/1,M10.5.0",
    "America/New_York": "EST5EDT,M3.2.0,M11.1.0",
    "America/Los_Angeles": "PST8PDT,M3.2.0,M11.1.0",
    "UTC": "UTC0",
}


def posix_tz(iana: str | None, zoneinfo_dir: str = "/usr/share/zoneinfo") -> str:
    """The POSIX TZ string for an IANA zone (what newlib's setenv("TZ") needs).
    TZif v2+ files end with it on their own line; fall back to a small table."""
    iana = (iana or "").strip()
    if not iana or ".." in iana or iana.startswith("/"):
        return "UTC0"
    try:
        with open(os.path.join(zoneinfo_dir, iana), "rb") as f:
            data = f.read()
        if data[:4] == b"TZif":
            footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii", "ignore").strip()
            if footer and re.match(r"^[A-Za-z<]", footer):
                return footer
    except OSError:
        pass
    return _TZ_FALLBACK.get(iana, "UTC0")


# --- theme -------------------------------------------------------------------------

_CSS_VARS = ("bg", "text", "text-dim", "accent", "accent-deep")


def theme_colors(css: str) -> dict:
    """The theme's main colours as #rrggbb (only the ones it declares)."""
    out = {}
    for name in _CSS_VARS:
        m = re.search(r"--%s\s*:\s*(#[0-9a-fA-F]{3,8})\b" % re.escape(name), css or "")
        if m:
            v = m.group(1)
            if len(v) == 4:  # #rgb
                v = "#" + "".join(c * 2 for c in v[1:])
            out[name.replace("-", "_")] = v[:7].lower()
    return out


# --- orb clips (.palm) ------------------------------------------------------------

STATES = ("idle", "listening", "processing", "speaking")
PALM_MAGIC = b"PALM"
PALM_HEADER = struct.Struct("<4sBBHHHHH")  # magic, ver, flags, fps, w, h, count, reserved


def orb_params(size, fps) -> tuple[int, int]:
    """Clamp the device's requested frame size (multiple of 16 for the
    decoder's MCUs) and frame rate."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 288
    try:
        fps = int(fps)
    except (TypeError, ValueError):
        fps = 12
    size = max(96, min(416, size)) // 16 * 16
    return size, max(4, min(20, fps))


def pack_palm(frames: list[bytes], size: int, fps: int) -> bytes:
    """Header, then count+1 little-endian u32 offsets (relative to the data
    start; the last one is the end), then the JPEG frames back to back."""
    if not frames:
        raise ValueError("no frames")
    head = PALM_HEADER.pack(PALM_MAGIC, 1, 0, fps, size, size, len(frames), 0)
    offs, pos = [], 0
    for fr in frames:
        offs.append(pos)
        pos += len(fr)
    offs.append(pos)
    return head + struct.pack(f"<{len(offs)}I", *offs) + b"".join(frames)


def unpack_palm(blob: bytes) -> tuple[dict, list[bytes]]:
    """Inverse of pack_palm (tests + debugging)."""
    magic, ver, _flags, fps, w, h, count, _ = PALM_HEADER.unpack_from(blob, 0)
    if magic != PALM_MAGIC:
        raise ValueError("not a palm file")
    base = PALM_HEADER.size + 4 * (count + 1)
    offs = struct.unpack_from(f"<{count + 1}I", blob, PALM_HEADER.size)
    frames = [blob[base + offs[i]:base + offs[i + 1]] for i in range(count)]
    return {"version": ver, "fps": fps, "width": w, "height": h, "count": count}, frames


def _circle_mask(size: int):
    """Anti-aliased circular mask (drawn 4× and downsampled)."""
    from PIL import Image, ImageDraw
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def encode_frame(img, size: int, quality: int = 80, mask=None) -> bytes:
    """Centre-crop to a square, scale to `size`, black outside the circle, and
    encode as a baseline 4:2:0 JPEG (what esp_new_jpeg decodes)."""
    from PIL import Image
    img = img.convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    img = img.resize((size, size), Image.LANCZOS)
    black = Image.new("RGB", (size, size), (0, 0, 0))
    img = Image.composite(img, black, mask or _circle_mask(size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=False, subsampling=2)
    return buf.getvalue()


def source_key(path: str, size: int, fps: int, max_s: float) -> str:
    """Cache key = source file identity + render parameters (the ETag)."""
    st = os.stat(path)
    raw = f"{os.path.realpath(path)}|{st.st_size}|{int(st.st_mtime)}|{size}|{fps}|{max_s}|v1"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


async def render_palm(src: str, kind: str, size: int, fps: int, max_s: float = 10.0,
                      timeout: float = 120.0) -> bytes:
    """Render a theme/orb media file into a .palm blob. Images become a single
    frame; videos are sampled at `fps` for at most `max_s` seconds."""
    from PIL import Image
    mask = _circle_mask(size)
    if kind == "image":
        with Image.open(src) as im:
            return pack_palm([encode_frame(im, size, mask=mask)], size, fps)
    with tempfile.TemporaryDirectory(prefix="palm-") as tmp:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-i", src, "-t", str(max_s), "-an",
            "-vf", f"fps={fps},scale={size * 2}:{size * 2}:force_original_aspect_ratio=increase",
            "-q:v", "3", os.path.join(tmp, "f%04d.jpg"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("ffmpeg timed out")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[-300:]}")
        names = sorted(n for n in os.listdir(tmp) if n.endswith(".jpg"))
        if not names:
            raise RuntimeError("no frames decoded")

        def encode_all():
            out = []
            for n in names:
                with Image.open(os.path.join(tmp, n)) as im:
                    out.append(encode_frame(im, size, mask=mask))
            return out
        frames = await asyncio.to_thread(encode_all)
    return pack_palm(frames, size, fps)


def resolve_orb_sources(theme, orb: dict | None, orb_root: str) -> dict:
    """state -> (path, kind) for what the orb shows, after the display's own
    fallbacks (processing → listening → idle, speaking → idle). A custom orb
    wins over the theme's state_videos; {} means "no media" (the device draws
    its native orb)."""
    media: dict[str, tuple[str, str]] = {}
    if orb and orb.get("media", {}).get("idle"):
        for st, m in orb["media"].items():
            p = os.path.join(orb_root, orb["id"], m.get("file", ""))
            if st in STATES and os.path.isfile(p):
                media[st] = (p, m.get("kind", "video"))
    elif theme is not None and getattr(theme, "state_videos", None):
        for st, fname in dict(theme.state_videos).items():
            p = os.path.join(theme.dir_path, fname)
            if st in STATES and os.path.isfile(p):
                kind = "image" if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else "video"
                media[st] = (p, kind)
    if "idle" not in media:
        return {}
    out = {"idle": media["idle"]}
    out["listening"] = media.get("listening", out["idle"])
    out["processing"] = media.get("processing", out["listening"])
    out["speaking"] = media.get("speaking", out["idle"])
    return out


# --- HA dashboard → device pages ---------------------------------------------------

_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
MAX_PAGES = 16
MAX_WIDGETS = 64
TILES_PER_PAGE = 4

# domain -> widget kind
_KIND = {
    "light": "light", "switch": "toggle", "input_boolean": "toggle", "fan": "toggle",
    "automation": "toggle", "siren": "toggle", "humidifier": "toggle", "group": "toggle",
    "cover": "cover", "lock": "lock", "climate": "climate", "water_heater": "climate",
    "media_player": "media", "weather": "weather",
    "scene": "action", "script": "action", "button": "action", "input_button": "action",
    "vacuum": "toggle",
}
BIG_KINDS = {"climate", "media", "weather"}

_DOMAIN_ICON = {
    "light": "lightbulb", "switch": "toggle-switch", "input_boolean": "toggle-switch",
    "fan": "fan", "automation": "robot", "cover": "window-shutter", "lock": "lock",
    "climate": "thermostat", "water_heater": "thermostat", "media_player": "speaker",
    "weather": "weather-partly-cloudy", "scene": "palette-outline", "script": "script-text",
    "button": "gesture-tap-button", "input_button": "gesture-tap-button",
    "sensor": "gauge", "binary_sensor": "eye-outline", "person": "account",
    "device_tracker": "map-marker", "sun": "white-balance-sunny", "vacuum": "robot-vacuum",
    "camera": "cctv", "calendar": "calendar", "timer": "timer-outline", "counter": "counter",
    "input_number": "numeric", "number": "numeric", "input_select": "format-list-bulleted",
    "select": "format-list-bulleted", "alarm_control_panel": "shield-home", "siren": "bell",
    "humidifier": "air-purifier", "group": "home", "zone": "map-marker",
}
_SENSOR_CLASS_ICON = {
    "temperature": "thermometer", "humidity": "water-percent", "power": "flash",
    "energy": "lightning-bolt", "battery": "battery", "carbon_dioxide": "molecule-co2",
    "illuminance": "brightness-6", "pressure": "gauge", "wind_speed": "weather-windy",
    "door": "door", "window": "window-open", "garage_door": "garage", "motion": "motion-sensor",
    "occupancy": "home-account", "presence": "home-account", "moisture": "water-alert",
    "smoke": "smoke-detector", "lock": "lock", "plug": "power-plug", "timestamp": "clock-outline",
    "date": "calendar", "monetary": "tag", "opening": "door",
}


def icon_codepoint(name: str | None) -> int | None:
    if not name:
        return None
    n = name.split(":", 1)[-1] if name.startswith(("mdi:", "hass:")) else None
    return MDI.get(n) if n else None


def default_icon(entity_id: str, attrs: dict | None = None) -> int:
    domain = entity_id.split(".", 1)[0]
    dc = (attrs or {}).get("device_class")
    if domain in ("sensor", "binary_sensor") and dc in _SENSOR_CLASS_ICON:
        return MDI[_SENSOR_CLASS_ICON[dc]]
    return MDI[_DOMAIN_ICON.get(domain, "circle-outline")]


def _widget(entity_id: str, name=None, icon=None, states=None, registry_icons=None) -> dict:
    domain = entity_id.split(".", 1)[0]
    st = (states or {}).get(entity_id) or {}
    attrs = st.get("attributes") or {}
    cp = (icon_codepoint(icon) or icon_codepoint((registry_icons or {}).get(entity_id))
          or icon_codepoint(attrs.get("icon")) or default_icon(entity_id, attrs))
    w = {"e": entity_id, "k": _KIND.get(domain, "sensor"), "i": cp}
    if name:
        w["n"] = str(name)[:40]
    return w


def _card_entities(card: dict) -> list[tuple[str, str | None, str | None]]:
    """(entity, name, icon) triples a card shows, in order."""
    out: list[tuple[str, str | None, str | None]] = []
    if not isinstance(card, dict):
        return out
    t = str(card.get("type") or "")
    if t in ("vertical-stack", "horizontal-stack", "grid") or isinstance(card.get("cards"), list) and not t.startswith("custom:"):
        for c in card.get("cards") or []:
            out.extend(_card_entities(c))
        return out
    if t in ("heading", "markdown", "iframe", "map", "conditional", "spacer", "picture"):
        return out
    if t in ("entities", "glance") or isinstance(card.get("entities"), list):
        for row in card.get("entities") or []:
            if isinstance(row, str):
                out.append((row, None, None))
            elif isinstance(row, dict) and isinstance(row.get("entity"), str):
                out.append((row["entity"], row.get("name"), row.get("icon")))
        if t in ("entities", "glance"):
            return out
    if isinstance(card.get("entity"), str):
        out.append((card["entity"], card.get("name") if isinstance(card.get("name"), str) else None,
                    card.get("icon") if isinstance(card.get("icon"), str) else None))
    if t.startswith("custom:") and not out:
        def walk(v):
            if isinstance(v, str) and _ENTITY_RE.match(v):
                out.append((v, None, None))
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, dict):
                for k, x in v.items():
                    if k not in ("tap_action", "hold_action", "double_tap_action", "triggers_update"):
                        walk(x)
        walk(card)
    return out


def lovelace_to_pages(cfg: dict, states: dict | None = None,
                      registry_icons: dict | None = None) -> list[dict]:
    """Convert a Lovelace dashboard config to device pages.

    Each view contributes its widgets in order (cards, sections, badges);
    small widgets are packed TILES_PER_PAGE to a page, climate / media /
    weather get a page each. Duplicates within a view are dropped."""
    pages: list[dict] = []
    total = 0
    for vi, view in enumerate((cfg or {}).get("views") or []):
        if not isinstance(view, dict):
            continue
        title = str(view.get("title") or view.get("path") or f"Page {vi + 1}")[:32]
        cards = list(view.get("cards") or [])
        for s in view.get("sections") or []:
            cards.extend((s or {}).get("cards") or [])
        triples = []
        for c in cards:
            triples.extend(_card_entities(c))
        for b in view.get("badges") or []:
            e = b if isinstance(b, str) else (b or {}).get("entity")
            if isinstance(e, str):
                triples.append((e, None, None))
        seen, small = set(), []
        for eid, name, icon in triples:
            if eid in seen or not _ENTITY_RE.match(eid or ""):
                continue
            seen.add(eid)
            if total >= MAX_WIDGETS:
                break
            w = _widget(eid, name, icon, states, registry_icons)
            total += 1
            if w["k"] in BIG_KINDS:
                pages.append({"title": title, "widgets": [w]})
            else:
                small.append(w)
        for i in range(0, len(small), TILES_PER_PAGE):
            pages.append({"title": title, "widgets": small[i:i + TILES_PER_PAGE]})
    return pages[:MAX_PAGES]


# --- TTS ------------------------------------------------------------------------------

def resample_wav(data: bytes, rate: int) -> bytes:
    """Return `data` (a PCM WAV) as 16-bit mono at `rate` Hz. Unchanged if it
    already is; unreadable input is returned as-is."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            ch, sw, sr, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(n)
    except (wave.Error, EOFError):
        return data
    if sw != 2:
        return data
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if sr == rate and ch == 1:
        return data
    if sr != rate and pcm.size:
        n_out = int(round(pcm.size * rate / sr))
        x_old = np.arange(pcm.size, dtype=np.float64) / sr
        x_new = np.arange(n_out, dtype=np.float64) / rate
        pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.clip(pcm, -32768, 32767).astype("<i2").tobytes())
    return out.getvalue()
