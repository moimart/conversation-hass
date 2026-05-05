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
    ):
        self.wake_word = wake_word.lower().strip() if wake_word else ""
        self.ollama_host = ollama_host.rstrip("/")
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.ollama_model = ollama_model
        self.mcp = mcp_client
        self.tts_engine = tts_engine
        self.memory = memory_client
        self.num_ctx = num_ctx
        self.num_predict = num_predict

        # State: idle, listening, processing, speaking
        self.state = "idle"

        # Conversation history for context
        self.history: list[dict] = []
        self.max_history = 100

        # Text buffer accumulated after wake word
        self._command_buffer: list[str] = []
        self._wake_detected = False
        self._last_text_time = 0.0

        # Follow-up: when the LLM asks a question, stay in listening mode
        # so the user can answer without repeating the wake word
        self._followup_window = 30.0  # seconds — generous for thinking time
        self._awaiting_followup = False

        # When the LLM uses speak_verbatim during this turn we suppress the
        # final TTS for the model's text response (otherwise we'd speak twice).
        self._suppress_final_tts = False

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
            memories = await self.memory.recall(user_text, limit=5)
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

            # Check if the LLM wants to call a tool
            tool_calls = response.get("message", {}).get("tool_calls")
            assistant_text = response.get("message", {}).get("content", "")
            log.info(f"Ollama response: tool_calls={bool(tool_calls)}, text={assistant_text[:100] if assistant_text else '(empty)'}")
            if not tool_calls:
                # No tool call — final text response
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

    async def _chat_completion(self, messages: list[dict]) -> dict:
        """Call Ollama chat API with tool definitions."""
        payload = {
            "model": self.ollama_model,
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
            f"[ollama] {self.ollama_model} wall={wall:.2f}s "
            f"load={load_s:.2f}s "
            f"prompt={prompt_n}t/{prompt_s:.2f}s ({prompt_tps:.0f} t/s) "
            f"gen={gen_n}t/{gen_s:.2f}s ({gen_tps:.0f} t/s) "
            f"msgs={msg_count} ctx≈{ctx_chars}c"
        )
        self._last_ollama_metrics = {
            "model": self.ollama_model,
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
