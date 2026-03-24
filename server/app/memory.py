"""Long-term memory via Shodh Memory.

Stores and retrieves conversational memories so the LLM can remember
facts about users, preferences, and past interactions across sessions.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger("hal.memory")


class MemoryClient:
    """Client for the Shodh Memory REST API."""

    def __init__(self, base_url: str = "http://localhost:3030", user_id: str = "hal-default", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        else:
            # Shodh default dev key
            headers["X-API-Key"] = "sk-shodh-dev-default"
        self._http = httpx.AsyncClient(timeout=10.0, headers=headers)
        self._available = False

    async def initialize(self):
        """Check connectivity to the Shodh Memory service."""
        log.info(f"Shodh Memory endpoint: {self.base_url}")
        try:
            resp = await self._http.get(f"{self.base_url}/health")
            if resp.status_code == 200:
                self._available = True
                log.info("Shodh Memory service is available")
            else:
                log.warning(f"Shodh Memory returned {resp.status_code}")
        except Exception as e:
            log.warning(f"Shodh Memory not reachable: {e}")
            log.warning("Long-term memory will be unavailable")

    @property
    def available(self) -> bool:
        return self._available

    async def remember(self, content: str, memory_type: str = "conversation") -> bool:
        """
        Store a memory.

        Args:
            content: The text to remember
            memory_type: Category (e.g., "conversation", "preference", "fact")

        Returns True if stored successfully.
        """
        if not self._available or not content.strip():
            return False

        try:
            resp = await self._http.post(
                f"{self.base_url}/api/remember",
                json={
                    "user_id": self.user_id,
                    "content": content,
                    "memory_type": memory_type,
                },
            )
            if resp.status_code == 200:
                log.debug(f"Remembered: {content[:80]}...")
                return True
            else:
                log.warning(f"Remember failed ({resp.status_code}): {resp.text[:200]}")
                return False
        except Exception as e:
            log.error(f"Remember error: {e}")
            return False

    async def recall(self, query: str, limit: int = 5) -> list[str]:
        """
        Retrieve relevant memories for a query.

        Returns a list of memory strings, most relevant first.
        """
        if not self._available or not query.strip():
            return []

        try:
            resp = await self._http.post(
                f"{self.base_url}/api/recall",
                json={
                    "user_id": self.user_id,
                    "query": query,
                    "limit": limit,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                # Extract text from results (format may vary)
                memories = []
                results = data if isinstance(data, list) else data.get("results", data.get("memories", []))
                for item in results:
                    if isinstance(item, str):
                        memories.append(item)
                    elif isinstance(item, dict):
                        text = item.get("content", item.get("text", item.get("memory", "")))
                        if text:
                            memories.append(text)
                if memories:
                    log.debug(f"Recalled {len(memories)} memories for: {query[:50]}...")
                return memories
            else:
                log.warning(f"Recall failed ({resp.status_code}): {resp.text[:200]}")
                return []
        except Exception as e:
            log.error(f"Recall error: {e}")
            return []

    def format_memories_for_prompt(self, memories: list[str]) -> str:
        """Format recalled memories as a context block for the system prompt."""
        if not memories:
            return ""

        lines = ["[Recalled memories from past conversations:]"]
        for i, mem in enumerate(memories, 1):
            lines.append(f"  {i}. {mem}")
        return "\n".join(lines)
