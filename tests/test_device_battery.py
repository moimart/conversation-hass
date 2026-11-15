"""Battery reports carried in satellite pings (PAL Round)."""
import logging
from types import SimpleNamespace

from server.app.routes_ws import _note_battery


def test_battery_stored_and_logged_on_change(caplog):
    state = SimpleNamespace()
    with caplog.at_level(logging.INFO, logger="hal"):
        _note_battery(state, "PAL Round 0274", 92, True)
        _note_battery(state, "PAL Round 0274", 92, True)   # unchanged: no new line
        _note_battery(state, "PAL Round 0274", 91, False)
    assert state.device_battery["PAL Round 0274"] == {"battery": 91, "charging": False}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("battery")]
    assert lines == ["battery PAL Round 0274: 92% (charging)", "battery PAL Round 0274: 91%"]


def test_no_battery_not_logged(caplog):
    state = SimpleNamespace()
    with caplog.at_level(logging.INFO, logger="hal"):
        _note_battery(state, "x", -1, False)
    assert not [r for r in caplog.records if r.getMessage().startswith("battery")]
