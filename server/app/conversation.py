"""Conversation manager: wake word detection, LLM with MCP tool-calling, response generation.

Transcription is always-on and always displayed. The wake word only gates
whether the LLM processes the utterance as a command/question.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import httpx

from .mcp_client import MCPClient, MultiMCPClient
from .memory import MemoryClient
from .tts import TTSEngine

log = logging.getLogger("hal.conversation")

DEFAULT_SYSTEM_PROMPT = """You are PAL, a helpful voice assistant for a smart home. \
You speak naturally and concisely, like a person in a conversation. \
Keep responses brief — 1-2 sentences max — because they will be spoken aloud.

You have access to tools that control a Home Assistant smart home. \
When the user asks you to do something with the home (lights, climate, media, etc.), \
use the appropriate tool. When the request is conversational, just respond naturally.

If you're unsure which entity the user means, ask for clarification."""


ROUTER_SYSTEM_PROMPT = """You are a routing classifier for a smart-home voice \
assistant. Classify the user's command:
- Reply SIMPLE if it is ONLY turning a light or switch on/off, or reading a \
single sensor value (e.g. "turn off the lamp", "what's the bedroom temperature").
- Reply COMPLEX for anything else: questions, general knowledge, scenes, \
automations, media, multi-step requests, or anything ambiguous.
Reply with exactly one word: SIMPLE or COMPLEX."""



class ConversationManager:
    """Manages the conversation state machine and LLM interactions via MCP."""

    def __init__(
        self,
        wake_word: str,
        ollama_host: str,
        ollama_model: str,
        mcp_client: MCPClient | MultiMCPClient,
        tts_engine: TTSEngine,
        memory_client: MemoryClient | None = None,
        system_prompt: str = "",
        num_ctx: int = 32768,
        num_predict: int = 512,
        fallback_ollama_model: str = "",
        router_enabled: bool = False,
        router_model: str = "",
        event_logger: Callable[..., Awaitable[None]] | None = None,
    ):
        self.wake_word = wake_word.lower().strip() if wake_word else ""
        self.ollama_host = ollama_host.rstrip("/")
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.ollama_model = ollama_model
        # Optional secondary model. Used only when the primary "collapses"
        # — produces a completion of pure whitespace/nothing that fills the
        # generation budget. Empty string = no fallback. See
        # `_is_model_collapsed` for the detection rule.
        self.fallback_ollama_model = (fallback_ollama_model or "").strip()
        self.mcp = mcp_client
        self.tts_engine = tts_engine
        self.memory = memory_client
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        # Conversation log sink (injected by main.py; resolves origin tokens to
        # device names at write time). Called as
        # event_logger(kind, text, origin_token=...). None = logging disabled.
        self.event_logger = event_logger

        # Router model: a tiny, fast LLM that decides — per command — whether
        # to use the local Ollama path or the OpenClaw agent. Only consulted
        # when OpenClaw is live (see on_silence). Both are mutated at runtime
        # by the config handlers, like ollama_model.
        self.router_enabled = bool(router_enabled)
        self.router_model = (router_model or "").strip()

        # State: idle, listening, processing, speaking
        self.state = "idle"

        # Conversation history for context. Long-term recall lives in
        # Shodh; raw history just keeps the recent thread coherent, so
        # 20 turns is plenty and keeps per-round prompt eval fast.
        self.history: list[dict] = []
        self.max_history = 20
        # Auto-compaction: once the rolling thread grows past max_history,
        # the older turns are summarized into a durable note, persisted to
        # Shodh long-term memory, and dropped — keeping only `compact_keep`
        # recent messages verbatim. `_context_summary` is the running summary,
        # injected into the system prompt so the live thread stays coherent
        # without depending on a Shodh recall hitting the same content.
        self.compact_keep = 6
        self._context_summary: str = ""
        self._compacting = False

        # Text buffer accumulated after wake word
        self._command_buffer: list[str] = []
        self._wake_detected = False
        self._last_text_time = 0.0
        # Satellite mode: origin of the in-flight turn. `_pending_origin` is set
        # by /api/command (the device token of a connected satellite, or None for
        # voice/kiosk/global); on_silence snapshots it into `_turn_origin` for the
        # duration of the turn so the output callbacks can route to that device.
        self._pending_origin: str | None = None
        self._turn_origin: str | None = None
        # True while a Push-to-Talk session owns the audio path. Set/cleared
        # exclusively from server/app/ptt.py. The wake-word + on_silence
        # path doesn't actually need this flag (it reuses _wake_detected),
        # but exposing it makes the LLM-side logging and any future
        # ptt-aware behaviour explicit.
        self.ptt_active: bool = False

        # Follow-up: when the LLM asks a question, stay in listening mode
        # so the user can answer without repeating the wake word
        self._followup_window = 30.0  # seconds — generous for thinking time
        self._awaiting_followup = False

        # When the LLM uses speak_verbatim during this turn we suppress the
        # final TTS for the model's text response (otherwise we'd speak twice).
        self._suppress_final_tts = False

        # OpenClaw integration
        self.openclaw_client = None  # OpenClawClient | None
        self.on_openclaw_media: Callable | None = None

        # Cloud LLM override (cloud_llm.CloudLLMClient | None). When set (the
        # "Cloud Override" switch is ON and a model is chosen) the whole tool
        # loop runs against the cloud provider — router + OpenClaw are skipped.
        # `_force_local_turn` is the per-turn fallback flag: a cloud failure
        # retries the turn on local Ollama WITHOUT touching the user's switch.
        self.cloud_llm = None
        self.cloud_llm_model: str = ""
        self._force_local_turn = False

        # Callbacks
        self.on_response: Callable[[str, bytes | None], Awaitable[None]] | None = None
        self.on_wake_word: Callable[[], Awaitable[None]] | None = None
        self.on_state_change: Callable[[str], Awaitable[None]] | None = None
        # Fired with a timing/metrics dict at the end of every task.
        self.on_metrics: Callable[[dict], Awaitable[None]] | None = None

        # Per-task scratch space for timing collection.
        self._llm_metrics_acc: dict = {}
        self._last_ollama_metrics: dict = {}

        self._http = httpx.AsyncClient(timeout=120.0)

    async def _set_state(self, new_state: str):
        """Update state and notify listeners."""
        self.state = new_state
        if self.on_state_change:
            await self.on_state_change(new_state)

    def _build_system_prompt(self) -> str:
        from datetime import datetime
        # datetime.now().astimezone() uses the system's /etc/localtime
        # (mounted from host) — automatically handles DST, works on any distro,
        # no timezone name detection needed
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %A %Z")
        prompt = f"{self._system_prompt}\n\nCurrent date and time: {now}"
        if self._context_summary:
            prompt += (
                "\n\n[Earlier in this conversation (summarized):]\n"
                f"{self._context_summary}"
            )
        return prompt

    @property
    def always_on(self) -> bool:
        """Whether the assistant processes all speech (no wake word configured)."""
        return not self.wake_word

    @property
    def in_conversation(self) -> bool:
        """Whether we're in an active conversation."""
        if self.always_on:
            return True
        if self._wake_detected:
            return True
        if self._awaiting_followup:
            return True
        return False

    async def process_text(self, text: str):
        """
        Process a finalized transcription segment.

        All text is displayed on the UI regardless (handled by main.py).
        This method only decides whether to engage the LLM.
        """
        self._last_text_time = time.monotonic()
        text_lower = text.lower().strip()

        if self.always_on:
            # No wake word — process every line
            self._command_buffer.append(text)
            await self._set_state("listening")
            return

        if not self.in_conversation:
            # Look for wake word to start a conversation
            if self.wake_word in text_lower:
                log.info(f"Wake word detected: '{text}'")
                self._wake_detected = True
                await self._set_state("listening")
                # Notify listeners (chime + UI flash)
                if self.on_wake_word:
                    await self.on_wake_word()
                # Capture text around the wake word (before + after)
                idx = text_lower.index(self.wake_word)
                before = text[:idx].strip()
                after = text[idx + len(self.wake_word):].strip()
                remainder = f"{before} {after}".strip()
                if remainder:
                    self._command_buffer.append(remainder)
                return
            # No wake word, not in conversation — do nothing
            return

        # In active conversation — accumulate text
        self._command_buffer.append(text)
        await self._set_state("listening")

    async def on_silence(self):
        """Called when silence is detected after speech. Triggers LLM if in conversation."""
        if not self.in_conversation or not self._command_buffer:
            return

        # Combine buffered text into a single command. Snapshot the turn origin
        # at this atomic point (no await between the guard above and here, so
        # exactly one on_silence claims a non-empty buffer). _turn_origin drives
        # output routing for the whole turn; voice/kiosk turns leave it None.
        full_text = " ".join(self._command_buffer).strip()
        self._command_buffer.clear()
        self._turn_origin = self._pending_origin
        self._pending_origin = None
        self._wake_detected = False
        self._awaiting_followup = False

        if not full_text:
            self._turn_origin = None
            await self._set_state("idle")
            return

        log.info(f"[task] START processing: '{full_text}'")
        t_task_start = time.monotonic()
        await self._set_state("processing")
        # Conversation log: record the request BEFORE the LLM runs (a crash
        # mid-turn still records what was asked). origin_token resolves to the
        # device name in the injected wrapper — never stored raw.
        if self.event_logger:
            try:
                await self.event_logger("user", full_text, origin_token=self._turn_origin)
            except Exception as e:
                log.debug(f"event_logger(user) failed: {e}")
        self._suppress_final_tts = False
        self._llm_metrics_acc = {}
        self._last_ollama_metrics = {}
        memory_remember_s = 0.0
        tts_s = 0.0

        route_s = 0.0
        try:
            # Cloud override wins over everything: skip the router AND OpenClaw
            # and run the normal tool loop with the cloud transport (the branch
            # lives in _chat_completion). A cloud failure falls back to local
            # Ollama for THIS turn only — never flip the user's switch.
            cloud_handled = False
            use_openclaw = False
            if self.cloud_llm is not None and self.cloud_llm_model:
                cloud_handled = True
                try:
                    response_text = await self._run_llm_with_tools(full_text)
                except Exception as e:
                    log.error(f"[cloud] {self.cloud_llm_model} failed ({e}); "
                              f"falling back to local Ollama for this turn")
                    # The failed attempt already appended the user msg to
                    # history — pop it so the local retry doesn't double-append.
                    if self.history and self.history[-1].get("role") == "user":
                        self.history.pop()
                    self._force_local_turn = True
                    try:
                        response_text = await self._run_llm_with_tools(full_text)
                    finally:
                        self._force_local_turn = False
            else:
                # Decide engine. OpenClaw is only "live" when its client is set —
                # the OpenClaw Enabled toggle nulls it when off, so this check also
                # covers configured-but-disabled. The router only runs when there's
                # genuinely a choice to make (OpenClaw live) and it's turned on.
                use_openclaw = self.openclaw_client is not None
                if use_openclaw and self.router_enabled and self.router_model:
                    t_route = time.monotonic()
                    decision = await self._route_decision(full_text)
                    route_s = time.monotonic() - t_route
                    use_openclaw = (decision != "ollama")
                    log.info(f"[router] {self.router_model} → {decision} in {route_s:.2f}s")

            if cloud_handled:
                pass  # response_text already produced by the cloud override
            elif use_openclaw:
                try:
                    oc_response = await self._run_openclaw(full_text)
                    response_text = oc_response.text or ""
                    if self.on_openclaw_media and oc_response.media_urls:
                        audio_media_played = await self.on_openclaw_media(oc_response)
                        if audio_media_played:
                            # An attached audio file is the spoken output —
                            # don't also speak the reply text (Option A).
                            self._suppress_final_tts = True
                            log.info("Audio media played — suppressing text TTS")
                except Exception as e:
                    log.error(f"OpenClaw failed ({e}), falling back to Ollama")
                    response_text = await self._run_llm_with_tools(full_text)
            else:
                response_text = await self._run_llm_with_tools(full_text)
            t_after_llm = time.monotonic()

            # Store conversation exchange in long-term memory
            if self.memory and self.memory.available:
                memory_text = f"User said: {full_text}\nHAL responded: {response_text}"
                t_remember = time.monotonic()
                await self.memory.remember(memory_text, memory_type="conversation")
                memory_remember_s = time.monotonic() - t_remember
                log.info(f"[task] memory.remember took {memory_remember_s:.2f}s")

            # Synthesize speech (skip if speak_verbatim already handled audio)
            if self._suppress_final_tts:
                log.info("Final TTS suppressed (speak_verbatim was used)")
                audio_bytes = None
            else:
                await self._set_state("speaking")
                t_tts = time.monotonic()
                audio_bytes = await self.tts_engine.synthesize(response_text)
                tts_s = time.monotonic() - t_tts
                log.info(
                    f"[task] tts.synthesize took {tts_s:.2f}s "
                    f"({len(response_text)} chars -> "
                    f"{len(audio_bytes) if audio_bytes else 0} bytes)"
                )

            # Send response back (with audio_bytes=None when suppressed)
            if self.on_response:
                await self.on_response(response_text, audio_bytes)

            # Conversation log: record the answer (skip empty — mirrors the
            # no-empty-history rule; speak_verbatim turns log their spoken text
            # as an announcement at the tool site instead).
            if self.event_logger and response_text.strip():
                try:
                    await self.event_logger(
                        "assistant", response_text, origin_token=self._turn_origin
                    )
                except Exception as e:
                    log.debug(f"event_logger(assistant) failed: {e}")

            # If the LLM asked a question, stay in conversation for a follow-up
            if response_text.rstrip().endswith("?"):
                self._awaiting_followup = True
                log.info("LLM asked a question — awaiting follow-up")

            task_total_s = time.monotonic() - t_task_start
            log.info(
                f"[task] END processing in {task_total_s:.2f}s "
                f"(llm+tools {t_after_llm - t_task_start:.2f}s)"
            )

            if self.on_metrics:
                metrics = {
                    "task_total_s": round(task_total_s, 2),
                    "route_s": round(route_s, 2),
                    "llm_total_s": round(self._llm_metrics_acc.get("llm_total_s", 0.0), 2),
                    "tools_total_s": round(self._llm_metrics_acc.get("tools_total_s", 0.0), 2),
                    "memory_recall_s": round(self._llm_metrics_acc.get("memory_recall_s", 0.0), 2),
                    "memory_remember_s": round(memory_remember_s, 2),
                    "tts_s": round(tts_s, 2),
                    "rounds": int(self._llm_metrics_acc.get("rounds", 0)),
                    "model": self._last_ollama_metrics.get("model") or self.ollama_model,
                    "gen_tps": round(self._last_ollama_metrics.get("gen_tps", 0.0), 1),
                    "prompt_tps": round(self._last_ollama_metrics.get("prompt_tps", 0.0), 1),
                    "gen_n": int(self._last_ollama_metrics.get("gen_n", 0)),
                }
                try:
                    await self.on_metrics(metrics)
                except Exception as e:
                    log.debug(f"on_metrics callback failed: {e}")

        except Exception as e:
            log.error(f"Error processing command: {e}", exc_info=True)
            error_msg = "Sorry, I encountered an error processing that."
            if self.on_response:
                audio = await self.tts_engine.synthesize(error_msg)
                await self.on_response(error_msg, audio)
            if self.event_logger:  # the apology is what was actually spoken
                try:
                    await self.event_logger(
                        "assistant", error_msg, origin_token=self._turn_origin
                    )
                except Exception as le:
                    log.debug(f"event_logger(error) failed: {le}")

        # Final idle state routes to the turn's origin; clear origin strictly
        # after, so the idle state-change still reaches the right device.
        await self._set_state("idle")
        self._turn_origin = None

        # Auto-compact the rolling thread once it grows too long (off the
        # response critical path — the user already heard the reply).
        await self._compact_history()

    async def _run_openclaw(self, user_text: str):
        from .openclaw_client import OpenClawResponse

        self.history.append({"role": "user", "content": user_text})
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        t0 = time.monotonic()
        response: OpenClawResponse = await self.openclaw_client.send_message(user_text)
        elapsed = time.monotonic() - t0

        if response.text:
            self.history.append({"role": "assistant", "content": response.text})

        self._llm_metrics_acc = {
            "llm_total_s": elapsed,
            "tools_total_s": 0.0,
            "memory_recall_s": 0.0,
            "rounds": 1,
        }
        self._last_ollama_metrics = {"model": "openclaw"}
        log.info(f"OpenClaw responded in {elapsed:.2f}s: {len(response.text)} chars")
        return response

    async def _run_llm_with_tools(self, user_text: str) -> str:
        """
        Send user text to Ollama with MCP tools available.

        Implements a tool-calling loop: the LLM can call tools, receive results,
        and then generate a final text response.
        """
        self.history.append({"role": "user", "content": user_text})
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        # Recall relevant memories and inject into system prompt
        system_prompt = self._build_system_prompt()
        memory_recall_s = 0.0
        if self.memory and self.memory.available:
            t_recall = time.monotonic()
            memories = await self.memory.recall(user_text, limit=3)
            memory_recall_s = time.monotonic() - t_recall
            log.info(
                f"[llm] memory.recall took {memory_recall_s:.2f}s "
                f"(returned {len(memories) if memories else 0} entries)"
            )
            if memories:
                memory_block = self.memory.format_memories_for_prompt(memories)
                system_prompt = system_prompt + "\n\n" + memory_block

        messages = [{"role": "system", "content": system_prompt}] + self.history

        # Tool-calling loop (max 8 rounds to prevent infinite loops)
        t_llm_total = 0.0
        t_tools_total = 0.0
        rounds = 0
        for _ in range(8):
            rounds += 1
            t_round = time.monotonic()
            response = await self._chat_completion(messages)
            t_llm_total += time.monotonic() - t_round
            self._llm_metrics_acc = {
                "rounds": rounds,
                "llm_total_s": t_llm_total,
                "tools_total_s": t_tools_total,
                "memory_recall_s": memory_recall_s,
            }

            # Collapse detection: if the model burned its generation
            # budget producing nothing, optionally retry the SAME round
            # with the fallback model. We don't touch `self.ollama_model`
            # — next turn starts fresh on the primary.
            if self._is_model_collapsed(response):
                gen_n = (self._last_ollama_metrics or {}).get("gen_n", 0)
                if self.fallback_ollama_model:
                    log.warning(
                        f"[llm] primary model {self.ollama_model!r} collapsed "
                        f"(empty text, no tool calls, gen={gen_n}/{self.num_predict}); "
                        f"retrying with fallback {self.fallback_ollama_model!r}"
                    )
                    t_round_fb = time.monotonic()
                    response = await self._chat_completion(
                        messages, model_override=self.fallback_ollama_model,
                    )
                    t_llm_total += time.monotonic() - t_round_fb
                    self._llm_metrics_acc["llm_total_s"] = t_llm_total
                    if self._is_model_collapsed(response):
                        log.error(
                            f"[llm] fallback model {self.fallback_ollama_model!r} "
                            f"ALSO collapsed; giving up this turn"
                        )
                else:
                    log.warning(
                        f"[llm] primary model {self.ollama_model!r} collapsed "
                        f"(empty text, no tool calls, gen={gen_n}/{self.num_predict}); "
                        f"no fallback_ollama_model configured — returning empty"
                    )

            # Check if the LLM wants to call a tool
            tool_calls = response.get("message", {}).get("tool_calls")
            assistant_text = response.get("message", {}).get("content", "")
            log.info(f"Ollama response: tool_calls={bool(tool_calls)}, text={assistant_text[:100] if assistant_text else '(empty)'}")
            if not tool_calls:
                # No tool call — final text response. Only append to
                # history if there's actually content; an empty string
                # would just poison subsequent turns (this is what we
                # saw with the collapsed gemma4:e4b prompts).
                if assistant_text.strip():
                    self.history.append({"role": "assistant", "content": assistant_text})
                log.info(
                    f"[llm] DONE in {rounds} round(s) "
                    f"(llm {t_llm_total:.2f}s, tools {t_tools_total:.2f}s)"
                )
                return assistant_text

            # Execute each tool call
            assistant_msg = response["message"]
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})

                log.info(f"LLM calling tool: {tool_name}({json.dumps(tool_args)})")
                t_tool = time.monotonic()
                result = await self.mcp.call_tool(tool_name, tool_args)
                tool_elapsed = time.monotonic() - t_tool
                t_tools_total += tool_elapsed
                self._llm_metrics_acc["tools_total_s"] = t_tools_total
                log.info(f"[llm] tool {tool_name} took {tool_elapsed:.2f}s")

                # Feed tool result back into the conversation
                messages.append({
                    "role": "tool",
                    "content": result,
                })

        # If we exhausted the loop, return last assistant message
        log.warning(
            f"[llm] hit 8-round cap "
            f"(llm {t_llm_total:.2f}s, tools {t_tools_total:.2f}s)"
        )
        fallback = "I've completed the action."
        self.history.append({"role": "assistant", "content": fallback})
        return fallback

    async def _compact_history(self) -> None:
        """Auto-compaction (size-triggered).

        Once the rolling thread grows past ``max_history``, summarize the older
        turns into a durable note, fold it into the running ``_context_summary``,
        persist that note to Shodh long-term memory, and keep only the most
        recent ``compact_keep`` messages verbatim. The running summary rides in
        the system prompt (see ``_build_system_prompt``) so continuity holds
        without relying on a Shodh recall surfacing the same content.

        Runs at the end of a turn, off the response critical path. If
        summarization is unavailable it falls back to a hard trim so the prompt
        can never grow unbounded.
        """
        if self._compacting or len(self.history) <= self.max_history:
            return
        self._compacting = True
        try:
            keep = max(0, self.compact_keep)
            older = self.history[:-keep] if keep else list(self.history)
            recent = self.history[-keep:] if keep else []
            if not older:
                return
            summary = await self._summarize_history(older)
            if not summary:
                # Summarization failed/unavailable — hard trim as a safety net.
                self.history = self.history[-self.max_history:]
                log.warning("[compact] summarize unavailable — hard-trimmed history")
                return
            self._context_summary = summary
            if self.memory and self.memory.available:
                try:
                    await self.memory.remember(summary, memory_type="conversation_summary")
                except Exception as e:
                    log.warning(f"[compact] memory.remember failed: {e}")
            self.history = recent
            log.info(
                f"[compact] folded {len(older)} msgs into summary "
                f"({len(summary)} chars), kept {len(recent)} verbatim"
            )
        except Exception as e:
            log.error(f"[compact] error: {e}", exc_info=True)
        finally:
            self._compacting = False

    async def _summarize_history(self, msgs: list[dict]) -> str:
        """Summarize a slice of history into a concise third-person memory note.

        Tool-free, single-shot. Folds in the prior running summary so successive
        compactions accumulate rather than forget."""
        convo = "\n".join(
            f"{m.get('role', 'user')}: {m['content']}"
            for m in msgs
            if (m.get("content") or "").strip()
        )
        if not convo.strip():
            return ""
        prior = (
            f"Summary of the conversation so far:\n{self._context_summary}\n\n"
            if self._context_summary else ""
        )
        instructions = (
            "Summarize the conversation below between a user and HAL (a home "
            "assistant) into a concise third-person memory note. Capture durable "
            "facts, preferences, decisions, and any unresolved threads; drop "
            "pleasantries and tool mechanics. If a prior summary is given, merge "
            "the new exchanges into it. 2-4 sentences, no preamble."
        )
        messages = [
            {"role": "system", "content": "You compress conversations into durable memory notes."},
            {"role": "user", "content": f"{instructions}\n\n{prior}New exchanges:\n{convo}"},
        ]
        try:
            resp = await self._chat_completion(messages, with_tools=False)
            return (resp.get("message", {}).get("content", "") or "").strip()
        except Exception as e:
            log.warning(f"[compact] summarize failed: {e}")
            return ""

    async def _route_decision(self, user_text: str) -> str:
        """Ask the small router model which engine should handle this command.

        Returns "ollama" for simple commands (light/switch on-off, single
        sensor read) or "openclaw" for everything else. Self-contained,
        tool-free, tiny context. Any error or unexpected output → "openclaw"
        so we never silently lose the agent's capability.
        """
        payload = {
            "model": self.router_model,
            "messages": [
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            "keep_alive": -1,
            # Disable the model's chain-of-thought: thinking-capable models
            # (e.g. gemma4:e2b) otherwise burn the tiny num_predict budget on
            # reasoning tokens and return empty content. We just want the label.
            "think": False,
            "options": {"temperature": 0, "num_predict": 16, "num_ctx": 2048},
        }
        try:
            resp = await self._http.post(
                f"{self.ollama_host}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            out = (resp.json().get("message", {}).get("content") or "").strip().lower()
        except Exception as e:
            log.warning(f"[router] decision failed ({e}) → openclaw")
            return "openclaw"
        return "ollama" if ("ollama" in out or "simple" in out) else "openclaw"

    async def _chat_completion(
        self,
        messages: list[dict],
        model_override: str | None = None,
        with_tools: bool = True,
    ) -> dict:
        """Call Ollama chat API with tool definitions.

        `model_override` is used for one-shot fallback retries when the
        primary model collapses (see `_is_model_collapsed`). When set, it
        replaces `self.ollama_model` for this single call ONLY — the
        instance attribute is untouched so subsequent rounds/turns go
        back to the primary.
        """
        # Cloud override transport: tool-loop calls go to the cloud provider.
        # Utility calls (with_tools=False — e.g. history compaction) stay on
        # local Ollama so background work never burns cloud tokens, and
        # `_force_local_turn` routes the per-turn failure fallback locally.
        # Exceptions propagate (no fake-response swallow like the Ollama path)
        # so on_silence can retry the turn on Ollama.
        if (self.cloud_llm is not None and self.cloud_llm_model
                and with_tools and not self._force_local_turn):
            t0 = time.monotonic()
            response = await self.cloud_llm.chat(
                self.cloud_llm_model,
                messages,
                self.mcp.tools_for_llm or None,
                temperature=0.7,
                # Reasoning models (gpt-5 family) spend completion budget on
                # hidden reasoning tokens — the local num_predict (512) leaves
                # zero room for the visible answer. Give cloud real headroom;
                # replies stay short via the system prompt.
                max_tokens=max(4096, self.num_predict),
            )
            wall = time.monotonic() - t0
            self._last_ollama_metrics = {
                "model": self.cloud_llm_model,
                "gen_n": 0, "gen_tps": 0.0, "prompt_tps": 0.0,
            }
            log.info(f"[cloud] {self.cloud_llm_model} wall={wall:.2f}s msgs={len(messages)}")
            return response

        active_model = (model_override or self.ollama_model)
        payload = {
            "model": active_model,
            "messages": messages,
            "stream": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0.7,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }

        # Include tools if available (skipped for utility calls like
        # summarization that must produce plain text, never a tool call).
        tools = self.mcp.tools_for_llm if with_tools else None
        if tools:
            payload["tools"] = tools
            log.debug(f"Sending {len(tools)} tools to Ollama")

        t0 = time.monotonic()
        try:
            resp = await self._http.post(
                f"{self.ollama_host}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Ollama error after {time.monotonic() - t0:.2f}s: {e}")
            return {"message": {"content": "I'm having trouble thinking right now. Could you try again?"}}

        # Wall clock from our side, plus Ollama's own breakdown if present.
        # Ollama returns durations in nanoseconds; we convert to seconds and
        # surface a tokens/sec figure so it's easy to spot a slow GPU vs slow
        # network/HTTP.
        wall = time.monotonic() - t0
        load_s = data.get("load_duration", 0) / 1e9
        prompt_n = data.get("prompt_eval_count", 0)
        prompt_s = data.get("prompt_eval_duration", 0) / 1e9
        gen_n = data.get("eval_count", 0)
        gen_s = data.get("eval_duration", 0) / 1e9
        prompt_tps = (prompt_n / prompt_s) if prompt_s else 0
        gen_tps = (gen_n / gen_s) if gen_s else 0
        msg_count = len(messages)
        # Rough char count of the inbound context for sizing intuition.
        ctx_chars = sum(
            len(m.get("content") or "")
            + sum(len(str(tc)) for tc in (m.get("tool_calls") or []))
            for m in messages
        )
        log.info(
            f"[ollama] {active_model} wall={wall:.2f}s "
            f"load={load_s:.2f}s "
            f"prompt={prompt_n}t/{prompt_s:.2f}s ({prompt_tps:.0f} t/s) "
            f"gen={gen_n}t/{gen_s:.2f}s ({gen_tps:.0f} t/s) "
            f"msgs={msg_count} ctx≈{ctx_chars}c"
        )
        self._last_ollama_metrics = {
            "model": active_model,
            "wall_s": wall,
            "load_s": load_s,
            "prompt_n": prompt_n,
            "prompt_s": prompt_s,
            "gen_n": gen_n,
            "gen_s": gen_s,
            "prompt_tps": prompt_tps,
            "gen_tps": gen_tps,
        }
        return data

    def _is_model_collapsed(self, response: dict) -> bool:
        """Heuristic: did the model burn its generation budget on nothing?

        Three signals must all hold:
          - text is empty (or pure whitespace)
          - no tool calls were emitted
          - eval_count from Ollama is at the num_predict cap (>=95 %)

        Signal #3 is what distinguishes a *collapsed* model from one
        that thoughtfully decided to say nothing — a healthy "I have
        nothing more" stops well before the cap. Ollama not returning
        eval_count yields 0, which fails the ratio check, so we never
        false-positive in that case.
        """
        if not isinstance(response, dict):
            return False
        msg = response.get("message") or {}
        text = (msg.get("content") or "").strip()
        if text:
            return False
        if msg.get("tool_calls"):
            return False
        gen_n = (self._last_ollama_metrics or {}).get("gen_n", 0)
        try:
            cap = int(self.num_predict)
        except (TypeError, ValueError):
            return False
        if cap <= 0:
            return False
        return gen_n >= int(cap * 0.95)
