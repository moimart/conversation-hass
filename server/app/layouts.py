"""Per-device satellite display layouts (HA cards + orb/text placement).

A paired phone/tablet can place Lovelace cards on its display and move the
orb, transcript and response, with separate portrait and landscape layouts.
The layout is stored HERE (not on the device) so it survives reinstalls. It is
keyed by the device's public id (sha256(token)[:16], see pairing.public_id) so
the file never holds a secret token.

Shape:
  {"cards": [{"id": "c1", "config": {...lovelace card...}}],
   "portrait":  {"items": [{"id": "orb", "x": 0, "y": 0, "w": 6, "h": 6}, ...]},
   "landscape": {"items": [...]},
   "version": 3}
Item ids are "orb", "clock", "transcript", "response" or a card id. Units are
grid cells (portrait COLS["portrait"] wide, landscape COLS["landscape"]); `res`
is the grid resolution the coordinates were saved at (see GRID_RES).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile

log = logging.getLogger("hal.layouts")

# Grid resolution 2 (half-size cells vs the first release's 6/12 columns).
# Layouts saved at res 1 are scaled ×2 by the app when it loads them.
GRID_RES = 2
COLS = {"portrait": 12, "landscape": 24}
MAX_ROWS = 120
MAX_CARDS = 40
MAX_CARD_BYTES = 32 * 1024
BUILTIN_IDS = ("orb", "clock", "transcript", "response")
_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")


class LayoutError(ValueError):
    pass


def _path() -> str:
    return os.environ.get("SATELLITE_LAYOUTS_FILE", "/app/runtime/satellite_layouts.json")


def _clamp(v, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = lo
    return max(lo, min(hi, n))


def validate(raw) -> dict:
    """Normalise a client-submitted layout, raising LayoutError on anything that
    can't be repaired by clamping (bad ids, oversized card configs, …)."""
    if not isinstance(raw, dict):
        raise LayoutError("layout must be an object")
    cards_in = raw.get("cards") or []
    if not isinstance(cards_in, list) or len(cards_in) > MAX_CARDS:
        raise LayoutError(f"cards must be a list of at most {MAX_CARDS}")
    cards, card_ids = [], set()
    for c in cards_in:
        if not isinstance(c, dict):
            raise LayoutError("card must be an object")
        cid = str(c.get("id") or "")
        if not _ID_RE.match(cid) or cid in BUILTIN_IDS or cid in card_ids:
            raise LayoutError(f"bad card id: {cid!r}")
        cfg = c.get("config")
        if not isinstance(cfg, dict) or not isinstance(cfg.get("type"), str):
            raise LayoutError(f"card {cid}: config needs a type")
        if len(json.dumps(cfg)) > MAX_CARD_BYTES:
            raise LayoutError(f"card {cid}: config too large")
        card_ids.add(cid)
        cards.append({"id": cid, "config": cfg})

    try:
        res = int(raw.get("res") or 1)
    except (TypeError, ValueError):
        res = 1
    out: dict = {"cards": cards, "res": max(1, min(GRID_RES, res))}
    for orient, cols in COLS.items():
        items_in = ((raw.get(orient) or {}).get("items")) or []
        if not isinstance(items_in, list):
            raise LayoutError(f"{orient}.items must be a list")
        items, seen = [], set()
        for it in items_in:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            if iid in seen or (iid not in BUILTIN_IDS and iid not in card_ids):
                continue  # unknown/duplicate placement: drop rather than fail
            seen.add(iid)
            w = _clamp(it.get("w"), 1, cols)
            h = _clamp(it.get("h"), 1, MAX_ROWS)
            items.append({
                "id": iid,
                "x": _clamp(it.get("x"), 0, cols - w),
                "y": _clamp(it.get("y"), 0, MAX_ROWS - h),
                "w": w, "h": h,
            })
        out[orient] = {"items": items}
    return out


class LayoutStore:
    def __init__(self, path: str | None = None):
        self._path = path or _path()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"layouts: could not load {self._path}: {e}")

    def _save(self) -> None:
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".satellite_layouts.", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, public_id: str) -> dict | None:
        return self._data.get(public_id)

    def put(self, public_id: str, raw) -> dict:
        layout = validate(raw)
        prev = self._data.get(public_id) or {}
        layout["version"] = int(prev.get("version", 0)) + 1
        self._data[public_id] = layout
        self._save()
        return layout

    def delete(self, public_id: str) -> bool:
        if self._data.pop(public_id, None) is None:
            return False
        self._save()
        return True

    def prune(self, keep_public_ids) -> int:
        """Drop layouts whose device is no longer paired."""
        keep = set(keep_public_ids)
        gone = [k for k in self._data if k not in keep]
        for k in gone:
            del self._data[k]
        if gone:
            self._save()
        return len(gone)
