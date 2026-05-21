FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System dependencies for audio + build tools for PyAudio, plus the
# two DPMS binaries the display_backend dispatcher tries in order
# (wlr-randr for Wayland kiosks, x11-xserver-utils → `xset` for X11
# kiosks). vcgencmd as a Pi-specific fallback isn't installed here
# because libraspberrypi-bin isn't available on the amd64 build leg;
# wlr-randr already covers the wlroots/labwc RPi setup and we don't
# need a third backend for now.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev \
    portaudio19-dev \
    libasound2-dev \
    alsa-utils \
    pulseaudio-utils \
    libpulse0 \
    libsndfile1 \
    wlr-randr \
    x11-xserver-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY audio_streamer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY audio_streamer/ ./audio_streamer/
COPY web/ ./web/

EXPOSE 8080

CMD ["python3", "-m", "audio_streamer.main"]
