FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    gcc g++ git curl \
    libsndfile1 ffmpeg \
    chromium-browser \
    && rm -rf /var/lib/apt/lists/*

# Chromium for MCP servers using nodriver/puppeteer
ENV CHROMIUM_PATH=/usr/bin/chromium-browser
ENV KINDLY_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium-browser
# Required: Chromium won't run as root without --no-sandbox
ENV CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-dev-shm-usage"
ENV CHROME_FLAGS="--no-sandbox --disable-gpu --disable-dev-shm-usage"
ENV PUPPETEER_CHROMIUM_REVISION=skip
# nodriver reads this
ENV NODRIVER_BROWSER_ARGS="--no-sandbox,--disable-gpu,--disable-dev-shm-usage"

# Create a wrapper that always passes --no-sandbox
RUN mv /usr/bin/chromium-browser /usr/bin/chromium-browser-real \
    && printf '#!/bin/sh\nexec /usr/bin/chromium-browser-real --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"\n' > /usr/bin/chromium-browser \
    && chmod +x /usr/bin/chromium-browser

# Install uv/uvx for stdio-based MCP servers
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Python dependencies — pre-install build deps needed by NeMo for metadata generation
RUN pip3 install --no-cache-dir numpy==1.26.4 setuptools wheel Cython typing_extensions
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/

# Create directories for models
RUN mkdir -p /app/models

EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--ws-max-size", "16777216"]
