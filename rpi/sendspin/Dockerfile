FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# PortAudio + Pulse client libs for audio output; curl for hook callbacks.
# libasound2-plugins provides the ALSA->Pulse bridge so PortAudio's ALSA
# backend can talk to pipewire-pulse without /dev/snd.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    libasound2-plugins \
    pulseaudio-utils \
    libpulse0 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf

RUN pip install --no-cache-dir sendspin

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
