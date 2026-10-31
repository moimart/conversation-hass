# pal-llm-control: switches PAL + OpenClaw between Ollama and vLLM (app.py).
# docker:cli carries the docker CLI + compose plugin; they talk to whatever
# engine socket is mounted (Docker, or Podman's compatible API socket).
FROM docker:28-cli
RUN apk add --no-cache python3 py3-pip curl \
 && pip install --no-cache-dir --break-system-packages fastapi==0.115.6 uvicorn==0.34.0 httpx==0.28.1
WORKDIR /app
COPY app.py /app/app.py
EXPOSE 8790
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8790"]
