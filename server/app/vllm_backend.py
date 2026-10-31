"""LLM backend switching (Ollama ⇄ vLLM) for PAL's local tool loop + OpenClaw.

Two pieces:

* VLLMClient — PAL's transport to vLLM's OpenAI-compatible server. It reuses
  the cloud override's message/tool-call translation (cloud_llm), so the
  conversation loop keeps speaking its Ollama-shaped messages.

* LLMBackendManager — drives the switch from the HA "LLM Backend" select.
  The container lifecycle (start/stop vLLM, unload Ollama, repoint OpenClaw)
  belongs to the pal-llm-control service; this manager asks it to apply the
  desired backend + settings and polls its status. PAL keeps answering on the
  CURRENT backend until the controller reports the new one ready, then flips
  conversation.llm_backend — so a switch never leaves voice turns without a
  model. A failed switch reverts the select and reports the error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from .cloud_llm import _from_openai_response, _to_openai_messages

log = logging.getLogger("hal.llm_backend")

BACKENDS = ("ollama", "vllm")
POLL_S = 3.0


class VLLMClient:
    def __init__(self, base_url: str, model: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = http or httpx.AsyncClient(timeout=180.0)

    async def chat(self, messages: list[dict], tools: list[dict] | None, *,
                   max_tokens: int, temperature: float = 0.7) -> tuple[dict, dict]:
        """One non-streaming completion. Returns (ollama-shaped response, usage)."""
        payload: dict = {
            "model": self.model,
            "messages": _to_openai_messages(messages),
            "stream": False,
            "temperature": temperature,
            # Qwen3.8 non-thinking sampling (model card).
            "top_p": 0.8,
            "top_k": 20,
            "max_tokens": max_tokens,
            # Voice turns: no hidden chain-of-thought (latency + it would eat the
            # small num_predict budget). The reasoning parser still strips any.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        r = await self._http.post(f"{self.base_url}/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        return _from_openai_response(data), (data.get("usage") or {})

    async def close(self) -> None:
        await self._http.aclose()


class LLMBackendManager:
    def __init__(self, state, control_url: str, vllm_url: str):
        self.state = state
        self.control_url = control_url.rstrip("/")
        self.vllm_url = vllm_url.rstrip("/")
        self.status: dict = {"phase": "unknown"}
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=10.0)

    @property
    def enabled(self) -> bool:
        return bool(self.control_url)

    def _settings(self) -> dict:
        cfg = self.state.runtime_config
        conv = self.state.conversation
        return {
            "num_ctx": int(getattr(conv, "num_ctx", 0) or cfg.get("num_ctx", 131072) or 131072),
            "concurrency": int(cfg.get("llm_concurrency", 4) or 4),
            "ollama_model": str(cfg.get("ollama_model", "") or ""),
        }

    async def apply(self, backend: str) -> None:
        """Ask the controller for `backend` with the current settings, then
        follow it to ready/error in the background."""
        if backend not in BACKENDS:
            raise ValueError(backend)
        if not self.enabled:
            log.warning("LLM backend switch unavailable (LLM_CONTROL_URL unset)")
            await self._publish("unavailable", "llm-control not configured")
            return
        body = {"backend": backend, **self._settings()}
        try:
            r = await self._http.post(f"{self.control_url}/apply", json=body)
            r.raise_for_status()
        except Exception as e:
            log.error(f"llm-control apply failed: {e}")
            await self._publish("error", f"llm-control unreachable: {e}")
            await self._revert()
            return
        log.info(f"LLM backend → {backend} requested ({body})")
        await self._publish("starting" if backend == "vllm" else "stopping", f"switching to {backend}")
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._follow(backend))

    async def _follow(self, backend: str) -> None:
        last = None
        while True:
            await asyncio.sleep(POLL_S)
            try:
                st = (await self._http.get(f"{self.control_url}/status")).json()
            except Exception as e:
                log.debug(f"llm-control status failed: {e}")
                continue
            self.status = st
            phase, detail = st.get("phase", "?"), st.get("detail", "")
            if (phase, detail) != last:
                last = (phase, detail)
                await self._publish(phase, detail)
            if st.get("target") != backend:
                return                       # superseded by a newer apply
            if phase == "ready":
                self._activate(backend, st)
                await self._publish("ready", f"{backend} active")
                return
            if phase == "error":
                log.error(f"LLM backend switch to {backend} failed: {st.get('error')}")
                await self._revert()
                return

    def _activate(self, backend: str, st: dict) -> None:
        conv = self.state.conversation
        if conv is None:
            return
        if backend == "vllm":
            served = st.get("served_model") or "qwen3.8-27b"
            old = getattr(conv, "vllm", None)
            if old is None or old.model != served or old.base_url != self.vllm_url:
                conv.vllm = VLLMClient(self.vllm_url, served)
        conv.llm_backend = backend
        log.info(f"PAL now answers on {backend}")

    async def _revert(self) -> None:
        """Snap the HA select back to the backend PAL actually uses."""
        conv = self.state.conversation
        actual = getattr(conv, "llm_backend", "ollama") if conv else "ollama"
        if self.state.runtime_config is not None:
            self.state.runtime_config.set("llm_backend", actual)
        bridge = getattr(self.state, "mqtt_bridge", None)
        if bridge is not None:
            try:
                await bridge.publish_config("llm_backend", actual)
            except Exception:
                pass

    async def _publish(self, phase: str, detail: str = "") -> None:
        self.status = {**self.status, "phase": phase, "detail": detail}
        bridge = getattr(self.state, "mqtt_bridge", None)
        if bridge is not None and hasattr(bridge, "publish_llm_backend_status"):
            try:
                await bridge.publish_llm_backend_status(phase, detail)
            except Exception as e:
                log.debug(f"llm backend status publish failed: {e}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        await self._http.aclose()


def manager_from_env(state) -> LLMBackendManager:
    return LLMBackendManager(
        state,
        control_url=os.environ.get("LLM_CONTROL_URL", "").strip(),
        vllm_url=os.environ.get("VLLM_URL", "http://host.docker.internal:8000").strip(),
    )
