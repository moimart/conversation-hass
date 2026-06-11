"""pal-mdns: advertise the PAL ai-server on the LAN over mDNS / DNS-SD.

Satellites (phone + watches) browse for ``_pal._tcp.local.`` during
onboarding/enrollment and prefill the server URL with whatever this advertises —
no more typing ``http://10.20.30.185:8765`` by hand on the home network.

Runs as its OWN host-networked container (``network_mode: host``): the ai-server
container is bridge-networked, so an advertiser inside it would announce a
``172.x`` container address and its multicast wouldn't reach the LAN. Sharing the
host net namespace lets us announce the real LAN IP and have multicast escape.

All the decision logic (LAN-IP detection, TXT records, the ServiceInfo kwargs)
is in pure, network-free helpers so it's unit-testable without zeroconf or a
live socket; ``main()`` is the only part that actually touches the network.
"""

from __future__ import annotations

import os
import signal
import socket
import threading

SERVICE_TYPE = "_pal._tcp.local."


def detect_lan_ip(env_ip: str | None = None) -> str:
    """Best LAN IPv4 to advertise.

    An explicit ``PAL_MDNS_IP`` override always wins. Otherwise open a UDP socket
    "toward" the LAN and read back the kernel-chosen source address — this needs
    no packets on the wire and picks the primary egress interface. Falls back to
    loopback if even that fails (degraded, but never raises).
    """
    if env_ip and env_ip.strip():
        return env_ip.strip()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # The address need not be reachable; we only want the routing decision.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def service_instance_name(friendly: str) -> str:
    """Fully-qualified DNS-SD instance name, e.g. ``HAL._pal._tcp.local.``."""
    label = (friendly or "PAL").strip() or "PAL"
    return f"{label}.{SERVICE_TYPE}"


def build_txt(
    *,
    name: str,
    scheme: str = "http",
    path: str = "/",
    ver: str = "1",
) -> dict[str, str]:
    """TXT key/value pairs clients use to label + build the URL."""
    return {"name": name or "PAL", "scheme": scheme, "path": path, "ver": ver}


def service_info_kwargs(
    *,
    friendly: str,
    ip: str,
    port: int,
    ver: str = "1",
    scheme: str = "http",
) -> dict:
    """Keyword args for ``zeroconf.ServiceInfo`` — pure, so tests can assert the
    wire shape (type, instance name, packed address, port, TXT) without zeroconf
    installed or any network access."""
    txt = build_txt(name=friendly, scheme=scheme, ver=ver)
    return {
        "type_": SERVICE_TYPE,
        "name": service_instance_name(friendly),
        "addresses": [socket.inet_aton(ip)],
        "port": port,
        "properties": txt,
        "server": f"{socket.gethostname()}.local.",
    }


def main() -> None:
    from zeroconf import ServiceInfo, Zeroconf

    friendly = os.environ.get("HAL_DEVICE_NAME", "HAL")
    port = int(os.environ.get("PAL_MDNS_PORT", "8765"))
    ver = os.environ.get("PAL_MDNS_VER", "1")
    ip = detect_lan_ip(os.environ.get("PAL_MDNS_IP"))

    info = ServiceInfo(**service_info_kwargs(friendly=friendly, ip=ip, port=port, ver=ver))
    zc = Zeroconf()
    zc.register_service(info)
    print(f"[pal-mdns] advertising {service_instance_name(friendly)} -> {ip}:{port}", flush=True)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        # Withdraw the record so satellites don't chase a stale entry after we go.
        print("[pal-mdns] unregistering", flush=True)
        zc.unregister_service(info)
        zc.close()


if __name__ == "__main__":
    main()
