"""Tests for the plug-in theme registry."""

import json
import os
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.app.themes import ThemeRegistry, Theme, _load_theme


def _make_theme(root: Path, name: str, **manifest_overrides) -> Path:
    """Create a minimal valid theme directory under root."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "display_name": f"{name.title()} Theme",
        "description": f"Test theme {name}",
        "version": "1.0.0",
        "kind": "dark",
    }
    manifest.update(manifest_overrides)
    (d / "manifest.json").write_text(json.dumps(manifest))
    (d / "theme.css").write_text(f"body.theme-{name} {{ --bg: #000; }}")
    return d


def test_load_minimal_theme(tmp_path):
    _make_theme(tmp_path, "alpha")
    t = _load_theme(str(tmp_path / "alpha"), "alpha")
    assert t is not None
    assert t.name == "alpha"
    assert t.display_name == "Alpha Theme"
    assert t.kind == "dark"
    assert t.has_effect is False
    assert t.fingerprint


def test_load_theme_with_effect(tmp_path):
    d = _make_theme(tmp_path, "anim", effect="effect.js")
    (d / "effect.js").write_text("export default function() {}")
    t = _load_theme(str(d), "anim")
    assert t.has_effect is True


def test_load_theme_with_missing_effect_file_falls_back(tmp_path):
    """Manifest declares an effect but the file doesn't exist."""
    _make_theme(tmp_path, "broken", effect="effect.js")
    t = _load_theme(str(tmp_path / "broken"), "broken")
    assert t.has_effect is False


def test_load_theme_invalid_kind_defaults_to_dark(tmp_path):
    _make_theme(tmp_path, "weird", kind="psychedelic")
    t = _load_theme(str(tmp_path / "weird"), "weird")
    assert t.kind == "dark"


def test_load_theme_missing_manifest_returns_none(tmp_path):
    d = tmp_path / "skel"
    d.mkdir()
    (d / "theme.css").write_text("/* no manifest */")
    assert _load_theme(str(d), "skel") is None


def test_load_theme_missing_css_returns_none(tmp_path):
    d = tmp_path / "skel"
    d.mkdir()
    (d / "manifest.json").write_text("{}")
    assert _load_theme(str(d), "skel") is None


def test_load_theme_malformed_manifest_returns_none(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "manifest.json").write_text("{not json")
    (d / "theme.css").write_text("")
    assert _load_theme(str(d), "bad") is None


def test_registry_scan_picks_up_themes(tmp_path):
    _make_theme(tmp_path, "alpha")
    _make_theme(tmp_path, "beta")
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    assert sorted(reg.names) == ["alpha", "beta"]


def test_registry_skips_dot_directories(tmp_path):
    _make_theme(tmp_path, "alpha")
    (tmp_path / ".git").mkdir()
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    assert reg.names == ["alpha"]


def test_registry_scan_diff_detects_change(tmp_path):
    _make_theme(tmp_path, "alpha")
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    # No change on a re-scan
    assert reg.scan() is False
    # Add another theme
    _make_theme(tmp_path, "beta")
    assert reg.scan() is True


def test_registry_diff_detects_manifest_edit(tmp_path):
    _make_theme(tmp_path, "alpha")
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    # Edit the manifest
    (tmp_path / "alpha" / "manifest.json").write_text(json.dumps({
        "name": "alpha",
        "display_name": "Alpha Renamed",
        "version": "2.0.0",
        "kind": "dark",
    }))
    assert reg.scan() is True
    assert reg.get("alpha").display_name == "Alpha Renamed"


def test_registry_static_path_safe(tmp_path):
    d = _make_theme(tmp_path, "alpha")
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    css = reg.static_path("alpha", "theme.css")
    assert css and os.path.isfile(css)
    # Traversal attempts get None
    assert reg.static_path("alpha", "../../../etc/passwd") is None
    assert reg.static_path("alpha", "..") is None
    assert reg.static_path("alpha", "subdir/file.css") is None
    # Unknown theme
    assert reg.static_path("nope", "theme.css") is None


def test_registry_to_public():
    t = Theme(
        name="alpha",
        display_name="Alpha",
        description="d",
        version="1.0.0",
        kind="dark",
        has_effect=True,
        dir_path="/x",
        fingerprint="xx",
    )
    pub = t.to_public()
    assert pub["name"] == "alpha"
    assert pub["has_effect"] is True
    assert "dir_path" not in pub
    assert "fingerprint" not in pub


@pytest.mark.asyncio
async def test_registry_polling_fires_listener(tmp_path):
    _make_theme(tmp_path, "alpha")
    reg = ThemeRegistry(str(tmp_path))
    reg.scan()
    seen = []

    async def listener(themes):
        seen.append([t.name for t in themes])

    reg.add_listener(listener)
    await reg.start_polling(interval_s=0.05)
    # Trigger a change between polls
    await asyncio.sleep(0.1)
    _make_theme(tmp_path, "beta")
    await asyncio.sleep(0.2)
    await reg.stop_polling()
    assert any("beta" in entry for entry in seen)
