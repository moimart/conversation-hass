"""Cloud LLM override: OpenAI-compatible chat client for remote providers.

When the "Cloud Override" switch is ON, the conversation engine routes the
whole tool loop to a cloud provider (OpenAI, Anthropic via its OpenAI-compat
layer, DeepSeek, Kimi, any /v1-compatible endpoint) instead of local Ollama —
skipping the router model and OpenClaw entirely. Only the transport changes;
tools, persona, history, memory, and satellite routing are untouched.

SECRETS: providers + API keys live ONLY in a hot-reloadable JSON file
(default /app/runtime/cloud_providers.json — the runtime dir is rw-mounted
and gitignored), with OPENAI_API_KEY / ANTHROPIC_API_KEY env vars as a
fallback. Keys are never logged, never published to MQTT, never returned by
any API. The file is re-read on mtime change, so adding/rotating a provider
needs no restart:

    {"providers": [
        {"name": "openai", "base_url": "https://api.openai.com/v1",
         "api_key": "sk-..."},
        ...
    ]}

The adapter translates between the Ollama message/response shapes the
conversation loop already speaks and the OpenAI wire format. The only tricky
part is tool calling: this codebase's `role:"tool"` results carry no
tool_call_id (Ollama doesn't need one), while OpenAI requires it — results
are paired to the preceding assistant tool_calls POSITIONALLY (the loop in
`_run_llm_with_tools` appends exactly one result per call, in order), and the
provider-issued ids are stashed on the returned message under a private key
so subsequent rounds replay the exact ids strict providers demand.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger("hal.cloud_llm")

DEFAULT_PROVIDERS_PATH = "/app/runtime/cloud_providers.json"

# Private key used to stash provider tool_call ids inside the Ollama-shaped
# assistant message (the conversation loop appends that dict verbatim to the
# running messages list, carrying the stash into the next round).
_ID_STASH = "_openai_tool_call_ids"

# Env-var fallbacks: present key -> auto-created provider profile.
_ENV_PROVIDERS = [
    ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1"),
    ("anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1"),
]


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str  # NEVER log or serialize this

    def __repr__(self) -> str:  # defensive: keep keys out of any repr/log
        return f"Provider(name={self.name!r}, base_url={self.base_url!r}, api_key=***)"


class ProviderRegistry:
    """Hot-reloadable provider registry (file wins over env on name collision)."""

    def __init__(self, path: str = DEFAULT_PROVIDERS_PATH):
        self.path = path
        self._mtime: float | None = None
        self._providers: list[Provider] = []
        self._load_if_changed(force=True)

    def _load_if_changed(self, force: bool = False) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime

        file_providers: dict[str, Provider] = {}
        if mtime is not None:
            try:
                with open(self.path) as f:
                    data = json.load(f)
                for p in (data.get("providers") or []):
                    name = str(p.get("name", "")).strip()
                    base = str(p.get("base_url", "")).strip().rstrip("/")
                    key = str(p.get("api_key", "")).strip()
                    if name and base and key and key != "REPLACE_ME":
                        file_providers[name] = Provider(name, base, key)
            except Exception as e:
                # Never include file contents (could hold key bytes).
                log.warning(f"cloud providers file unreadable ({type(e).__name__}) — using env fallback only")

        merged: dict[str, Provider] = {}
        for name, env_var, base in _ENV_PROVIDERS:
            key = os.environ.get(env_var, "").strip()
            if key:
                merged[name] = Provider(name, base, key)
        merged.update(file_providers)  # file wins
        self._providers = list(merged.values())

    def providers(self) -> list[Provider]:
        self._load_if_changed()
        return list(self._providers)

    def get(self, name: str) -> Provider | None:
        self._load_if_changed()
        for p in self._providers:
            if p.name == name:
                return p
        return None


def _is_anthropic(base_url: str) -> bool:
    return "anthropic" in base_url


def _auth_headers(p: Provider, *, for_models: bool = False) -> dict:
    """Anthropic's native /v1/models wants x-api-key + anthropic-version; its
    OpenAI-compat /chat/completions accepts Bearer. Everyone else: Bearer."""
    if _is_anthropic(p.base_url) and for_models:
        return {"x-api-key": p.api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {p.api_key}"}


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate the Ollama-shaped running messages list to OpenAI wire shape.

    role:"tool" entries (no id in this codebase) pair POSITIONALLY with the
    preceding assistant message's tool_calls — `_run_llm_with_tools` appends
    exactly one result per call in iteration order, so the mapping is exact.
    """
    out: list[dict] = []
    pending_ids: list[str] = []
    tool_cursor = 0
    for idx, m in enumerate(messages):
        role = m.get("role", "user")
        if role in ("system", "user"):
            out.append({"role": role, "content": m.get("content") or ""})
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                ids = m.get(_ID_STASH)
                if not ids or len(ids) != len(tcs):
                    # Cross-engine history (e.g. an Ollama tool round before the
                    # override was enabled): synthesize stable ids.
                    ids = [f"call_{idx}_{i}" for i in range(len(tcs))]
                pending_ids = list(ids)
                tool_cursor = 0
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": ids[i],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": json.dumps(tc["function"].get("arguments") or {}),
                            },
                        }
                        for i, tc in enumerate(tcs)
                    ],
                })
            else:
                out.append({"role": "assistant", "content": m.get("content") or ""})
        elif role == "tool":
            tcid = (
                pending_ids[tool_cursor]
                if tool_cursor < len(pending_ids)
                else f"call_orphan_{tool_cursor}"
            )
            tool_cursor += 1
            content = m.get("content")
            out.append({
                "role": "tool",
                "tool_call_id": tcid,
                "content": content if isinstance(content, str) else json.dumps(content),
            })
    return out


def _from_openai_response(data: dict) -> dict:
    """Translate an OpenAI /chat/completions response to the Ollama shape the
    conversation loop consumes, stashing provider tool_call ids for round 2+."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    result: dict = {"message": {"role": "assistant", "content": msg.get("content") or ""}}
    tcs = msg.get("tool_calls")
    if tcs:
        ids: list[str] = []
        out_tcs: list[dict] = []
        for tc in tcs:
            ids.append(tc.get("id") or f"call_{len(ids)}")
            args = (tc.get("function") or {}).get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    log.warning("cloud tool_call arguments were not valid JSON — passing empty args")
                    args = {}
            out_tcs.append({
                "function": {
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments": args or {},
                }
            })
        result["message"]["tool_calls"] = out_tcs
        result["message"][_ID_STASH] = ids
    if choice.get("finish_reason") == "length":
        log.warning("cloud response truncated (finish_reason=length) — consider a larger max_tokens")
    return result


class CloudLLMClient:
    """One OpenAI-compatible client for every configured provider."""

    def __init__(self, registry: ProviderRegistry, http: httpx.AsyncClient | None = None):
        self.registry = registry
        self._http = http or httpx.AsyncClient(timeout=120.0)

    async def list_models(self) -> list[str]:
        """Merged ["provider/model-id", ...] from every provider's GET /models.
        Per-provider failures are swallowed so one dead provider doesn't blank
        the list."""
        out: list[str] = []
        for p in self.registry.providers():
            try:
                r = await self._http.get(
                    f"{p.base_url}/models",
                    headers=_auth_headers(p, for_models=True),
                    timeout=8.0,
                )
                r.raise_for_status()
                data = r.json() or {}
                for m in data.get("data") or []:
                    mid = m.get("id")
                    if mid:
                        out.append(f"{p.name}/{mid}")
            except Exception as e:
                # status only — never key material
                log.warning(f"cloud models fetch failed for {p.name!r}: {type(e).__name__}: "
                            f"{getattr(getattr(e, 'response', None), 'status_code', '')}")
        return sorted(out)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> dict:
        """POST /chat/completions for `model` = "provider/model-id". Returns the
        Ollama-shaped response dict. Raises on any failure (the conversation
        falls back to local Ollama for the turn)."""
        provider_name, _, model_id = model.partition("/")
        p = self.registry.get(provider_name)
        if p is None or not model_id:
            raise RuntimeError(f"cloud provider {provider_name!r} not configured")

        payload: dict = {
            "model": model_id,
            "messages": _to_openai_messages(messages),
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        r = await self._http.post(
            f"{p.base_url}/chat/completions",
            headers=_auth_headers(p),
            json=payload,
        )
        r.raise_for_status()
        return _from_openai_response(r.json())

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception:
            pass
