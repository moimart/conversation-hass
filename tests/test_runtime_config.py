"""Tests for the live runtime config."""

import json
import os

import pytest

from server.app.runtime_config import RuntimeConfig, DEFAULT_KEYS


def test_bootstraps_from_env_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("THEME_DAY", "japandi")
    monkeypatch.setenv("THEME_NIGHT", "odyssey")
    monkeypatch.setenv("WAKE_WORD", "hey hal")
    monkeypatch.setenv("AUTO_THEME", "false")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    assert values["theme_day"] == "japandi"
    assert values["theme_night"] == "odyssey"
    assert values["auto_theme"] is False
    # File should now exist with the bootstrap values.
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["theme_day"] == "japandi"
    assert written["auto_theme"] is False


def test_file_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("THEME_DAY", "birch")
    file_path = tmp_path / "config.json"
    file_path.write_text(json.dumps({"theme_day": "odyssey"}))
    cfg = RuntimeConfig(str(file_path))
    values = cfg.load()
    assert values["theme_day"] == "odyssey"


def test_missing_keys_filled_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKE_WORD", "hey homie")
    file_path = tmp_path / "config.json"
    file_path.write_text(json.dumps({"theme_day": "birch"}))
    cfg = RuntimeConfig(str(file_path))
    values = cfg.load()
    assert values["theme_day"] == "birch"           # from file
    assert values["wake_word"] == "hey homie"       # from env (file missing this key)
    # The pre-existing file should not have been rewritten on load.
    on_disk = json.loads(file_path.read_text())
    assert on_disk == {"theme_day": "birch"}


def test_malformed_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("THEME_DAY", "birch")
    file_path = tmp_path / "config.json"
    file_path.write_text("{ this is not json")
    cfg = RuntimeConfig(str(file_path))
    values = cfg.load()
    assert values["theme_day"] == "birch"
    # Don't overwrite a malformed file — preserve so the user can fix it.
    assert "this is not json" in file_path.read_text()


def test_set_persists_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("THEME_DAY", "birch")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    cfg.load()
    cfg.set("theme_day", "japandi")
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["theme_day"] == "japandi"
    # Subsequent load reflects the new value, not the env default.
    cfg2 = RuntimeConfig(str(tmp_path / "config.json"))
    assert cfg2.load()["theme_day"] == "japandi"


def test_unknown_keys_are_preserved(tmp_path):
    file_path = tmp_path / "config.json"
    file_path.write_text(json.dumps({"theme_day": "birch", "future_key": 42}))
    cfg = RuntimeConfig(str(file_path))
    values = cfg.load()
    assert values["future_key"] == 42
    cfg.set("theme_day", "odyssey")
    on_disk = json.loads(file_path.read_text())
    assert on_disk["future_key"] == 42


def test_auto_theme_string_coercion(tmp_path, monkeypatch):
    """AUTO_THEME=true string must coerce to True."""
    monkeypatch.setenv("AUTO_THEME", "yes")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    assert cfg.load()["auto_theme"] is True
    monkeypatch.setenv("AUTO_THEME", "0")
    cfg2 = RuntimeConfig(str(tmp_path / "config2.json"))
    assert cfg2.load()["auto_theme"] is False


def test_default_keys_has_expected_entries():
    # Sanity: the keys we manage match what the rest of the code expects.
    assert "theme_day" in DEFAULT_KEYS
    assert "theme_night" in DEFAULT_KEYS
    assert "tts_voice" in DEFAULT_KEYS
    assert "wake_word" in DEFAULT_KEYS
    assert "ollama_model" in DEFAULT_KEYS
    assert "auto_theme" in DEFAULT_KEYS


def test_photo_frame_video_keys_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_FRAME_VIDEO_URL", raising=False)
    monkeypatch.delenv("PHOTO_FRAME_VIDEO_MODE", raising=False)
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    assert values["photo_frame_video_url"] == ""
    assert values["photo_frame_video_mode"] is False


def test_photo_frame_video_mode_coerced_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_FRAME_VIDEO_MODE", "on")
    monkeypatch.setenv("PHOTO_FRAME_VIDEO_URL", "http://nas/loop.mp4")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    assert values["photo_frame_video_mode"] is True
    assert values["photo_frame_video_url"] == "http://nas/loop.mp4"


def test_weather_keys_default_and_persist(tmp_path, monkeypatch):
    monkeypatch.delenv("WEATHER_ENTITY", raising=False)
    monkeypatch.delenv("WEATHER_ENABLED", raising=False)
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    # Defaults: no entity, shown by default.
    assert values["weather_entity"] == ""
    assert values["weather_enabled"] is True
    # Persist round-trip.
    cfg.set("weather_entity", "weather.home")
    cfg.set("weather_enabled", False)
    cfg2 = RuntimeConfig(str(tmp_path / "config.json"))
    reloaded = cfg2.load()
    assert reloaded["weather_entity"] == "weather.home"
    assert reloaded["weather_enabled"] is False


def test_weather_enabled_coerced_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_ENABLED", "off")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    assert cfg.load()["weather_enabled"] is False


def test_photo_frame_show_clock_defaults_on(tmp_path, monkeypatch):
    # Default must be ON (clock shown during photo mode) when unset.
    monkeypatch.delenv("PHOTO_FRAME_SHOW_CLOCK", raising=False)
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    assert values["photo_frame_show_clock"] is True


def test_photo_frame_show_clock_coerced_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_FRAME_SHOW_CLOCK", "off")
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    values = cfg.load()
    assert values["photo_frame_show_clock"] is False


def test_cloud_llm_model_persists_but_enabled_never_does(tmp_path):
    # The MODEL is a runtime-config key (persists across restarts); the
    # ENABLED switch deliberately is NOT — it must boot OFF every restart.
    from server.app.runtime_config import DEFAULT_KEYS
    assert "cloud_llm_model" in DEFAULT_KEYS
    assert "cloud_llm_enabled" not in DEFAULT_KEYS
    cfg = RuntimeConfig(str(tmp_path / "config.json"))
    cfg.load()
    cfg.set("cloud_llm_model", "openai/gpt-test")
    reloaded = RuntimeConfig(str(tmp_path / "config.json")).load()
    assert reloaded["cloud_llm_model"] == "openai/gpt-test"
