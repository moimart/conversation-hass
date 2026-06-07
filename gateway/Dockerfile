# Satellite gateway: a tiny aiohttp reverse proxy. No ML, no browser, no
# secrets — just enough to allowlist + token-gate the satellite surface.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway/gateway.py .

# Non-root: the gateway needs no privileges.
RUN useradd -r -u 10001 gateway
USER gateway

EXPOSE 8766
CMD ["python", "gateway.py"]
