#!/usr/bin/env bash
set -euo pipefail

# HAL Voice Assistant — Setup Script
# Run this on each machine after cloning the repo.

echo "=== HAL Voice Assistant Setup ==="
echo ""

# Check for .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[!] Created .env from .env.example"
    echo "    Edit .env with your configuration before starting."
    echo ""
fi

# Detect which node this is
echo "Which node is this?"
echo "  1) AI Server (GPU machine running Ollama)"
echo "  2) Raspberry Pi (audio capture + web UI)"
read -rp "Select [1/2]: " NODE

case "$NODE" in
    1)
        echo ""
        echo "=== Setting up AI Server ==="

        # Check for Docker + NVIDIA runtime
        if ! command -v docker &>/dev/null; then
            echo "[!] Docker not found. Please install Docker first."
            exit 1
        fi

        if ! docker info 2>/dev/null | grep -q "nvidia"; then
            echo "[!] NVIDIA Docker runtime not detected."
            echo "    Install nvidia-container-toolkit for GPU support."
            echo "    The server will fall back to CPU if GPU is unavailable."
        fi

        # Pull Ollama model
        echo ""
        read -rp "Pull Ollama model? (y/n): " PULL_MODEL
        if [ "$PULL_MODEL" = "y" ]; then
            MODEL=$(grep OLLAMA_MODEL .env | cut -d= -f2)
            MODEL=${MODEL:-llama3.2}
            echo "Pulling $MODEL..."
            docker run --rm -v ollama-data:/root/.ollama ollama/ollama pull "$MODEL"
        fi

        echo ""
        echo "Starting AI server..."
        docker compose -f docker-compose.server.yml up --build -d
        echo ""
        echo "=== AI Server is running ==="
        echo "  WebSocket endpoint: ws://$(hostname -I | awk '{print $1}'):8765"
        echo "  Health check:       http://$(hostname -I | awk '{print $1}'):8765/health"
        ;;

    2)
        echo ""
        echo "=== Setting up Raspberry Pi ==="

        if ! command -v docker &>/dev/null; then
            echo "[!] Docker not found. Please install Docker first."
            exit 1
        fi

        # Check audio device
        echo ""
        echo "Available audio devices:"
        arecord -l 2>/dev/null || echo "  (arecord not available — devices will be detected in container)"

        echo ""
        echo "Starting RPi services..."
        docker compose -f docker-compose.rpi.yml up --build -d
        echo ""
        echo "=== Raspberry Pi is running ==="
        echo "  Web UI: http://$(hostname -I | awk '{print $1}'):${WEB_PORT:-8080}"
        ;;

    *)
        echo "Invalid selection."
        exit 1
        ;;
esac

echo ""
echo "Done! View logs with:"
echo "  docker compose -f docker-compose.{server,rpi}.yml logs -f"
