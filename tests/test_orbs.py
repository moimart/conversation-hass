"""Custom orbs: the store (names, reserved "default", persistence), media
ingest through real ffmpeg (video → 480² H.264 mp4, image → 720² jpg),
the set_orb display message, route scope gating, and global selection."""
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from server.app import routes_orbs
from server.app.orbs import DEFAULT_ID, OrbError, OrbStore, orb_message

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_name,width,height,codec_type", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


@pytest.fixture
def clip(tmp_path):
    """A 2 s 640x360 test video WITH audio (ingest must drop it)."""
    p = tmp_path / "clip.mov"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine", "-t", "2", "-shortest", str(p)], check=True)
    return p.read_bytes()


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "pic.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1000x600",
                    "-frames:v", "1", str(p)], check=True)
    return p.read_bytes()


def test_names_and_reserved_default(tmp_path):
    s = OrbStore(str(tmp_path))
    with pytest.raises(OrbError):
        s.create("Default")
    with pytest.raises(OrbError):
        s.create("   ")
    a = s.create("  Cosmic   Cat ")
    assert a["name"] == "Cosmic Cat" and a["id"].startswith("cosmic-cat-")
    with pytest.raises(OrbError) as e:
        s.create("cosmic cat")                       # case-insensitive unique
    assert e.value.status == 409
    assert s.names() == [DEFAULT_ID]                 # not selectable until it has idle media
    assert s.id_for_name("COSMIC CAT") == a["id"] and s.id_for_name("default") == DEFAULT_ID
    assert OrbStore(str(tmp_path)).get(a["id"])["name"] == "Cosmic Cat"   # persisted


@needs_ffmpeg
@pytest.mark.asyncio
async def test_video_and_image_ingest(tmp_path, clip, png):
    s = OrbStore(str(tmp_path))
    orb = s.create("Test")
    await s.set_media(orb["id"], "idle", clip, "video/quicktime")
    v = _probe(tmp_path / orb["id"] / "idle.mp4")
    assert [x["codec_type"] for x in v] == ["video"]            # audio dropped
    assert (v[0]["codec_name"], v[0]["width"], v[0]["height"]) == ("h264", 480, 480)
    await s.set_media(orb["id"], "speaking", png, "")           # sniffed as image
    i = _probe(tmp_path / orb["id"] / "speaking.jpg")
    assert (i[0]["width"], i[0]["height"]) == (720, 720)
    # Replacing idle with an image swaps the file (no stale .mp4 left).
    await s.set_media(orb["id"], "idle", png, "image/png")
    assert not (tmp_path / orb["id"] / "idle.mp4").exists()
    assert s.names() == [DEFAULT_ID, "Test"]
    msg = orb_message(s, orb["id"])
    assert msg["id"] == orb["id"] and set(msg["media"]) == {"idle", "speaking"}
    assert msg["media"]["idle"]["kind"] == "image"
    assert msg["media"]["idle"]["url"].startswith(f"/orbs/{orb['id']}/idle.jpg?v=")
    assert s.media_path(orb["id"], "idle.jpg") and not s.media_path(orb["id"], "../index.json")
    with pytest.raises(OrbError):
        s.remove_media(orb["id"], "idle")
    s.remove_media(orb["id"], "speaking")
    assert not (tmp_path / orb["id"] / "speaking.jpg").exists()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_rejects_garbage_and_unknown_state(tmp_path):
    s = OrbStore(str(tmp_path))
    orb = s.create("X")
    with pytest.raises(OrbError) as e:
        await s.set_media(orb["id"], "idle", b"definitely not media", "application/pdf")
    assert e.value.status == 415
    with pytest.raises(OrbError) as e:
        await s.set_media(orb["id"], "idle", b"\x00" * 64, "video/mp4")   # claims video, isn't
    assert e.value.status == 422
    with pytest.raises(OrbError):
        await s.set_media(orb["id"], "dancing", b"x", "video/mp4")


def test_message_falls_back_to_default(tmp_path):
    s = OrbStore(str(tmp_path))
    orb = s.create("Empty")
    assert orb_message(s, orb["id"])["id"] == DEFAULT_ID       # no idle media yet
    assert orb_message(s, "gone-123")["id"] == DEFAULT_ID
    assert orb_message(None, "anything") == {"type": "set_orb", "id": DEFAULT_ID,
                                             "name": DEFAULT_ID, "media": {}}


# --- routes --------------------------------------------------------------------

class Bridge:
    def __init__(self):
        self.published = []
        self.discovery = 0
        self.orb_options = []

    async def publish_config(self, key, value):
        self.published.append((key, value))

    async def publish_discovery(self):
        self.discovery += 1


@pytest.fixture
def st(monkeypatch, tmp_path):
    pushed = []
    state = SimpleNamespace(
        pairing=SimpleNamespace(is_valid_token=lambda t: t in ("full", "watch"),
                                scope_allows=lambda t, p: t == "full"),
        orbs=OrbStore(str(tmp_path)), current_orb=DEFAULT_ID,
        runtime_config=SimpleNamespace(set=lambda k, v: pushed.append(("cfg", k, v))),
        mqtt_bridge=Bridge(), pushed=pushed,
    )

    async def push_rpi(_s, msg):
        pushed.append(("rpi", msg))
        return True

    async def force(_s, msg, **_kw):
        pushed.append(("all", msg))

    monkeypatch.setitem(sys.modules, "server.app.main", SimpleNamespace(
        _get_state=lambda app: state, _push_to_rpi=push_rpi, broadcast_force_action=force))
    return state


def _req(token=None, body=None, headers=None):
    h = {"authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()

    async def _json():
        return json.loads(raw)

    async def _body():
        return raw

    return SimpleNamespace(app=SimpleNamespace(), headers=h, json=_json, body=_body)


@pytest.mark.asyncio
async def test_routes_scope_gated(st):
    assert (await routes_orbs.list_orbs(_req())).status_code == 401
    assert (await routes_orbs.list_orbs(_req("watch"))).status_code == 403
    assert (await routes_orbs.create_orb(_req("watch", {"name": "x"}))).status_code == 403
    assert await routes_orbs.list_orbs(_req("full")) == {"orbs": [], "selected": DEFAULT_ID}


@needs_ffmpeg
@pytest.mark.asyncio
async def test_create_upload_select_delete_flow(st, png):
    orb = await routes_orbs.create_orb(_req("full", {"name": "Moon"}))
    assert orb["states"] == []
    up = await routes_orbs.upload_orb_media(_req("full", png, {"content-type": "image/png",
                                                                "content-length": str(len(png))}),
                                            orb["id"], "idle")
    assert up["states"] == ["idle"]
    assert st.mqtt_bridge.orb_options == [DEFAULT_ID, "Moon"]          # now selectable
    sel = await routes_orbs.select_orb(_req("full", {"id": orb["id"]}))
    assert sel == {"selected": orb["id"]} and st.current_orb == orb["id"]
    kinds = [p[0] for p in st.pushed]
    assert ("cfg", "orb", orb["id"]) in st.pushed and "rpi" in kinds and "all" in kinds
    assert ("orb", "Moon") in st.mqtt_bridge.published
    # Deleting the selected orb falls back to the theme's orb everywhere.
    st.pushed.clear()
    assert await routes_orbs.delete_orb(_req("full"), orb["id"]) == {"ok": True}
    assert st.current_orb == DEFAULT_ID
    assert st.pushed[-1][1]["id"] == DEFAULT_ID
    assert (await routes_orbs.select_orb(_req("full", {"id": "nope"}))).status_code == 404


@pytest.mark.asyncio
async def test_upload_size_cap(st):
    orb = await routes_orbs.create_orb(_req("full", {"name": "Big"}))
    r = await routes_orbs.upload_orb_media(
        _req("full", b"x", {"content-length": str(200 * 1024 * 1024)}), orb["id"], "idle")
    assert r.status_code == 413


# --- admin import via PAL's /mcp (create_orb & co.) --------------------------------

def _mcp_tool(state, name):
    from server.app.mcp_server import build_mcp_server
    return build_mcp_server(state)._tool_manager._tools[name].fn


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    monkeypatch.setenv("ORBS_INBOX", str(d))
    return d


@needs_ffmpeg
@pytest.mark.asyncio
async def test_mcp_create_orb_from_inbox_and_url(st, inbox, png, clip, monkeypatch):
    import httpx
    st.local_tools = None
    (inbox / "moon.png").write_bytes(png)
    real_client = httpx.AsyncClient

    def handler(request):
        assert str(request.url) == "https://example.com/talk.mov"
        return httpx.Response(200, content=clip, headers={"content-type": "video/quicktime"})

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    out = await _mcp_tool(st, "create_orb")(name="Moon", idle="moon.png",
                                            speaking="https://example.com/talk.mov", select=True)
    assert "Moon" in out and "idle, speaking" in out and "selected" in out
    assert not (inbox / "moon.png").exists()                      # inbox file consumed
    orb_id = st.orbs.id_for_name("Moon")
    assert st.orbs.get(orb_id)["media"]["speaking"]["kind"] == "video"
    assert st.mqtt_bridge.orb_options == [DEFAULT_ID, "Moon"]     # visible to every device
    assert st.current_orb == orb_id and any(p[0] == "rpi" for p in st.pushed)
    # Same name again = add/replace media on the existing orb, not a duplicate.
    (inbox / "think.png").write_bytes(png)
    out = await _mcp_tool(st, "create_orb")(name="moon", processing="think.png")
    assert "idle, processing, speaking" in out and len(st.orbs.list()) == 1
    assert "Moon" in await _mcp_tool(st, "list_orbs")()
    assert await _mcp_tool(st, "select_orb")(name="default") == "Orb set to default"
    assert await _mcp_tool(st, "delete_orb")(name="Moon") == "Deleted orb Moon"
    assert st.orbs.list() == []


@pytest.mark.asyncio
async def test_mcp_create_orb_rejects_bad_sources(st, inbox):
    st.local_tools = None
    create = _mcp_tool(st, "create_orb")
    assert "inbox" in await create(name="X", idle="../index.json")    # no path escape
    assert "idle" in await create(name="X", speaking="a.png")         # new orb needs idle
    assert "theme" in await create(name="default", idle="a.png")
    (inbox / "junk.png").write_bytes(b"not an image")
    assert "failed" in await create(name="X", idle="junk.png")
    assert st.orbs.list() == []                                       # nothing half-made
