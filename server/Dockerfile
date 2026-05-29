FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    gcc g++ git curl wget gnupg \
    libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install real Chromium from Debian repos (Ubuntu 22.04's is a snap stub)
RUN echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Chrome for MCP servers using nodriver/puppeteer
# Create a wrapper that always passes --no-sandbox (required when running as root)
RUN printf '#!/bin/sh\nexec /usr/bin/google-chrome-stable --no-sandbox --disable-gpu --disable-dev-shm-usage --headless=new "$@"\n' > /usr/bin/chromium \
    && chmod +x /usr/bin/chromium
ENV CHROMIUM_PATH=/usr/bin/chromium
ENV KINDLY_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV CHROME_PATH=/usr/bin/chromium

# Install uv/uvx for stdio-based MCP servers
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Pin numpy before the rest so resemblyzer/librosa resolve against it.
RUN pip3 install --no-cache-dir numpy==1.26.4
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/

# Bundled themes — get baked in so the registry has the built-in set
# available even before users mount their own. The compose bind mount
# overlays this directory at deploy time; the registry handles either.
COPY themes/ ./themes/

# Create directories for models
RUN mkdir -p /app/models

EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--ws-max-size", "16777216"]
