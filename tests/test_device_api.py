"""Native-satellite helpers + routes (PAL Round firmware): orb clips as .palm,
HA dashboards as device pages, theme colours, POSIX TZ, TTS resampling."""
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from server.app import device_api as d
from server.app.device_icons import MDI

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


# --- time zone / theme -----------------------------------------------------------

def test_posix_tz_reads_tzif_footer(tmp_path):
    (tmp_path / "Europe").mkdir()
    (tmp_path / "Europe" / "Berlin").write_bytes(b"TZif2" + b"\0" * 40 + b"\nCET-1CEST,M3.5.0,M10.5.0/3\n")
    assert d.posix_tz("Europe/Berlin", str(tmp_path)) == "CET-1CEST,M3.5.0,M10.5.0/3"


def test_posix_tz_fallbacks(tmp_path):
    assert d.posix_tz("Europe/Madrid", str(tmp_path)) == "CET-1CEST,M3.5.0,M10.5.0/3"
    assert d.posix_tz("Mars/Olympus", str(tmp_path)) == "UTC0"
    assert d.posix_tz("../../etc/passwd", str(tmp_path)) == "UTC0"
    assert d.posix_tz("", str(tmp_path)) == "UTC0"


def test_theme_colors():
    css = "body.theme-x { --bg: #0a0a0c; --text:#E8E8EC; --text-dim: #6b6b78; --accent: #f80; --accent-rgb: 1,2,3; }"
    assert d.theme_colors(css) == {"bg": "#0a0a0c", "text": "#e8e8ec", "text_dim": "#6b6b78", "accent": "#ff8800"}
    assert d.theme_colors("") == {}


# --- .palm -------------------------------------------------------------------------

def test_orb_params_clamp():
    assert d.orb_params("288", "12") == (288, 12)
    assert d.orb_params(300, 99) == (288, 20)       # multiple of 16, fps cap
    assert d.orb_params(5000, 1) == (448, 4)
    assert d.orb_params(448, 12) == (448, 12)      # PAL Round: the orb fills ~96% of the screen
    assert d.orb_params("x", None) == (288, 12)


def test_palm_roundtrip():
    frames = [b"\xff\xd8a\xff\xd9", b"\xff\xd8bb\xff\xd9", b"\xff\xd8ccc\xff\xd9"]
    blob = d.pack_palm(frames, 288, 12)
    assert blob[:4] == b"PALM"
    meta, out = d.unpack_palm(blob)
    assert meta == {"version": 1, "fps": 12, "width": 288, "height": 288, "count": 3}
    assert out == frames
    # header (16) + 4 offsets + data
    assert len(blob) == 16 + 4 * 4 + sum(map(len, frames))
    with pytest.raises(ValueError):
        d.pack_palm([], 288, 12)


def test_encode_frame_is_square_masked_baseline_jpeg():
    from PIL import Image
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    jpg = d.encode_frame(img, 96)
    out = Image.open(io.BytesIO(jpg))
    assert out.format == "JPEG" and out.size == (96, 96)
    assert "progressive" not in out.info and "progression" not in out.info
    px = out.convert("RGB")
    assert sum(px.getpixel((1, 1))) < 30            # corner masked to black
    assert sum(px.getpixel((48, 48))) > 700         # centre keeps the image


@pytest.mark.asyncio
async def test_render_palm_image(tmp_path):
    from PIL import Image
    p = tmp_path / "o.jpg"
    Image.new("RGB", (720, 720), (200, 50, 50)).save(p)
    meta, frames = d.unpack_palm(await d.render_palm(str(p), "image", 128, 12))
    assert meta["count"] == 1 and meta["width"] == 128


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_render_palm_video_samples_fps_and_caps_length(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x320:rate=24:duration=3",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    meta, frames = d.unpack_palm(await d.render_palm(str(src), "video", 96, 10, max_s=2))
    assert meta["fps"] == 10 and meta["width"] == 96
    assert 18 <= meta["count"] <= 21                 # ~2 s at 10 fps
    assert all(f[:2] == b"\xff\xd8" for f in frames)


def test_resolve_orb_sources_fallbacks(tmp_path):
    theme_dir = tmp_path / "t"
    theme_dir.mkdir()
    for f in ("idle.mp4", "listen.mp4"):
        (theme_dir / f).write_bytes(b"x")
    theme = SimpleNamespace(dir_path=str(theme_dir),
                            state_videos=(("idle", "idle.mp4"), ("listening", "listen.mp4")))
    src = d.resolve_orb_sources(theme, None, "")
    assert src["idle"][0].endswith("idle.mp4")
    assert src["processing"][0].endswith("listen.mp4")      # processing → listening
    assert src["speaking"][0].endswith("idle.mp4")          # speaking → idle
    # a custom orb wins over the theme
    root = tmp_path / "orbs"
    (root / "dad-1").mkdir(parents=True)
    (root / "dad-1" / "idle.jpg").write_bytes(b"x")
    orb = {"id": "dad-1", "media": {"idle": {"file": "idle.jpg", "kind": "image"}}}
    src = d.resolve_orb_sources(theme, orb, str(root))
    assert set(src) == set(d.STATES) and all(v == (str(root / "dad-1" / "idle.jpg"), "image") for v in src.values())
    # no media at all → native orb
    assert d.resolve_orb_sources(SimpleNamespace(dir_path="", state_videos=()), None, "") == {}
    assert d.resolve_orb_sources(None, None, "") == {}


# --- pages ---------------------------------------------------------------------------

def test_lovelace_to_pages():
    cfg = {"views": [
        {"title": "Home",
         "cards": [
             {"type": "tile", "entity": "light.kitchen", "name": "Küche"},
             {"type": "entities", "entities": ["switch.fan", {"entity": "sensor.temp", "name": "Temp"},
                                               {"type": "divider"}, "switch.fan"]},
             {"type": "thermostat", "entity": "climate.living"},
             {"type": "vertical-stack", "cards": [{"type": "button", "entity": "scene.movie", "icon": "mdi:movie-open"},
                                                  {"type": "markdown", "content": "hi"}]},
             {"type": "custom:button-card", "entity": "sensor.next_garbage",
              "tap_action": {"action": "call-service", "target": {"entity_id": "light.ignored"}}},
         ],
         "badges": ["person.anna"]},
        {"path": "media", "sections": [{"cards": [{"type": "heading", "heading": "x"},
                                                  {"type": "media-control", "entity": "media_player.tv"}]}]},
    ]}
    states = {"sensor.temp": {"attributes": {"device_class": "temperature"}}}
    pages = d.lovelace_to_pages(cfg, states, {"switch.fan": "mdi:fan"})
    titles = [(p["title"], [w["e"] for w in p["widgets"]]) for p in pages]
    assert titles == [
        ("Home", ["climate.living"]),
        ("Home", ["light.kitchen", "switch.fan", "sensor.temp", "scene.movie"]),
        ("Home", ["sensor.next_garbage", "person.anna"]),
        ("media", ["media_player.tv"]),
    ]
    w = {w["e"]: w for p in pages for w in p["widgets"]}
    assert w["light.kitchen"] == {"e": "light.kitchen", "k": "light", "i": MDI["lightbulb"], "n": "Küche"}
    assert w["switch.fan"]["i"] == MDI["fan"]                 # registry icon
    assert w["sensor.temp"]["i"] == MDI["thermometer"]        # device_class default
    assert w["scene.movie"]["i"] == MDI["movie-open"] and w["scene.movie"]["k"] == "action"
    assert w["climate.living"]["k"] == "climate" and w["media_player.tv"]["k"] == "media"
    assert "light.ignored" not in w                           # actions aren't entities shown


def test_pages_unknown_icon_and_limits():
    cfg = {"views": [{"cards": [{"type": "tile", "entity": "light.x", "icon": "mdi:not-a-real-icon"}]
                      + [{"type": "tile", "entity": f"switch.s{i}"} for i in range(100)]}]}
    pages = d.lovelace_to_pages(cfg)
    assert pages[0]["widgets"][0]["i"] == MDI["lightbulb"]
    assert len(pages) <= d.MAX_PAGES
    assert sum(len(p["widgets"]) for p in pages) <= d.MAX_WIDGETS
    assert d.lovelace_to_pages({}) == [] and d.lovelace_to_pages({"views": ["junk"]}) == []


# --- TTS resample ---------------------------------------------------------------------

def _wav(rate, ch=1, seconds=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    pcm = (np.sin(2 * np.pi * 440 * t) * 10000).astype("<i2")
    if ch == 2:
        pcm = np.repeat(pcm, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_resample_wav():
    out = d.resample_wav(_wav(22050, ch=2), 16000)
    with wave.open(io.BytesIO(out)) as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)
        assert abs(wf.getnframes() - 8000) <= 1
    same = _wav(16000)
    assert d.resample_wav(same, 16000) is same
    assert d.resample_wav(b"not a wav", 16000) == b"not a wav"


# --- routes ---------------------------------------------------------------------------

def _req(token=None, query=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(app=SimpleNamespace(), headers=headers, query_params=dict(query or {}),
                           url=SimpleNamespace(path="/api/device"))


class _Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.states = {}

    async def send(self, msg):
        assert msg["type"] == "lovelace/config"
        return self.cfg

    async def bootstrap(self):
        return {"config": {"time_zone": "Europe/Berlin"},
                "registry": {"entities": [{"ei": "switch.fan", "ic": "mdi:fan"}]}}


@pytest.fixture
def dev_state(monkeypatch, tmp_path):
    theme_dir = tmp_path / "themes" / "sunset"
    theme_dir.mkdir(parents=True)
    (theme_dir / "theme.css").write_text("--bg:#2a1a2a;--accent:#ff8050;")
    from PIL import Image
    Image.new("RGB", (64, 64), (10, 200, 10)).save(theme_dir / "idle.jpg")
    theme = SimpleNamespace(dir_path=str(theme_dir), kind="dark", state_videos=(("idle", "idle.jpg"),))
    st = SimpleNamespace(
        pairing=SimpleNamespace(
            is_valid_token=lambda t: t in ("full", "watch"),
            scope_allows=lambda t, p: t == "full",
            device_name=lambda t: "Round",
        ),
        themes=SimpleNamespace(get=lambda n: theme if n == "sunset" else None),
        current_theme="sunset", current_orb="default", orbs=None,
        ha_bridge=_Bridge({"views": [{"title": "Home", "cards": [{"type": "tile", "entity": "switch.fan"}]}]}),
    )
    monkeypatch.setitem(sys.modules, "server.app.main", SimpleNamespace(_get_state=lambda app: st))
    monkeypatch.setenv("DEVICE_ORB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DEVICE_FIRMWARE_DIR", str(tmp_path / "fw"))
    return st


def _body(resp):
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_device_routes_auth(dev_state):
    from server.app import routes_device as r
    for fn in (r.device_info, r.device_orb, r.device_pages):
        assert (await fn(_req())).status_code == 401
        assert (await fn(_req("bogus"))).status_code == 401
        assert (await fn(_req("watch"))).status_code == 403
    assert (await r.device_orb_clip(_req("watch"), "0" * 20)).status_code == 403
    assert (await r.device_firmware(_req("watch"), "pal-round")).status_code == 403


@pytest.mark.asyncio
async def test_device_info(dev_state, tmp_path):
    from server.app import routes_device as r
    info = await r.device_info(_req("full"))
    assert info["tz"] == "Europe/Berlin" and info["tz_posix"].startswith("CET-1CEST")
    assert info["theme"] == {"name": "sunset", "kind": "dark", "colors": {"bg": "#2a1a2a", "accent": "#ff8050"}}
    assert info["firmware"] is None and info["device_name"] == "Round"
    fw = tmp_path / "fw" / "pal-round"
    fw.mkdir(parents=True)
    (fw / "firmware.bin").write_bytes(b"\xe9" * 100)
    (fw / "version").write_text("0.2.0\n")
    info = await r.device_info(_req("full"))
    assert info["firmware"]["version"] == "0.2.0" and info["firmware"]["size"] == 100
    resp = await r.device_firmware(_req("full"), "pal-round")
    assert resp.path.endswith("firmware.bin")
    assert (await r.device_firmware(_req("full"), "../etc")).status_code == 404


@pytest.mark.asyncio
async def test_device_orb_manifest_and_clip(dev_state):
    from server.app import routes_device as r
    m = await r.device_orb(_req("full", {"size": "96", "fps": "8"}))
    assert m["id"] == "default" and m["size"] == 96 and set(m["states"]) == set(d.STATES)
    key = m["states"]["idle"]["key"]
    assert all(s["key"] == key for s in m["states"].values())   # one source → one render
    resp = await r.device_orb_clip(_req("full"), key)
    meta, _ = d.unpack_palm(open(resp.path, "rb").read())
    assert meta["count"] == 1 and meta["width"] == 96
    assert (await r.device_orb_clip(_req("full"), "../../x")).status_code == 404
    assert (await r.device_orb_clip(_req("full"), "f" * 20)).status_code == 404
    # a theme without media → empty states (native orb)
    dev_state.current_theme = "none"
    assert (await r.device_orb(_req("full")))["states"] == {}


@pytest.mark.asyncio
async def test_device_orb_custom(dev_state, tmp_path):
    from server.app import routes_device as r
    from PIL import Image
    root = tmp_path / "orbs"
    (root / "dad-1").mkdir(parents=True)
    Image.new("RGB", (100, 100), (1, 2, 3)).save(root / "dad-1" / "idle.jpg")
    dev_state.orbs = SimpleNamespace(
        root=str(root),
        get=lambda i: {"name": "Papá", "media": {"idle": {"file": "idle.jpg", "kind": "image"}}} if i == "dad-1" else None)
    dev_state.current_orb = "dad-1"
    m = await r.device_orb(_req("full", {"size": "96"}))
    assert (m["id"], m["name"]) == ("dad-1", "Papá")
    m = await r.device_orb(_req("full", {"orb": "default", "size": "96"}))   # device's own pick
    assert m["id"] == "default"


@pytest.mark.asyncio
async def test_device_pages(dev_state):
    from server.app import routes_device as r
    out = await r.device_pages(_req("full", {"dashboard": "pal-round"}))
    assert out["dashboard"] == "pal-round"
    assert out["pages"] == [{"title": "Home", "widgets": [{"e": "switch.fan", "k": "toggle", "i": MDI["fan"]}]}]
    dev_state.ha_bridge = None
    assert (await r.device_pages(_req("full"))).status_code == 503


@pytest.mark.asyncio
async def test_satellite_tts_rate(monkeypatch):
    import time
    from server.app.routes_http import get_satellite_tts
    st = SimpleNamespace(
        pairing=SimpleNamespace(is_valid_token=lambda t: t == "tok"),
        satellite_ws={"tok": object()},
        satellite_tts={"tok": {"audio": _wav(22050), "mime": "audio/wav", "ts": time.monotonic()}},
    )
    monkeypatch.setitem(sys.modules, "server.app.main", SimpleNamespace(_get_state=lambda app: st))
    plain = await get_satellite_tts(_req("tok"))
    assert plain.body == st.satellite_tts["tok"]["audio"]
    resampled = await get_satellite_tts(_req("tok", {"rate": "16000"}))
    with wave.open(io.BytesIO(resampled.body)) as wf:
        assert wf.getframerate() == 16000
    bogus = await get_satellite_tts(_req("tok", {"rate": "999999"}))
    assert bogus.body == plain.body


@pytest.mark.asyncio
async def test_ble_pair_reaches_online_rounds_only(monkeypatch):
    from server.app import routes_device as r

    class WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, m):
            self.sent.append(m)

    names = {"phone": "Pixel", "full": "Pixel", "r1": "PAL Round 0274", "tab": "Tablet"}
    round_ws, tab_ws, me_ws = WS(), WS(), WS()
    st = SimpleNamespace(
        pairing=SimpleNamespace(
            is_valid_token=lambda t: t in names or t == "watch",
            scope_allows=lambda t, p: t != "watch",
            device_name=lambda t: names.get(t),
        ),
        satellite_ws={"r1": round_ws, "tab": tab_ws, "full": me_ws},
    )
    monkeypatch.setitem(sys.modules, "server.app.main", SimpleNamespace(_get_state=lambda app: st))
    out = await r.ble_pair(_req("full"))
    assert out == {"ok": True, "devices": 1}
    assert round_ws.sent == [{"type": "ble_pair", "from": "Pixel"}]
    assert tab_ws.sent == [] and me_ws.sent == []
    assert (await r.ble_pair(_req("watch"))).status_code == 403
    assert (await r.ble_pair(_req("nope"))).status_code == 401
