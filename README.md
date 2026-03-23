# HAL — Local Voice Assistant for Home Assistant

A fully local, always-listening voice assistant that controls your smart home through natural conversation. Runs on two nodes: a **Raspberry Pi** for audio I/O and a sleek web UI, and an **AI server** with a GPU for speech recognition, language understanding, and speech synthesis.

No cloud services. No subscriptions. All processing stays on your network.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                      LOCAL NETWORK                                      │
│                                                                                         │
│  ┌──────────────────────────────┐              ┌──────────────────────────────────────┐  │
│  │       RASPBERRY PI           │   WebSocket  │           AI SERVER (GPU)            │  │
│  │                              │    :8765     │                                      │  │
│  │  ┌────────────────────────┐  │              │  ┌──────────────────────────────┐    │  │
│  │  │   Audio Streamer       │  │  PCM 16-bit  │  │       FastAPI Server          │    │  │
│  │  │                        │──┼──────────────┼─►│                              │    │  │
│  │  │  Anker Powerconf S330  │  │   audio      │  │  ┌────────┐  ┌───────────┐  │    │  │
│  │  │  ┌──────┐  ┌───────┐  │  │              │  │  │Silero  │  │ faster-   │  │    │  │
│  │  │  │ Mic  │  │Speaker│  │  │              │  │  │VAD     │─►│ whisper   │  │    │  │
│  │  │  │Capture│  │Playback│  │◄─┼──────────────┼──│  │(voice  │  │(speech-to │  │    │  │
│  │  │  └──────┘  └───────┘  │  │  WAV audio   │  │  │activity│  │ -text)    │  │    │  │
│  │  └────────────────────────┘  │  + JSON msgs │  │  └────────┘  └─────┬─────┘  │    │  │
│  │                              │              │  │                    │         │    │  │
│  │  ┌────────────────────────┐  │              │  │  ┌────────────────▼──────┐  │    │  │
│  │  │   Web UI  (:8080)      │  │  transcript  │  │  │  Speaker Filter       │  │    │  │
│  │  │                        │◄─┼──────────────┼──│  │  (resemblyzer)        │  │    │  │
│  │  │  HAL 9000 Eye          │  │   + state    │  │  │  human vs AI voice    │  │    │  │
│  │  │  Live Transcription    │  │              │  │  └──────────┬────────────┘  │    │  │
│  │  │  Response Display      │  │              │  │             │               │    │  │
│  │  └────────────────────────┘  │              │  │  ┌──────────▼────────────┐  │    │  │
│  │                              │              │  │  │ Conversation Manager   │  │    │  │
│  └──────────────────────────────┘              │  │  │                        │  │    │  │
│                                                │  │  │  Wake word detection   │  │    │  │
│                                                │  │  │  Command accumulation  │  │    │  │
│                                                │  │  │  Follow-up window      │  │    │  │
│                                                │  │  └───┬──────────────┬─────┘  │    │  │
│                                                │  │      │              │        │    │  │
│                                                │  │  ┌───▼───┐   ┌─────▼──────┐ │    │  │
│                                                │  │  │Ollama │   │ Wyoming    │ │    │  │
│                                                │  │  │LLM    │   │ TTS        │ │    │  │
│                                                │  │  │       │   │ (Whisper)  │ │    │  │
│                                                │  │  └───┬───┘   └────────────┘ │    │  │
│                                                │  │      │ tool calls           │    │  │
│                                                │  │  ┌───▼──────────────────┐   │    │  │
│                                                │  │  │ MCP Client           │   │    │  │
│                                                │  │  │ (Home Assistant)     │───┼────┼──┼──► HA MCP Server
│                                                │  │  └──────────────────────┘   │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                │                                      │  │
│                                                │  ┌──────────────────────────────┐    │  │
│                                                │  │  Ollama Container (:11434)   │    │  │
│                                                │  │  LLM model (e.g. gpt-oss)   │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Continuous listening** — The Raspberry Pi captures audio from the Anker Powerconf S330 USB speakerphone and streams raw PCM audio over WebSocket to the AI server.

2. **Always-on transcription** — The AI server runs Silero VAD (voice activity detection) and faster-whisper to transcribe everything said in the room. All transcriptions appear live on the web UI regardless of whether a command was issued.

3. **Speaker identification** — Resemblyzer voice embeddings distinguish human speakers from the AI's own TTS output, preventing the assistant from responding to itself.

4. **Wake word activation** — Transcribed text is monitored for the wake word (default: `"hey homie"`). Only after detecting it does the system engage the LLM. A 10-second follow-up window allows natural back-and-forth conversation without repeating the wake word.

5. **LLM with tool calling** — The user's command is sent to Ollama with MCP tool definitions from your Home Assistant server. The LLM can call HA tools (turn on lights, set thermostat, etc.) and receives their results before generating a spoken response.

6. **Local TTS** — The response is synthesized via a Wyoming-protocol TTS service and streamed back to the Raspberry Pi for playback through the speaker.

7. **Web UI** — A modern HAL 9000-inspired interface shows live transcription, AI responses, and assistant state (idle, listening, processing, speaking) with animated visual feedback.

## Prerequisites

| Component | Requirement |
|---|---|
| AI Server | Linux machine with NVIDIA GPU, Docker with nvidia-container-toolkit |
| Raspberry Pi | Raspberry Pi 4/5 with Docker, Anker Powerconf S330 connected via USB |
| Home Assistant | Running instance with a configured [MCP server](https://www.home-assistant.io/) exposed over HTTP(S) |
| Wyoming TTS | A Wyoming-protocol TTS service (e.g., [wyoming-piper](https://github.com/rhasspy/wyoming-piper), whisper-tts) |
| Ollama | Pre-installed on the AI server, or use the bundled container |

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url> conversation-hass
cd conversation-hass
cp .env.example .env
```

Edit `.env` with your network addresses and tokens:

```ini
AI_SERVER_HOST=10.20.30.185       # IP of your GPU server
RPI_HOST=10.20.30.180             # IP of your Raspberry Pi
MCP_SERVER_URL=https://your-ha-mcp-server/endpoint
WAKE_WORD=hey homie
OLLAMA_HOST=http://10.20.30.185:11434
OLLAMA_MODEL=gpt-oss:20b
WYOMING_TTS_HOST=10.20.30.185
WYOMING_TTS_PORT=10300
```

### 2. Start the AI server

```bash
# On the GPU machine
docker compose -f docker-compose.server.yml up --build -d
```

This starts two containers:
- `hal-ai-server` — FastAPI WebSocket server on port **8765**
- `hal-ollama` — Ollama LLM server on port **11434** (skip if you already run Ollama)

### 3. Start the Raspberry Pi

```bash
# On the Raspberry Pi
docker compose -f docker-compose.rpi.yml up --build -d
```

Or build the arm64 image from another machine and transfer it:

```bash
# On a build machine with Docker buildx
docker buildx build --platform linux/arm64 -t hal-rpi:latest -f rpi/Dockerfile --load rpi/
docker save hal-rpi:latest | gzip > hal-rpi-arm64.tar.gz

# On the Raspberry Pi
docker load < hal-rpi-arm64.tar.gz
```

### 4. Open the web UI

Navigate to `http://<rpi-ip>:8080` in a browser.

### Alternative: interactive setup

```bash
./setup.sh
```

The script detects the node type, validates prerequisites, and starts the appropriate containers.

## Project Structure

```
conversation-hass/
├── docker-compose.server.yml     # AI server: FastAPI + Ollama
├── docker-compose.rpi.yml        # RPi: audio streamer + web UI
├── .env.example                  # Configuration template
├── setup.sh                      # Interactive setup script
│
├── server/
│   ├── Dockerfile                # NVIDIA CUDA base image
│   ├── requirements.txt          # faster-whisper, mcp, httpx, etc.
│   └── app/
│       ├── main.py               # FastAPI server, WebSocket endpoints
│       ├── audio_pipeline.py     # VAD + transcription + speaker filter
│       ├── transcriber.py        # faster-whisper STT wrapper
│       ├── speaker_filter.py     # Voice embedding comparison
│       ├── conversation.py       # Wake word, LLM tool-calling loop
│       ├── mcp_client.py         # MCP client for Home Assistant
│       └── tts.py                # Wyoming protocol TTS client
│
├── rpi/
│   ├── Dockerfile                # Python slim image for arm64
│   ├── audio_streamer/
│   │   └── main.py               # Mic capture, TTS playback, WebSocket
│   └── web/
│       ├── index.html            # HAL 9000 UI
│       ├── style.css             # Animations, state-driven styles
│       └── app.js                # WebSocket client, live transcription
│
└── tests/                        # 90 pytest tests (all mocked, no GPU needed)
    ├── test_mcp_client.py
    ├── test_tts.py
    ├── test_conversation.py
    ├── test_audio_pipeline.py
    ├── test_speaker_filter.py
    └── test_transcriber.py
```

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `AI_SERVER_HOST` | — | IP address of the AI server (used by RPi to connect) |
| `RPI_HOST` | — | IP address of the Raspberry Pi |
| `MCP_SERVER_URL` | — | Full URL of your Home Assistant MCP server |
| `WAKE_WORD` | `hey homie` | Phrase that activates command processing |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.2` | LLM model name for Ollama |
| `WYOMING_TTS_HOST` | — | Hostname/IP of Wyoming TTS service |
| `WYOMING_TTS_PORT` | `10200` | Port of Wyoming TTS service |
| `AUDIO_DEVICE` | `default` | ALSA audio device (auto-detects Anker) |
| `SAMPLE_RATE` | `16000` | Audio sample rate in Hz |
| `CHANNELS` | `1` | Audio channels (mono) |
| `CHUNK_SIZE` | `4096` | Audio buffer size per WebSocket frame |
| `WEB_PORT` | `8080` | Port for the web UI on the RPi |

## Running Tests

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements-test.txt
pytest tests/ -v
```

All 90 tests run without GPU, ML models, or external services — dependencies are fully mocked.

## API Endpoints

### AI Server (`hal-ai-server:8765`)

| Endpoint | Protocol | Description |
|---|---|---|
| `/ws/audio` | WebSocket | Main audio channel. Receives PCM audio, sends transcription JSON + TTS binary |
| `/ws/ui` | WebSocket | UI channel. Receives live transcription and state updates |
| `/health` | HTTP GET | Health check: pipeline, MCP, and TTS status |

### WebSocket Message Types

**Server → Client:**
```jsonc
{"type": "transcription", "text": "...", "is_partial": false, "speaker": "human"}
{"type": "response", "text": "..."}          // LLM response text
{"type": "tts_start", "size": 48000}         // TTS audio incoming
{"type": "tts_end"}                          // TTS audio complete
{"type": "state", "state": "listening", "wake_word": "hey homie"}
```

**Client → Server:**
```jsonc
// Binary: raw PCM 16-bit LE audio chunks
{"type": "tts_finished"}                     // RPi finished playing audio
{"type": "ping"}                             // Keepalive
```

## License

MIT
