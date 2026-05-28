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

DEFAULT_SYSTEM_PROMPT = """You are HAL, a helpful voice assistant for a smart home. \
You speak naturally and concisely, like a person in a conversation. \
Keep responses brief — 1-2 sentences max — because they will be spoken aloud.

You have access to tools that control a Home Assistant smart home. \
When the user asks you to do something with the home (lights, climate, media, etc.), \
use the appropriate tool. When the request is conversational, just respond naturally.

If you're unsure which entity the user means, ask for clarification."""



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

        # State: idle, listening, processing, speaking
        self.state = "idle"

        # Conversation history for context. Long-term recall lives in
        # Shodh; raw history just keeps the recent thread coherent, so
        # 20 turns is plenty and keeps per-round prompt eval fast.
        self.history: list[dict] = []
        self.max_history = 20

        # Text buffer accumulated after wake word
        self._command_buffer: list[str] = []
        self._wake_detected = False
        self._last_text_time = 0.0
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
        return f"{self._system_prompt}\n\nCurrent date and time: {now}"

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

        # Combine buffered text into a single command
        full_text = " ".join(self._command_buffer).strip()
        self._command_buffer.clear()
        self._wake_detected = False
        self._awaiting_followup = False

        if not full_text:
            await self._set_state("idle")
            return

        log.info(f"[task] START processing: '{full_text}'")
        t_task_start = time.monotonic()
        await self._set_state("processing")
        self._suppress_final_tts = False
        self._llm_metrics_acc = {}
        self._last_ollama_metrics = {}
        memory_remember_s = 0.0
        tts_s = 0.0

        try:
            if self.openclaw_client is not None:
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

        await self._set_state("idle")

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

    async def _chat_completion(
        self,
        messages: list[dict],
        model_override: str | None = None,
    ) -> dict:
        """Call Ollama chat API with tool definitions.

        `model_override` is used for one-shot fallback retries when the
        primary model collapses (see `_is_model_collapsed`). When set, it
        replaces `self.ollama_model` for this single call ONLY — the
        instance attribute is untouched so subsequent rounds/turns go
        back to the primary.
        """
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

        # Include tools if available
        tools = self.mcp.tools_for_llm
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
