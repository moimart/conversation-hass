"""pal-llm-control: switches PAL + OpenClaw between the Ollama and vLLM backends.

A small internal service (no published port; reached by ai-server on the
compose network). It owns the lifecycle the ai-server container can't:

  backend=vllm   1. write llm/llm.env (context size, concurrency, ...)
                 2. unload Ollama's loaded models (frees their VRAM)
                 3. `compose up -d` the vLLM service (llm/docker-compose.llm.yml)
                 4. wait for vLLM /health (first start downloads the weights)
                 5. point OpenClaw at vllm/<served-name> (openclaw.json hot-applies)
  backend=ollama 1. point OpenClaw back at its previous Ollama model
                 2. `compose stop` vLLM
                 3. apply Ollama's concurrency (OLLAMA_NUM_PARALLEL) if it changed

It talks to the container engine only through the compose CLI and the
engine's API socket (DOCKER_HOST), so it works the same with Docker or
Podman. Host paths are mounted at the SAME paths inside the container so
compose files resolve exactly as on the host.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("pal.llm-control")

LLM_COMPOSE = os.environ.get("LLM_COMPOSE_FILE", "/home/moimart/conversation-hass/llm/docker-compose.llm.yml")
LLM_ENV = os.environ.get("LLM_ENV_FILE", "/home/moimart/conversation-hass/llm/llm.env")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
VLLM_URL = os.environ.get("VLLM_URL", "http://host.docker.internal:8000").rstrip("/")
OPENCLAW_CONFIG = os.environ.get("OPENCLAW_CONFIG", "/home/moimart/.openclaw/openclaw.json")
OLLAMA_COMPOSE_DIR = os.environ.get("OLLAMA_COMPOSE_DIR", "")   # e.g. /home/moimart/ai_storage
STATE_FILE = os.environ.get("LLM_CONTROL_STATE", "/state/llm-control.json")
VLLM_OPENCLAW_BASE = os.environ.get("VLLM_OPENCLAW_BASE_URL", "http://127.0.0.1:8000/v1")
HEALTH_TIMEOUT_S = float(os.environ.get("VLLM_HEALTH_TIMEOUT_S", "1500"))
MAX_CTX = 262144

DEFAULT_ENV = {
    "VLLM_MODEL": "Qwen/Qwen3.8-27B-FP8",
    "VLLM_SERVED_NAME": "qwen3.8-27b",
    "VLLM_MAX_MODEL_LEN": "131072",
    "VLLM_MAX_NUM_SEQS": "4",
    "VLLM_GPU_MEM_UTIL": "0.55",
    "VLLM_TOOL_PARSER": "qwen3_xml",
}


# --- small file helpers --------------------------------------------------------

def read_env(path: str) -> dict[str, str]:
    out = dict(DEFAULT_ENV)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def atomic_write(path: str, text: str, *, like: str | None = None, mode: int = 0o644) -> None:
    """Write via temp + rename, keeping the owner/mode of `like` (or the old
    file). This service runs as root; a root-owned 0600 openclaw.json would be
    unreadable for the gateway, which runs as the user."""
    ref = like or path
    try:
        st = os.stat(ref)
        uid, gid, mode = st.st_uid, st.st_gid, st.st_mode & 0o777
    except FileNotFoundError:
        try:
            st = os.stat(os.path.dirname(path) or ".")
            uid, gid = st.st_uid, st.st_gid
        except FileNotFoundError:
            uid = gid = None
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".llmctl.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        if uid is not None:
            try:
                os.chown(tmp, uid, gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_env(path: str, values: dict[str, str]) -> bool:
    """Write llm.env; returns True if anything changed."""
    if read_env(path) == values and os.path.exists(path):
        return False
    body = "# Written by pal-llm-control from the HA LLM entities. See llm.env.example.\n"
    body += "".join(f"{k}={v}\n" for k, v in values.items())
    atomic_write(path, body)
    return True


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    atomic_write(STATE_FILE, json.dumps(st, indent=1))


# --- engine / compose -----------------------------------------------------------

async def run(*args: str, timeout: float = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


def compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", LLM_COMPOSE, "--env-file", LLM_ENV, *args]


async def vllm_container_state() -> str:
    """'running' | 'exited' | 'created' | 'absent' ..."""
    rc, out = await run("docker", "inspect", "-f", "{{.State.Status}}", "pal-vllm", timeout=30)
    return out.strip() if rc == 0 else "absent"


async def vllm_logs_tail(n: int = 25) -> str:
    _, out = await run("docker", "logs", "--tail", str(n), "pal-vllm", timeout=30)
    return out[-3000:]


async def vllm_healthy(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{VLLM_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


# --- Ollama -------------------------------------------------------------------

async def ollama_unload_all(client: httpx.AsyncClient) -> list[str]:
    """Free Ollama's VRAM: unload every loaded model (keep_alive 0). They reload
    on demand when PAL/OpenClaw are back on Ollama."""
    try:
        r = await client.get(f"{OLLAMA_URL}/api/ps", timeout=5.0)
        models = [m.get("name") for m in (r.json() or {}).get("models", []) if m.get("name")]
    except Exception as e:
        log.warning(f"ollama ps failed: {e}")
        return []
    for m in models:
        try:
            await client.post(f"{OLLAMA_URL}/api/generate", json={"model": m, "keep_alive": 0}, timeout=30.0)
        except Exception as e:
            log.warning(f"ollama unload {m} failed: {e}")
    return models


async def ollama_apply_concurrency(n: int) -> str | None:
    """Set OLLAMA_NUM_PARALLEL via <ollama compose dir>/.env and recreate the
    container if it changed. The compose file must reference
    ${OLLAMA_NUM_PARALLEL}. Returns a note, or None when not configured."""
    if not OLLAMA_COMPOSE_DIR:
        return None
    env_path = os.path.join(OLLAMA_COMPOSE_DIR, ".env")
    current = {}
    try:
        with open(env_path) as f:
            current = dict(l.strip().split("=", 1) for l in f if "=" in l and not l.startswith("#"))
    except FileNotFoundError:
        pass
    if current.get("OLLAMA_NUM_PARALLEL") == str(n):
        return "unchanged"
    current["OLLAMA_NUM_PARALLEL"] = str(n)
    atomic_write(env_path, "".join(f"{k}={v}\n" for k, v in current.items()))
    rc, out = await run("docker", "compose", "--project-directory", OLLAMA_COMPOSE_DIR,
                        "up", "-d", "ollama", timeout=300)
    if rc != 0:
        raise RuntimeError(f"recreating ollama failed: {out[-400:]}")
    return f"OLLAMA_NUM_PARALLEL={n}"


# --- OpenClaw -----------------------------------------------------------------

def openclaw_patch(fn) -> bool:
    """Read-modify-write openclaw.json (the gateway hot-applies model and
    provider changes). Returns False when OpenClaw isn't configured here."""
    if not os.path.exists(OPENCLAW_CONFIG):
        return False
    with open(OPENCLAW_CONFIG) as f:
        cfg = json.load(f)
    if not fn(cfg):
        return True
    atomic_write(OPENCLAW_CONFIG, json.dumps(cfg, indent=2) + "\n")
    return True


def openclaw_to_vllm(served: str, ctx: int, state: dict) -> bool:
    def patch(cfg: dict) -> bool:
        providers = cfg.setdefault("models", {}).setdefault("providers", {})
        providers["vllm"] = {
            "baseUrl": VLLM_OPENCLAW_BASE,
            "apiKey": "vllm-local",
            "api": "openai-completions",
            "timeoutSeconds": 300,
            "models": [{
                "id": served, "name": f"{served} (vLLM)", "reasoning": True,
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": ctx, "maxTokens": 8192,
                "compat": {"supportsTools": True, "supportsUsageInStreaming": True},
            }],
        }
        agents = cfg.setdefault("agents", {}).setdefault("defaults", {})
        model = agents.get("model")
        if isinstance(model, str):
            model = {"primary": model}
        model = dict(model or {})
        primary = model.get("primary") or ""
        if not primary.startswith("vllm/"):
            state["openclaw_previous_primary"] = primary
        model["primary"] = f"vllm/{served}"
        agents["model"] = model
        return True
    return openclaw_patch(patch)


def openclaw_to_ollama(state: dict, fallback_model: str) -> bool:
    def patch(cfg: dict) -> bool:
        agents = cfg.setdefault("agents", {}).setdefault("defaults", {})
        model = agents.get("model")
        if isinstance(model, str):
            model = {"primary": model}
        model = dict(model or {})
        if not (model.get("primary") or "").startswith("vllm/"):
            return False                                # already on Ollama
        prev = state.get("openclaw_previous_primary") or (f"ollama/{fallback_model}" if fallback_model else "")
        if not prev:
            raise RuntimeError("no previous OpenClaw model to restore")
        model["primary"] = prev
        agents["model"] = model
        return True
    return openclaw_patch(patch)


# --- orchestration --------------------------------------------------------------

class ApplyRequest(BaseModel):
    backend: str                  # "ollama" | "vllm"
    num_ctx: int = 131072
    concurrency: int = 4
    ollama_model: str = ""        # OpenClaw fallback if no previous primary is remembered


class Controller:
    def __init__(self):
        self.state = load_state()
        self.phase = "ready"
        self.target = self.state.get("backend", "ollama")
        self.error: str | None = None
        self.detail = ""
        self._job: asyncio.Task | None = None
        self._http = httpx.AsyncClient()

    def status(self) -> dict:
        return {
            "backend": self.state.get("backend", "ollama"),
            "target": self.target,
            "phase": self.phase,
            "detail": self.detail,
            "error": self.error,
            "settings": self.state.get("settings", {}),
            "served_model": read_env(LLM_ENV)["VLLM_SERVED_NAME"],
        }

    def apply(self, req: ApplyRequest) -> None:
        if self._job and not self._job.done():
            self._job.cancel()
        self.target = req.backend
        self.error = None
        self._job = asyncio.create_task(self._run(req))

    def _set(self, phase: str, detail: str = "") -> None:
        self.phase, self.detail = phase, detail
        log.info(f"[{self.target}] {phase}: {detail}")

    async def _run(self, req: ApplyRequest) -> None:
        try:
            if req.backend == "vllm":
                await self._to_vllm(req)
            else:
                await self._to_ollama(req)
            self.state["backend"] = req.backend
            self.state["settings"] = {"num_ctx": req.num_ctx, "concurrency": req.concurrency}
            save_state(self.state)
            self._set("ready", f"{req.backend} active")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            self._set("error", str(e)[:300])
            log.exception("apply failed")

    async def _to_vllm(self, req: ApplyRequest) -> None:
        env = read_env(LLM_ENV)
        env["VLLM_MAX_MODEL_LEN"] = str(max(4096, min(MAX_CTX, int(req.num_ctx))))
        env["VLLM_MAX_NUM_SEQS"] = str(max(1, min(64, int(req.concurrency))))
        changed = write_env(LLM_ENV, env)
        running = await vllm_container_state() == "running"
        if running and not changed and await vllm_healthy(self._http):
            self._set("starting", "vLLM already running")
        else:
            self._set("starting", "unloading Ollama models")
            unloaded = await ollama_unload_all(self._http)
            self._set("starting", f"starting vLLM (unloaded {', '.join(unloaded) or 'nothing'})")
            rc, out = await run(*compose("up", "-d", *(["--force-recreate"] if changed else []), "vllm"),
                                timeout=1800)            # first run pulls a ~10 GB image
            if rc != 0:
                raise RuntimeError(f"compose up failed: {out[-500:]}")
            deadline = time.monotonic() + HEALTH_TIMEOUT_S
            while not await vllm_healthy(self._http):
                st = await vllm_container_state()
                if st not in ("running", "created", "restarting"):
                    raise RuntimeError(f"vLLM container {st}: {await vllm_logs_tail()}")
                if time.monotonic() > deadline:
                    raise RuntimeError("vLLM did not become healthy in time "
                                       f"(still loading?): {await vllm_logs_tail(8)}")
                self._set("starting", "vLLM loading the model")
                await asyncio.sleep(5)
        self._set("starting", "pointing OpenClaw at vLLM")
        openclaw_to_vllm(env["VLLM_SERVED_NAME"], int(env["VLLM_MAX_MODEL_LEN"]), self.state)
        save_state(self.state)

    async def _to_ollama(self, req: ApplyRequest) -> None:
        self._set("stopping", "pointing OpenClaw back at Ollama")
        openclaw_to_ollama(self.state, req.ollama_model)
        save_state(self.state)
        if await vllm_container_state() not in ("absent", "exited", "created"):
            self._set("stopping", "stopping vLLM")
            rc, out = await run(*compose("stop", "vllm"), timeout=300)
            if rc != 0:
                raise RuntimeError(f"compose stop failed: {out[-500:]}")
        note = await ollama_apply_concurrency(max(1, min(16, int(req.concurrency))))
        if note:
            self._set("stopping", note)


controller: Controller | None = None
app = FastAPI(title="pal-llm-control")


@app.on_event("startup")
async def _startup() -> None:
    global controller
    controller = Controller()
    log.info(f"llm-control ready (backend={controller.state.get('backend', 'ollama')})")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    st = controller.status()
    st["vllm"] = {"container": await vllm_container_state(),
                  "healthy": await vllm_healthy(controller._http)}
    return st


@app.post("/apply")
async def apply(req: ApplyRequest):
    if req.backend not in ("ollama", "vllm"):
        return {"error": "backend must be ollama or vllm"}
    controller.apply(req)
    return controller.status()
