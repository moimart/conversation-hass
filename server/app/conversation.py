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

from .mcp_client import MCPClient
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
        mcp_client: MCPClient,
        tts_engine: TTSEngine,
        system_prompt: str = "",
    ):
        self.wake_word = wake_word.lower().strip()
        self.ollama_host = ollama_host.rstrip("/")
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.ollama_model = ollama_model
        self.mcp = mcp_client
        self.tts_engine = tts_engine

        # State: idle, listening, processing, speaking
        self.state = "idle"

        # Conversation history for context
        self.history: list[dict] = []
        self.max_history = 20

        # Text buffer accumulated after wake word
        self._command_buffer: list[str] = []
        self._wake_detected = False
        self._last_text_time = 0.0

        # Continuous conversation: after the AI responds, stay in listening
        # mode for a follow-up window (no need to repeat the wake word)
        self._followup_window = 10.0  # seconds
        self._last_response_time = 0.0

        # Callbacks
        self.on_response: Callable[[str, bytes | None], Awaitable[None]] | None = None
        self.on_wake_word: Callable[[], Awaitable[None]] | None = None

        self._http = httpx.AsyncClient(timeout=120.0)

    def _build_system_prompt(self) -> str:
        return self._system_prompt

    @property
    def in_conversation(self) -> bool:
        """Whether we're in an active conversation (wake word or follow-up window)."""
        if self._wake_detected:
            return True
        if self._last_response_time > 0:
            return (time.monotonic() - self._last_response_time) < self._followup_window
        return False

    async def process_text(self, text: str):
        """
        Process a finalized transcription segment.

        All text is displayed on the UI regardless (handled by main.py).
        This method only decides whether to engage the LLM.
        """
        self._last_text_time = time.monotonic()
        text_lower = text.lower().strip()

        if not self.in_conversation:
            # Look for wake word to start a conversation
            if self.wake_word in text_lower:
                log.info(f"Wake word detected: '{text}'")
                self._wake_detected = True
                self.state = "listening"
                # Notify listeners (chime + UI flash)
                if self.on_wake_word:
                    await self.on_wake_word()
                # Capture any text after the wake word as part of the command
                idx = text_lower.index(self.wake_word) + len(self.wake_word)
                remainder = text[idx:].strip()
                if remainder:
                    self._command_buffer.append(remainder)
                return
            # No wake word, not in conversation — do nothing
            return

        # In active conversation — accumulate text
        self._command_buffer.append(text)
        self.state = "listening"

    async def on_silence(self):
        """Called when silence is detected after speech. Triggers LLM if in conversation."""
        if not self.in_conversation or not self._command_buffer:
            return

        # Combine buffered text into a single command
        full_text = " ".join(self._command_buffer).strip()
        self._command_buffer.clear()
        self._wake_detected = False

        if not full_text:
            self.state = "idle"
            return

        log.info(f"Processing: '{full_text}'")
        self.state = "processing"

        try:
            response_text = await self._run_llm_with_tools(full_text)

            # Synthesize speech
            self.state = "speaking"
            audio_bytes = await self.tts_engine.synthesize(response_text)

            # Send response back
            if self.on_response:
                await self.on_response(response_text, audio_bytes)

            self._last_response_time = time.monotonic()

        except Exception as e:
            log.error(f"Error processing command: {e}", exc_info=True)
            error_msg = "Sorry, I encountered an error processing that."
            if self.on_response:
                audio = await self.tts_engine.synthesize(error_msg)
                await self.on_response(error_msg, audio)

        self.state = "idle"

    async def _run_llm_with_tools(self, user_text: str) -> str:
        """
        Send user text to Ollama with MCP tools available.

        Implements a tool-calling loop: the LLM can call tools, receive results,
        and then generate a final text response.
        """
        self.history.append({"role": "user", "content": user_text})
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        messages = [{"role": "system", "content": self._build_system_prompt()}] + self.history

        # Tool-calling loop (max 5 rounds to prevent infinite loops)
        for _ in range(5):
            response = await self._chat_completion(messages)

            # Check if the LLM wants to call a tool
            tool_calls = response.get("message", {}).get("tool_calls")
            assistant_text = response.get("message", {}).get("content", "")
            log.info(f"Ollama response: tool_calls={bool(tool_calls)}, text={assistant_text[:100] if assistant_text else '(empty)'}")
            if not tool_calls:
                # No tool call — final text response
                self.history.append({"role": "assistant", "content": assistant_text})
                return assistant_text

            # Execute each tool call
            assistant_msg = response["message"]
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})

                log.info(f"LLM calling tool: {tool_name}({json.dumps(tool_args)})")
                result = await self.mcp.call_tool(tool_name, tool_args)

                # Feed tool result back into the conversation
                messages.append({
                    "role": "tool",
                    "content": result,
                })

        # If we exhausted the loop, return last assistant message
        fallback = "I've completed the action."
        self.history.append({"role": "assistant", "content": fallback})
        return fallback

    async def _chat_completion(self, messages: list[dict]) -> dict:
        """Call Ollama chat API with tool definitions."""
        payload = {
            "model": self.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 256,
            },
        }

        # Include tools if available
        tools = self.mcp.tools_for_llm
        if tools:
            payload["tools"] = tools
            log.info(f"Sending {len(tools)} tools to Ollama")

        try:
            resp = await self._http.post(
                f"{self.ollama_host}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Ollama error: {e}")
            return {"message": {"content": "I'm having trouble thinking right now. Could you try again?"}}
