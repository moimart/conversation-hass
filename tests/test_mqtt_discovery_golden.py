"""Golden-snapshot guard for the MQTT discovery payloads.

The bridge publishes ~57 HA MQTT-discovery entities. This test pins the exact
generated payloads to a fixture so the data-driven-registry refactor (and any
future change) can't silently alter, drop, or reshape an HA entity. If you
intentionally change discovery, regenerate the fixture:

    python -c "import json; from server.app.mqtt_bridge import MQTTBridge; \
        b=MQTTBridge(host='x', device_id='hal-default', device_name='HAL'); \
        json.dump(sorted([[t,p] for t,p in b._discovery_payloads()], key=lambda x:x[0]), \
        open('tests/fixtures/mqtt_discovery_golden.json','w'), indent=2, sort_keys=True)"
"""

import json
from pathlib import Path

from server.app.mqtt_bridge import MQTTBridge

GOLDEN = Path(__file__).parent / "fixtures" / "mqtt_discovery_golden.json"


def _current():
    b = MQTTBridge(host="x", device_id="hal-default", device_name="HAL")
    return sorted([[t, p] for t, p in b._discovery_payloads()], key=lambda x: x[0])


def test_discovery_payloads_match_golden():
    expected = json.loads(GOLDEN.read_text())
    actual = json.loads(json.dumps(_current()))  # round-trip to normalize tuples→lists
    assert actual == expected, (
        "MQTT discovery payloads changed. If intentional, regenerate the "
        "fixture (see this file's docstring)."
    )


def test_discovery_entity_count_is_stable():
    # Cheap canary independent of the byte-for-byte fixture.
    b = MQTTBridge(host="x", device_id="hal-default", device_name="HAL")
    assert len(b._discovery_payloads()) == 62


def test_every_config_entity_has_set_and_state_topics():
    # Each `config_*` discovery entity must declare a command_topic ending
    # /set and a state_topic ending /state — the topic contract HA depends on.
    b = MQTTBridge(host="x", device_id="hal-default", device_name="HAL")
    config_entities = [
        (t, p) for t, p in b._discovery_payloads()
        if str(p.get("unique_id", "")).startswith(f"{b.device_id}_config_")
    ]
    assert config_entities, "expected some config_* entities"
    for topic, payload in config_entities:
        assert payload["command_topic"].endswith("/set"), topic
        assert payload["state_topic"].endswith("/state"), topic
