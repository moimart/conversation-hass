"""Remote MCP + OAuth (server/app/oauth_mcp.py): the Claude-connector flow,
end to end against the real FastMCP tier apps, plus the security properties:
redirect allowlist, PKCE, hub-code approval with attempt limits, tier
allowlists (never-remote tools absent), refresh rotation + reuse detection,
revocation, and "off unless enabled"."""
import base64
import hashlib
import re
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from server.app import oauth_mcp as om

BASE = "https://pal.example"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_REMOTE_MCP", "1")
    monkeypatch.setenv("HAL_GATEWAY_URL", BASE + "/")
    monkeypatch.delenv("HAL_DEMO_MODE", raising=False)
    monkeypatch.setenv("OAUTH_STORE_FILE", str(tmp_path / "oauth.json"))
    monkeypatch.setattr(om, "_store", None)
    shown = []

    async def fake_push(state, msg):
        shown.append(msg)
    import server.app.pairing as pairing
    monkeypatch.setattr(pairing, "_push_pairing", fake_push)
    calls = []

    async def call_tool(name, args):
        calls.append((name, args))
        return f"ok:{name}"
    tool_names = {"list_notes", "get_note", "list_timers", "audio_set_volume", "speak_verbatim",
                  "pair_phone", "show_notes", "audio_toggle_mute"}
    state = SimpleNamespace(local_tools=SimpleNamespace(tool_names=tool_names, call_tool=call_tool),
                            current_theme="x", themes=None, orbs=None, pairing=None)
    monkeypatch.setattr(om, "_state", lambda request: state)
    return SimpleNamespace(shown=shown, calls=calls, state=state)


@asynccontextmanager
async def make_client(env):
    """App + session managers entered and exited in the SAME task (anyio)."""
    app = FastAPI()
    app.include_router(om.router)
    async with AsyncExitStack() as stack:
        assert await om.mount_remote_mcp(app, env.state, stack)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            yield c


def _pkce():
    verifier = "v" * 20 + base64.urlsafe_b64encode(b"0123456789abcdef0123").decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _register(c, uris=(CLAUDE_CB,)):
    r = await c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": list(uris),
                                              "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def _authorize(c, cid, challenge, scope="pal.read", redirect=CLAUDE_CB):
    r = await c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
        "scope": scope, "resource": BASE + "/mcp"})
    assert r.status_code == 200, r.text
    return re.search(r"name=rid value=([\w-]+)", r.text).group(1)


async def _approve(c, env, rid, tier="read"):
    r = await c.post("/oauth/authorize", data={"rid": rid, "action": "show", "tier": tier})
    assert r.status_code == 200 and "code is now on your hub" in r.text
    code = env.shown[-1]["code"]
    assert env.shown[-1]["title"] == "Connect Claude"
    r = await c.post("/oauth/authorize", data={"rid": rid, "action": "approve", "code": code})
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(CLAUDE_CB) and q["state"] == ["st8"] and q["iss"] == [BASE]
    return q["code"][0]


async def _connect(c, env, tier="read"):
    cid = await _register(c)
    verifier, challenge = _pkce()
    rid = await _authorize(c, cid, challenge, scope=f"pal.{tier}")
    code = await _approve(c, env, rid, tier)
    om.store().show_times.clear()   # let the next test flow show a code immediately
    r = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                           "client_id": cid, "redirect_uri": CLAUDE_CB,
                                           "code_verifier": verifier})
    assert r.status_code == 200, r.text
    return cid, r.json()


async def _rpc(c, token, method, params=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "MCP-Protocol-Version": "2025-06-18"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return await c.post("/remote-mcp/mcp", headers=h,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


# --- pure helpers ----------------------------------------------------------------

def test_redirects():
    assert om.redirect_allowed(CLAUDE_CB)
    assert om.redirect_allowed("https://claude.com/api/mcp/auth_callback")
    assert om.redirect_allowed("http://localhost:3118/callback")
    assert om.redirect_allowed("http://127.0.0.1:9/callback")
    assert not om.redirect_allowed("https://evil.example/cb")
    assert not om.redirect_allowed("https://claude.ai.evil.example/api/mcp/auth_callback")
    assert not om.redirect_allowed("http://claude.ai/api/mcp/auth_callback")
    assert om.redirect_matches(["http://localhost/callback"], "http://localhost:51234/callback")
    assert not om.redirect_matches(["http://localhost/callback"], "http://localhost:5/other")
    assert not om.redirect_matches([CLAUDE_CB], CLAUDE_CB + "x")


def test_pkce():
    v, ch = _pkce()
    assert om.pkce_ok(v, ch)
    assert not om.pkce_ok(v + "x", ch)
    assert not om.pkce_ok("short", ch)


def test_tiers_never_include_dangerous_tools():
    for tools in om.TIER_TOOLS.values():
        assert not tools & om.NEVER_REMOTE
    assert "pair_phone" in om.NEVER_REMOTE and "create_orb" in om.NEVER_REMOTE


# --- discovery + registration ----------------------------------------------------

@pytest.mark.asyncio
async def test_metadata(env):
    async with make_client(env) as client:
        prm = (await client.get("/.well-known/oauth-protected-resource")).json()
        assert prm["resource"] == BASE + "/mcp" and prm["authorization_servers"] == [BASE]
        asm = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert asm["issuer"] == BASE and asm["code_challenge_methods_supported"] == ["S256"]
        assert asm["registration_endpoint"] == BASE + "/oauth/register"
        assert "none" in asm["token_endpoint_auth_methods_supported"]



@pytest.mark.asyncio
async def test_register_rejects_foreign_redirects(env):
    async with make_client(env) as client:
        r = await client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
        assert r.status_code == 400
        r = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CB],
                                                        "token_endpoint_auth_method": "client_secret_post"})
        assert r.status_code == 400



@pytest.mark.asyncio
async def test_disabled_means_404(env, monkeypatch):
    async with make_client(env) as client:
        monkeypatch.setenv("PAL_REMOTE_MCP", "0")
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server",
                     "/oauth/authorize"):
            assert (await client.get(path)).status_code == 404
        assert (await client.post("/oauth/token", data={})).status_code == 404



# --- 401 handshake + full flow -------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthenticated_mcp_gets_401_with_metadata_pointer(env):
    async with make_client(env) as client:
        r = await _rpc(client, None, "tools/list")
        assert r.status_code == 401
        wa = r.headers["www-authenticate"]
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in wa
        r = await _rpc(client, "not-a-token", "tools/list")
        assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]



@pytest.mark.asyncio
async def test_read_tier_flow(env):
    async with make_client(env) as client:
        _cid, tok = await _connect(client, env, "read")
        assert tok["token_type"] == "Bearer" and tok["scope"] == "pal.read"
        r = await _rpc(client, tok["access_token"], "tools/list")
        assert r.status_code == 200, r.text
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names <= om.TIER_TOOLS["read"] and "list_notes" in names
        assert not names & om.NEVER_REMOTE and "speak_verbatim" not in names
        r = await _rpc(client, tok["access_token"], "tools/call", {"name": "list_notes", "arguments": {}})
        assert r.status_code == 200 and ("list_notes", {"limit": 20, "query": ""}) in env.calls or \
            any(n == "list_notes" for n, _ in env.calls)
        # a control tool doesn't exist for this token
        r = await _rpc(client, tok["access_token"], "tools/call", {"name": "speak_verbatim",
                                                                     "arguments": {"text": "hi"}})
        assert not any(n == "speak_verbatim" for n, _ in env.calls)



@pytest.mark.asyncio
async def test_control_tier_lists_control_tools(env):
    async with make_client(env) as client:
        _cid, tok = await _connect(client, env, "control")
        names = {t["name"] for t in (await _rpc(client, tok["access_token"], "tools/list")).json()["result"]["tools"]}
        assert "speak_verbatim" in names and "set_volume" in names
        assert "pair_phone" not in names and "create_orb" not in names



@pytest.mark.asyncio
async def test_wrong_codes_drop_the_request(env):
    async with make_client(env) as client:
        cid = await _register(client)
        _v, ch = _pkce()
        rid = await _authorize(client, cid, ch)
        await client.post("/oauth/authorize", data={"rid": rid, "action": "show", "tier": "read"})
        good = env.shown[-1]["code"]
        bad = "000000" if good != "000000" else "111111"
        for i in range(om.APPROVAL_MAX_ATTEMPTS - 1):
            r = await client.post("/oauth/authorize", data={"rid": rid, "action": "approve", "code": bad})
            assert r.status_code == 200 and "Wrong code" in r.text
        r = await client.post("/oauth/authorize", data={"rid": rid, "action": "approve", "code": bad})
        assert r.status_code == 403
        # even the right code is useless now
        r = await client.post("/oauth/authorize", data={"rid": rid, "action": "approve", "code": good})
        assert r.status_code == 400



@pytest.mark.asyncio
async def test_show_code_is_throttled(env):
    async with make_client(env) as client:
        cid = await _register(client)
        _v, ch = _pkce()
        rid = await _authorize(client, cid, ch)
        assert (await client.post("/oauth/authorize", data={"rid": rid, "action": "show"})).status_code == 200
        r = await client.post("/oauth/authorize", data={"rid": rid, "action": "show"})
        assert r.status_code == 429 and len([m for m in env.shown if m.get("code")]) == 1



@pytest.mark.asyncio
async def test_authorize_rejects_unknown_redirect_without_redirecting(env):
    async with make_client(env) as client:
        cid = await _register(client)
        _v, ch = _pkce()
        r = await client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": "https://evil.example/cb",
            "code_challenge": ch, "code_challenge_method": "S256"})
        assert r.status_code == 400 and "location" not in r.headers
        r = await client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": CLAUDE_CB})   # no PKCE
        assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]



@pytest.mark.asyncio
async def test_code_needs_pkce_and_is_single_use(env):
    async with make_client(env) as client:
        cid = await _register(client)
        verifier, ch = _pkce()
        code = await _approve(client, env, await _authorize(client, cid, ch))
        bad = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                       "client_id": cid, "redirect_uri": CLAUDE_CB,
                                                       "code_verifier": verifier[:-1] + "Q"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        again = await client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                         "client_id": cid, "redirect_uri": CLAUDE_CB,
                                                         "code_verifier": verifier})
        assert again.status_code == 400            # burnt by the failed attempt



@pytest.mark.asyncio
async def test_refresh_rotation_and_reuse_revokes(env):
    async with make_client(env) as client:
        cid, tok = await _connect(client, env, "read")
        r = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
                                                     "refresh_token": tok["refresh_token"]})
        assert r.status_code == 200
        new = r.json()
        assert new["refresh_token"] != tok["refresh_token"]
        assert (await _rpc(client, new["access_token"], "tools/list")).status_code == 200
        # the OLD refresh token comes back → theft assumed → whole grant revoked
        r = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
                                                     "refresh_token": tok["refresh_token"]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        assert (await _rpc(client, new["access_token"], "tools/list")).status_code == 401



@pytest.mark.asyncio
async def test_revoke_and_persistence(env):
    async with make_client(env) as client:
        _cid, tok = await _connect(client, env, "read")
        grants = (await client.get("/api/oauth/grants")).json()["grants"]
        assert len(grants) == 1 and grants[0]["client"] == "Claude" and grants[0]["tier"] == "read"
        # a fresh store (= ai-server restart) still knows the token; it's hashed on disk
        om._store = None
        assert (await _rpc(client, tok["access_token"], "tools/list")).status_code == 200
        with open(om.store().path) as f:
            assert tok["access_token"] not in f.read()
        assert (await client.post("/api/oauth/revoke", json={"all": True})).json()["revoked"] == 1
        assert (await _rpc(client, tok["access_token"], "tools/list")).status_code == 401

