# Self-contained: build context is this directory.
#   docker build -f stt-service/Dockerfile stt-service/
# (docker-compose.server.yml sets context: ./stt-service)
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    gcc g++ git \
    libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pre-install build deps NeMo needs for metadata generation
RUN pip3 install --no-cache-dir numpy==1.26.4 setuptools wheel Cython typing_extensions
COPY requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# ASR engines + the HTTP wrapper — both owned by this image (no cross-service COPY).
COPY engines.py ./engines.py
COPY app.py ./app.py

RUN mkdir -p /app/models

EXPOSE 8770

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8770"]
