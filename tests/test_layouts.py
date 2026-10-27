"""Per-device satellite layouts: validation/clamping, versioned atomic save,
and pruning of unpaired devices."""
import json

import pytest

from server.app.layouts import LayoutError, LayoutStore, validate


def _layout(**over):
    base = {
        "cards": [{"id": "c1", "config": {"type": "tile", "entity": "light.kitchen"}}],
        "portrait": {"items": [{"id": "orb", "x": 0, "y": 0, "w": 6, "h": 6},
                               {"id": "c1", "x": 0, "y": 6, "w": 3, "h": 2}]},
        "landscape": {"items": [{"id": "orb", "x": 0, "y": 0, "w": 5, "h": 5}]},
    }
    base.update(over)
    return base


def test_validate_clamps_rects_to_the_grid():
    out = validate(_layout(portrait={"items": [
        {"id": "orb", "x": 99, "y": -3, "w": 40, "h": 0},
        {"id": "c1", "x": 5, "y": 1, "w": 3, "h": 2},
    ]}))
    orb, card = out["portrait"]["items"]
    assert orb == {"id": "orb", "x": 0, "y": 0, "w": 6, "h": 1}   # portrait is 6 wide
    assert card["x"] == 3                                         # pulled back inside


def test_validate_drops_unknown_and_duplicate_placements():
    out = validate(_layout(landscape={"items": [
        {"id": "orb", "x": 0, "y": 0, "w": 4, "h": 4},
        {"id": "orb", "x": 4, "y": 0, "w": 4, "h": 4},
        {"id": "ghost", "x": 0, "y": 0, "w": 1, "h": 1},
    ]}))
    assert [i["id"] for i in out["landscape"]["items"]] == ["orb"]


@pytest.mark.parametrize("cards", [
    [{"id": "Bad ID", "config": {"type": "tile"}}],
    [{"id": "orb", "config": {"type": "tile"}}],                     # reserved id
    [{"id": "c1", "config": {"type": "tile"}}, {"id": "c1", "config": {"type": "x"}}],
    [{"id": "c1", "config": {"entity": "light.x"}}],                 # no type
    [{"id": "c1", "config": {"type": "markdown", "content": "x" * 40000}}],
    [{"id": f"c{i}", "config": {"type": "tile"}} for i in range(41)],
])
def test_validate_rejects_bad_cards(cards):
    with pytest.raises(LayoutError):
        validate(_layout(cards=cards))


def test_store_put_get_version_and_persist(tmp_path):
    path = tmp_path / "layouts.json"
    s = LayoutStore(str(path))
    assert s.get("dev1") is None
    assert s.put("dev1", _layout())["version"] == 1
    assert s.put("dev1", _layout())["version"] == 2
    again = LayoutStore(str(path))
    assert again.get("dev1")["cards"][0]["id"] == "c1"
    assert json.loads(path.read_text())["dev1"]["version"] == 2
    assert not list(tmp_path.glob(".satellite_layouts.*"))       # no temp left behind


def test_store_prune_and_delete(tmp_path):
    s = LayoutStore(str(tmp_path / "l.json"))
    s.put("keep", _layout())
    s.put("gone", _layout())
    assert s.prune(["keep"]) == 1
    assert s.get("gone") is None and s.get("keep") is not None
    assert s.delete("keep") is True and s.delete("keep") is False
