"""pal-mdns advertiser: the decision logic (LAN-IP detection, instance name, TXT
records, ServiceInfo wire shape) is pure and network-free, so we pin it here
without zeroconf installed or any socket traffic. The register/serve loop in
main() is integration-only (verified live with avahi-browse on the LAN)."""

import importlib.util
import socket
from pathlib import Path

# server/mdns isn't a package (it's the advertiser container's build context),
# so load advertise.py by file path like the gateway/stt-service tests do.
_ADV_PATH = Path(__file__).resolve().parent.parent / "server" / "mdns" / "advertise.py"
_spec = importlib.util.spec_from_file_location("pal_mdns_advertise", _ADV_PATH)
advertise = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(advertise)


def test_detect_lan_ip_env_override_wins():
    assert advertise.detect_lan_ip("192.168.7.42") == "192.168.7.42"
    assert advertise.detect_lan_ip("  10.20.30.185  ") == "10.20.30.185"


def test_detect_lan_ip_autodetect_returns_ipv4():
    # No override → must return a dotted-quad (the egress IP, or the loopback
    # fallback). Either way it parses as a valid IPv4 address.
    ip = advertise.detect_lan_ip(None)
    socket.inet_aton(ip)  # raises if not a valid IPv4
    assert ip.count(".") == 3


def test_detect_lan_ip_blank_override_is_ignored():
    # A blank/whitespace override falls through to autodetect, not "".
    ip = advertise.detect_lan_ip("   ")
    assert ip and ip != ""
    socket.inet_aton(ip)


def test_service_instance_name():
    assert advertise.service_instance_name("HAL") == "HAL._pal._tcp.local."
    assert advertise.service_instance_name("PAL Kitchen") == "PAL Kitchen._pal._tcp.local."
    # Empty/whitespace degrades to a stable default, never a bare dot.
    assert advertise.service_instance_name("") == "PAL._pal._tcp.local."
    assert advertise.service_instance_name("  ") == "PAL._pal._tcp.local."


def test_build_txt_shape():
    txt = advertise.build_txt(name="HAL")
    assert txt == {"name": "HAL", "scheme": "http", "path": "/", "ver": "1"}
    txt2 = advertise.build_txt(name="HAL", scheme="https", ver="3")
    assert txt2["scheme"] == "https" and txt2["ver"] == "3"
    # Empty name degrades to a label, never blank.
    assert advertise.build_txt(name="")["name"] == "PAL"


def test_service_info_kwargs_wire_shape():
    kw = advertise.service_info_kwargs(friendly="HAL", ip="10.20.30.185", port=8765)
    assert kw["type_"] == "_pal._tcp.local."
    assert kw["name"] == "HAL._pal._tcp.local."
    assert kw["port"] == 8765
    # Address is packed 4-byte network order matching the IP we passed.
    assert kw["addresses"] == [socket.inet_aton("10.20.30.185")]
    assert socket.inet_ntoa(kw["addresses"][0]) == "10.20.30.185"
    assert kw["properties"]["name"] == "HAL"
    assert kw["server"].endswith(".local.")


def test_service_info_constructs_with_zeroconf_if_available():
    # When zeroconf is installed, the kwargs must actually build a ServiceInfo.
    zc = __import__("importlib").util.find_spec("zeroconf")
    if zc is None:
        return  # zeroconf not in the test env — pure-helper coverage above suffices
    from zeroconf import ServiceInfo

    kw = advertise.service_info_kwargs(friendly="HAL", ip="10.20.30.185", port=8765)
    info = ServiceInfo(**kw)
    assert info.type == "_pal._tcp.local."
    assert info.port == 8765
