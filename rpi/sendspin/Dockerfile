FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# PortAudio + Pulse client libs for audio output; curl for hook callbacks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    pulseaudio-utils \
    libpulse0 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir sendspin

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
