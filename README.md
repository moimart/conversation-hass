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
│  │  │  Anker Powerconf S330  │  │   mono audio │  │  ┌────────┐  ┌───────────┐  │    │  │
│  │  │  (48kHz stereo → 16kHz │  │              │  │  │Silero  │  │ STT Engine│  │    │  │
│  │  │   mono, FIR filtered)  │  │              │  │  │VAD     │─►│ Whisper / │  │    │  │
│  │  │  ┌──────┐  ┌───────┐  │◄─┼──────────────┼──│  │(voice  │  │ Nemotron  │  │    │  │
│  │  │  │ Mic  │  │Speaker│  │  │  WAV audio   │  │  │activity│  │           │  │    │  │
│  │  │  └──────┘  └───────┘  │  │  + JSON msgs │  │  └────────┘  └─────┬─────┘  │    │  │
│  │  └────────────────────────┘  │              │  │                    │         │    │  │
│  │                              │              │  │  ┌────────────────▼──────┐  │    │  │
│  │  ┌────────────────────────┐  │              │  │  │  Speaker Filter       │  │    │  │
│  │  │   Web UI  (:8080)      │  │  transcript  │  │  │  (resemblyzer)        │  │    │  │
│  │  │                        │◄─┼──────────────┼──│  │  human vs AI voice    │  │    │  │
│  │  │  HAL 9000 Eye          │  │  + state     │  │  └──────────┬────────────┘  │    │  │
│  │  │  (metallic bezel,      │  │  + wake flash│  │             │               │    │  │
│  │  │   white-hot speaking)  │  │              │  │  ┌──────────▼────────────┐  │    │  │
│  │  │  Live Transcription    │  │              │  │  │ Conversation Manager   │  │    │  │
│  │  │  Response Display      │  │              │  │  │                        │  │    │  │
│  │  └────────────────────────┘  │              │  │  │  Wake word + chime     │  │    │  │
│  │                              │              │  │  │  (or always-on mode)   │  │    │  │
│  └──────────────────────────────┘              │  │  │  Command accumulation  │  │    │  │
│                                                │  │  │  Follow-up window      │  │    │  │
│                                                │  │  └───┬──────────────┬─────┘  │    │  │
│                                                │  │      │              │        │    │  │
│                                                │  │  ┌───▼───┐   ┌─────▼──────┐ │    │  │
│                                                │  │  │Ollama │   │ Wyoming    │ │    │  │
│                                                │  │  │LLM    │   │ TTS        │ │    │  │
│                                                │  │  │(tool  │   │            │ │    │  │
│                                                │  │  │calling│   │            │ │    │  │
│                                                │  │  └───┬───┘   └────────────┘ │    │  │
│                                                │  │      │ MCP tool calls       │    │  │
│                                                │  │  ┌───▼──────────────────┐   │    │  │
│                                                │  │  │ MCP Client           │   │    │  │
│                                                │  │  │ (Home Assistant)     │───┼────┼──► HA MCP Server
│                                                │  │  └──────────────────────┘   │    │  │
│                                                │  └──────────────────────────────┘    │  │
│                                                └──────────────────────────────────────┘  │
│                                                                                         │
│  External services (pre-existing, not managed by this project):                         │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                             │
│  │ Ollama (:11434)│  │ Wyoming TTS    │  │ HA MCP Server  │                             │
│  │ LLM inference  │  │ Speech synth   │  │ Home Assistant │                             │
│  └────────────────┘  └────────────────┘  └────────────────┘                             │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Continuous listening** — The Raspberry Pi captures audio from the Anker Powerconf S330 USB speakerphone (48kHz stereo, FIR anti-alias filtered and downmixed to 16kHz mono) and streams raw PCM audio over WebSocket to the AI server.

2. **Always-on transcription** — The AI server runs Silero VAD (voice activity detection) and a configurable STT engine to transcribe everything said in the room. All transcriptions appear live on the web UI regardless of whether a command was issued.

3. **Speaker identification** — Resemblyzer voice embeddings distinguish human speakers from the AI's own TTS output, preventing the assistant from responding to itself.

4. **Wake word or always-on** — Two modes:
   - **Wake word mode** (default): Transcribed text is monitored for the wake word (configurable). Only after detecting it does the system engage the LLM. A two-tone chime plays and the HAL eye flashes white as confirmation. A 10-second follow-up window allows natural back-and-forth conversation without repeating the wake word.
   - **Always-on mode**: Set `WAKE_WORD=` (empty) to process every transcribed line through the LLM automatically.

5. **LLM with MCP tool calling** — The user's command is sent to Ollama (32k context window) with MCP tool definitions discovered from your Home Assistant MCP server at startup (88+ tools). The LLM searches for entities by friendly name, then calls HA services with the correct entity IDs. If the command is conversational, the LLM simply responds naturally. The system prompt is loaded from `server/system_prompt.txt` — edit it to customize HAL's personality.

6. **Local TTS** — The response is synthesized via a Wyoming-protocol TTS service and streamed back to the Raspberry Pi for playback through the speaker (resampled to the device's native 48kHz).

7. **Web UI** — A modern HAL 9000-inspired interface with metallic bezel ring, animated red eye (bright white-hot center when speaking), live transcription, AI responses, and assistant state indicators.

## Speech-to-Text Engines

Two STT engines are available, selectable at runtime via the `STT_ENGINE` environment variable:

| Engine | Models | Speed | Best for |
|---|---|---|---|
| `whisper` (default) | `large-v3-turbo`, `large-v3`, `medium`, `base` | Baseline | Accuracy, multilingual, noisy environments |
| `nemotron` | `nvidia/parakeet-tdt-0.6b-v2`, `nvidia/parakeet-ctc-1.1b` | ~21x faster | Real-time streaming, low latency |

```ini
# .env — switch to Nemotron
STT_ENGINE=nemotron
STT_MODEL=nvidia/parakeet-tdt-0.6b-v2
```

Both engines are included in the Docker image and share the same interface. Models are cached in a Docker volume across restarts.

## Prerequisites

| Component | Requirement |
|---|---|
| AI Server | Linux machine with NVIDIA GPU, Docker with nvidia-container-toolkit |
| Raspberry Pi | Raspberry Pi 4/5 with Docker, USB speakerphone (e.g., Anker Powerconf S330) |
| Ollama | Running on the AI server host with a tool-calling model (e.g., llama3.2) |
| Home Assistant | Running instance with a configured MCP server exposed over HTTP(S) |
| Wyoming TTS | A Wyoming-protocol TTS service (e.g., [wyoming-piper](https://github.com/rhasspy/wyoming-piper), whisper-tts) |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/moimart/conversation-hass.git
cd conversation-hass
cp .env.example .env
```

Edit `.env` with your network addresses:

```ini
AI_SERVER_HOST=10.20.30.185       # IP of your GPU server
RPI_HOST=10.20.30.180             # IP of your Raspberry Pi
MCP_SERVER_URL=https://your-ha-mcp-server/endpoint
WAKE_WORD=hey homie               # leave empty for always-on mode
OLLAMA_HOST=http://10.20.30.185:11434
OLLAMA_MODEL=gpt-oss:20b
WYOMING_TTS_HOST=10.20.30.185
WYOMING_TTS_PORT=10300
STT_ENGINE=whisper                 # or "nemotron"
```

### 2. Start the AI server

**Option A — Build locally:**
```bash
docker compose -f docker-compose.server.yml up --build -d
```

**Option B — Use pre-built image from GHCR:**
```bash
docker compose -f docker-compose.server-ghcr.yml up -d
```

This starts `hal-ai-server` — FastAPI WebSocket server on port **8765**. Ollama must already be running on the host.

STT models are cached in a Docker volume (`huggingface-cache`) so they're only downloaded once.

### 3. Start the Raspberry Pi

**Option A — Build locally:**
```bash
docker compose -f docker-compose.rpi.yml up --build -d
```

**Option B — Use pre-built arm64 image from GHCR:**
```bash
docker compose -f docker-compose.rpi-ghcr.yml up -d
```

The container auto-detects the USB speakerphone, probes its native sample rate and channel count, and resamples/downmixes to 16kHz mono with FIR anti-aliasing for the AI server. TTS playback is upsampled to the device's native rate.

### 4. Open the web UI

Navigate to `http://<rpi-ip>:8080` in a browser.

## Pre-built Images

| Image | Platform | Registry |
|---|---|---|
| `ghcr.io/moimart/conversation-hass/hal-rpi:latest` | linux/arm64 | GHCR |
| `ghcr.io/moimart/conversation-hass/hal-ai-server:latest` | linux/amd64 | GHCR |

## Project Structure

```
conversation-hass/
├── docker-compose.server.yml          # AI server (build locally)
├── docker-compose.server-ghcr.yml     # AI server (pre-built GHCR image)
├── docker-compose.rpi.yml             # RPi (build locally)
├── docker-compose.rpi-ghcr.yml        # RPi (pre-built GHCR image)
├── .env.example                       # Configuration template
├── setup.sh                           # Interactive setup script
│
├── server/
│   ├── Dockerfile                     # NVIDIA CUDA 12.4 + NeMo + Whisper
│   ├── requirements.txt               # faster-whisper, nemo_toolkit, silero-vad, mcp
│   ├── system_prompt.txt              # LLM personality (editable, mounted read-only)
│   └── app/
│       ├── main.py                    # FastAPI server, WebSocket endpoints, wake chime
│       ├── audio_pipeline.py          # VAD + STT engine selection + speaker filter
│       ├── transcriber.py             # WhisperTranscriber + NemotronTranscriber
│       ├── speaker_filter.py          # Voice embedding comparison (resemblyzer)
│       ├── conversation.py            # Wake word / always-on, MCP tool-calling, 32k context
│       ├── mcp_client.py              # MCP client (Streamable HTTP transport)
│       └── tts.py                     # Wyoming protocol TTS client
│
├── rpi/
│   ├── Dockerfile                     # Python 3.11 slim + PulseAudio + ALSA
│   ├── audio_streamer/
│   │   └── main.py                    # Mic capture, FIR resample, TTS playback
│   └── web/
│       ├── index.html                 # HAL 9000 UI (metallic bezel)
│       ├── style.css                  # Animations, wake flash, white-hot speaking eye
│       └── app.js                     # WebSocket client, live transcription
│
└── tests/                             # 92 pytest tests (all mocked, no GPU needed)
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
| `MCP_SERVER_URL` | — | Full URL of your Home Assistant MCP server |
| `WAKE_WORD` | `hey homie` | Phrase that activates command processing. Empty = always-on |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.2` | LLM model name (must support tool calling) |
| `STT_ENGINE` | `whisper` | Speech-to-text engine: `whisper` or `nemotron` |
| `STT_MODEL` | (auto) | STT model name/size. Defaults: whisper=`large-v3-turbo`, nemotron=`nvidia/parakeet-tdt-0.6b-v2` |
| `WYOMING_TTS_HOST` | — | Hostname/IP of Wyoming TTS service |
| `WYOMING_TTS_PORT` | `10200` | Port of Wyoming TTS service |
| `AUDIO_DEVICE` | `default` | ALSA audio device (auto-detects USB speakerphones) |
| `SAMPLE_RATE` | `16000` | Target audio sample rate in Hz (device is auto-probed) |
| `CHANNELS` | `1` | Target audio channels (device channels auto-detected, downmixed) |
| `CHUNK_SIZE` | `4096` | Audio buffer size per WebSocket frame |
| `WEB_PORT` | `8080` | Port for the web UI on the RPi |

## Customizing the System Prompt

Edit `server/system_prompt.txt` to change HAL's personality. The file is mounted read-only into the container — no rebuild needed, just restart. MCP tool definitions are passed to Ollama separately via its native tool-calling API.

## Running Tests

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements-test.txt
pytest tests/ -v
```

All 92 tests run without GPU, ML models, or external services — dependencies are fully mocked.

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
{"type": "wake"}                             // Wake word detected (triggers UI flash)
{"type": "chime_start", "size": 13230}       // Wake chime audio incoming
{"type": "chime_end"}                        // Wake chime complete
{"type": "tts_start", "size": 48000}         // TTS audio incoming
{"type": "tts_end"}                          // TTS audio complete
{"type": "state", "state": "listening", "wake_word": "hey homie"}
```

**Client → Server:**
```jsonc
// Binary: raw PCM 16-bit LE audio chunks (16kHz mono)
{"type": "tts_finished"}                     // RPi finished playing audio
{"type": "ping"}                             // Keepalive
```

## License

MIT
