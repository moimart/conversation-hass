# pal-mdns: tiny host-networked mDNS advertiser for the PAL ai-server.
# See server/mdns/advertise.py. Runs with `network_mode: host` in compose so its
# multicast reaches the LAN (the bridge-networked ai-server can't do this).
FROM python:3.11-slim

WORKDIR /app

# zeroconf is the only dependency; no torch/HF here, so the image stays tiny.
RUN pip install --no-cache-dir "zeroconf>=0.130,<1.0"

COPY advertise.py .

CMD ["python", "advertise.py"]
