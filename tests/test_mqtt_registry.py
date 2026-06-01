"""Tests for the data-driven config-entity registry in mqtt_bridge.py.

Covers the generic API that replaced the ~23 per-entity publish_config_* /
on_config_* / _cached_config_* triplets: set_config_callback, publish_config,
the per-entity parse/serialize, and the republish-skip-when-empty contract.
"""

from unittest.mock import AsyncMock

import pytest

from server.app.mqtt_bridge import MQTTBridge, CONFIG_ENTITIES, _CONFIG_BY_KEY


def _bridge():
    b = MQTTBridge(host="x", device_id="hal-default", device_name="HAL")
    b._safe_publish = AsyncMock()
    return b


def test_registry_covers_expected_keys():
    keys = {e.key for e in CONFIG_ENTITIES}
    # A representative spread across platforms must be present.
    for k in ("theme_day", "num_ctx", "start_muted", "photo_frame_video_mode",
              "router_model", "openclaw_gateway_url"):
        assert k in keys, k
    assert _CONFIG_BY_KEY["num_ctx"].platform == "number"
    assert _CONFIG_BY_KEY["start_muted"].platform == "switch"


@pytest.mark.asyncio
async def test_publish_config_serializes_and_caches():
    b = _bridge()
    await b.publish_config("start_muted", True)
    b._safe_publish.assert_awaited()
    topic, payload = b._safe_publish.await_args.args[0], b._safe_publish.await_args.args[1]
    assert topic == "hal/hal-default/config/start_muted/state"
    assert payload == "ON"                  # switch serialize
    assert b._cached_config["start_muted"] is True   # cached for republish


@pytest.mark.asyncio
async def test_fallback_model_empty_serializes_to_sentinel():
    b = _bridge()
    await b.publish_config("fallback_ollama_model", "")
    payload = b._safe_publish.await_args.args[1]
    assert payload == "(none — disabled)"


def test_set_config_callback_registers():
    b = _bridge()
    cb = AsyncMock()
    b.set_config_callback("wake_word", cb)
    assert b._config_callbacks["wake_word"] is cb


def test_switch_parse_roundtrip():
    e = _CONFIG_BY_KEY["start_muted"]
    assert e.parse("ON") is True
    assert e.parse("off") is False
    assert e.serialize(True) == "ON"
    assert e.serialize(False) == "OFF"


def test_guarded_selects_skip_republish_when_empty():
    # theme_day/theme_night/wake_word/ollama_model must NOT push an empty
    # state on reconnect (would clear the HA selection). Others may.
    for k in ("theme_day", "theme_night", "wake_word", "ollama_model"):
        assert _CONFIG_BY_KEY[k].republish_if_empty is False, k
    assert _CONFIG_BY_KEY["tts_voice"].republish_if_empty is True
