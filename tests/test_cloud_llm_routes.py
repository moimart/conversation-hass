"""Regression: /api/cloud_llm reports `available` (provider configured or not).

The mobile settings sheet shows its "Cloud LLM" toggle only when the server
has a cloud provider configured — fully-local installs must get
`available: false` so the toggle stays hidden. The response must NEVER include
provider keys or endpoints (cloud_providers.json stays server-side)."""
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_main(monkeypatch, tmp_path):
    # Deterministic "no provider" baseline: nonexistent providers file and no
    # env-var fallback keys (a dev machine may have OPENAI_API_KEY exported).
    monkeypatch.setenv("CLOUD_PROVIDERS_PATH", str(tmp_path / "none.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = SimpleNamespace(
        cloud_llm_enabled=False,
        cloud_llm_model="",
        cloud_model_options=[],
        cloud_llm_client=None,
        mqtt_bridge=None,
    )
    fake = SimpleNamespace(_get_state=lambda app: st)
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return st


def _req():
    return SimpleNamespace(app=SimpleNamespace(), headers={}, query_params={})


@pytest.mark.asyncio
async def test_get_unavailable_when_no_provider(fake_main):
    from server.app.routes_http import get_cloud_llm
    out = await get_cloud_llm(_req())
    assert out["available"] is False
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_get_available_with_provider(fake_main):
    from server.app.routes_http import get_cloud_llm
    fake_main.cloud_llm_client = object()
    fake_main.cloud_llm_enabled = True
    fake_main.cloud_llm_model = "openai/gpt-5.5"
    out = await get_cloud_llm(_req())
    assert out == {
        "enabled": True,
        "model": "openai/gpt-5.5",
        "options": [],
        "available": True,
    }


@pytest.mark.asyncio
async def test_available_from_registry_before_first_enable(fake_main, monkeypatch, tmp_path):
    """The client is created lazily on first enable — a configured providers
    file alone must already report available, or the mobile toggle would stay
    hidden until someone flips the switch elsewhere first."""
    from server.app.routes_http import get_cloud_llm
    p = tmp_path / "cloud_providers.json"
    p.write_text('{"providers": [{"name": "openai", '
                 '"base_url": "https://api.openai.com/v1", "api_key": "sk-test"}]}')
    monkeypatch.setenv("CLOUD_PROVIDERS_PATH", str(p))
    out = await get_cloud_llm(_req())
    assert out["available"] is True
    assert out["enabled"] is False   # availability ≠ enabled


@pytest.mark.asyncio
async def test_post_reports_available(fake_main):
    from server.app.routes_http import CloudLLMRequest, post_cloud_llm
    fake_main.cloud_llm_client = object()
    out = await post_cloud_llm(_req(), CloudLLMRequest(enabled=True))
    assert out["available"] is True
    assert out["enabled"] is True


@pytest.mark.asyncio
async def test_never_leaks_provider_secrets(fake_main):
    """The status payload must only ever contain the four public fields."""
    from server.app.routes_http import get_cloud_llm
    fake_main.cloud_llm_client = object()
    out = await get_cloud_llm(_req())
    assert set(out) == {"enabled", "model", "options", "available"}
