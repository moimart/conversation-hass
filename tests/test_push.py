"""Push-notification dispatch (server/app/push.py): signed image URLs, payload
builders, offline targeting, and the fan-out/clear-dead-token logic. No real
APNs/FCM is touched — senders are stubbed."""
import asyncio
import time

import pytest

from server.app import push

GW = "https://pal.example"
SECRET = "s3cr3t-signing-key"


# --- signed image URLs -------------------------------------------------------

def _parts(url):
    return dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))


def test_sign_verify_roundtrip():
    url = push.sign_image_url(GW, SECRET, 42)
    assert url.startswith(f"{GW}/api/push/image/42.jpg?exp=")
    p = _parts(url)
    assert push.verify_image_sig(SECRET, 42, p["exp"], p["sig"]) is True


def test_verify_rejects_tampered_id_exp_sig():
    p = _parts(push.sign_image_url(GW, SECRET, 42))
    assert push.verify_image_sig(SECRET, 99, p["exp"], p["sig"]) is False          # id
    assert push.verify_image_sig(SECRET, 42, int(p["exp"]) + 1, p["sig"]) is False  # exp
    assert push.verify_image_sig(SECRET, 42, p["exp"], "deadbeef") is False         # sig


def test_verify_rejects_expired():
    now = 1_000_000
    exp = now - 1
    sig = push._sig(SECRET, push._canonical(42, exp))
    assert push.verify_image_sig(SECRET, 42, exp, sig, now=now) is False


def test_verify_rejects_far_future_exp():
    # Even a validly-signed URL is rejected if exp is beyond the clamp window
    # (defends against a bug minting eternal URLs).
    now = 1_000_000
    exp = now + push._IMAGE_TTL_S + 100
    sig = push._sig(SECRET, push._canonical(42, exp))
    assert push.verify_image_sig(SECRET, 42, exp, sig, now=now) is False


def test_verify_requires_secret_and_sig():
    assert push.verify_image_sig("", 1, int(time.time()) + 10, "x") is False
    assert push.verify_image_sig(SECRET, 1, int(time.time()) + 10, "") is False


# --- payload builders --------------------------------------------------------

def test_apns_payload_text_only_has_no_mutable_content():
    p = push.build_apns_payload("speak", "hi there", None, "speak")
    assert p["aps"]["alert"] == {"title": "PAL", "body": "hi there"}
    assert p["aps"]["category"] == "speak"
    assert "mutable-content" not in p["aps"]
    assert "image_url" not in p


def test_apns_payload_with_image_sets_mutable_content():
    p = push.build_apns_payload("image", "pic", "https://x/y.jpg", "image")
    assert p["aps"]["mutable-content"] == 1
    assert p["image_url"] == "https://x/y.jpg"


def test_apns_body_is_clipped():
    p = push.build_apns_payload("speak", "a" * 5000, None, None)
    assert len(p["aps"]["alert"]["body"]) == push._BODY_MAX


def test_fcm_channel_and_image_mapping():
    m = push.build_fcm_message("tok", "timer", "done", "https://x/y.jpg", "timer")
    assert m["token"] == "tok"
    assert m["android"]["notification"]["channel_id"] == "timers"
    assert m["notification"] == {"title": "Timer", "body": "done", "image": "https://x/y.jpg"}
    m2 = push.build_fcm_message("tok", "speak", "hi", None, "speak")
    assert m2["android"]["notification"]["channel_id"] == "announcements"
    assert "image" not in m2["notification"]


# --- offline targeting + dispatch -------------------------------------------

class FakePairing:
    def __init__(self, targets):
        self._t = targets
        self.cleared = []

    def push_targets(self):
        return list(self._t)

    def clear_push_token(self, token):
        self.cleared.append(token)


class FakeState:
    def __init__(self, pairing, satellite_ws=None):
        self.pairing = pairing
        self.satellite_ws = satellite_ws or {}


class FakeSender:
    def __init__(self, result=(True, False)):
        self.result = result
        self.sends = []

    async def send(self, *a):
        self.sends.append(a)
        return self.result


class FakeRegistry:
    def __init__(self, configured=True):
        self._c = configured

    def configured(self):
        return self._c

    def apns(self):
        return object()

    def fcm(self):
        return object()


def test_offline_targets_excludes_connected_and_tokenless():
    pairing = FakePairing([("t1", "apns", "p1"), ("t2", "fcm", "p2")])
    state = FakeState(pairing, satellite_ws={"t1": object()})   # t1 app open
    assert push._offline_targets(state) == [("t2", "fcm", "p2")]


def test_offline_targets_empty():
    assert push._offline_targets(FakeState(FakePairing([]))) == []


def test_offline_targets_dedups_duplicate_push_token():
    # Same device re-paired under two pairing tokens (same push_token): notify once.
    pairing = FakePairing([("tA", "fcm", "dup"), ("tB", "fcm", "dup"), ("tC", "apns", "p3")])
    out = push._offline_targets(FakeState(pairing))
    assert sorted(pt for _t, _s, pt in out) == ["dup", "p3"]
    assert len(out) == 2


def test_offline_targets_skips_device_open_under_any_pairing_token():
    # If any pairing entry sharing the push_token is connected, the device is open.
    pairing = FakePairing([("tA", "fcm", "dup"), ("tB", "fcm", "dup")])
    state = FakeState(pairing, satellite_ws={"tB": object()})
    assert push._offline_targets(state) == []


def test_image_url_needs_gateway_secret_and_id():
    assert push.PushService(FakeRegistry(), SECRET, "").image_url(5) is None
    assert push.PushService(FakeRegistry(), "", GW).image_url(5) is None
    assert push.PushService(FakeRegistry(), SECRET, GW).image_url(None) is None
    assert push.PushService(FakeRegistry(), SECRET, GW).image_url(5).startswith(GW)


@pytest.mark.asyncio
async def test_send_all_routes_by_service_and_clears_dead():
    pairing = FakePairing([])
    state = FakeState(pairing)
    svc = push.PushService(FakeRegistry(), SECRET, GW)
    apns = FakeSender(result=(False, True))    # APNs reports the token dead
    fcm = FakeSender(result=(True, False))
    svc._apns_sender = lambda: apns
    svc._fcm_sender = lambda: fcm
    await svc._send_all(state, [("t1", "apns", "p1"), ("t2", "fcm", "p2")],
                        "speak", "hello", None, "speak")
    assert len(apns.sends) == 1 and len(fcm.sends) == 1
    assert pairing.cleared == ["t1"]           # only the dead one cleared


@pytest.mark.asyncio
async def test_send_all_skips_reconnected_device():
    pairing = FakePairing([])
    state = FakeState(pairing, satellite_ws={"t1": object()})   # reconnected mid-flight
    svc = push.PushService(FakeRegistry(), SECRET, GW)
    apns = FakeSender()
    svc._apns_sender = lambda: apns
    await svc._send_all(state, [("t1", "apns", "p1")], "speak", "hi", None, "speak")
    assert apns.sends == []


@pytest.mark.asyncio
async def test_dispatch_noop_when_unconfigured():
    svc = push.PushService(FakeRegistry(configured=False), SECRET, GW)
    state = FakeState(FakePairing([("t1", "apns", "p1")]))
    sent = []
    svc._apns_sender = lambda: FakeSender()
    await svc.dispatch(state, "speak", "hi")
    await asyncio.sleep(0)
    # unconfigured -> never schedules a send
    assert svc._apns_sender().sends == []


@pytest.mark.asyncio
async def test_dispatch_schedules_send_when_targets_exist():
    svc = push.PushService(FakeRegistry(), SECRET, GW)
    state = FakeState(FakePairing([("t1", "apns", "p1")]))
    apns = FakeSender()
    svc._apns_sender = lambda: apns
    await svc.dispatch(state, "speak", "hi", category="speak")
    await asyncio.sleep(0.05)                  # let the fire-and-forget task run
    assert len(apns.sends) == 1
