# -*- coding: utf-8 -*-
"""In-memory mapping of AgentLoop run_id -> conversation_id per workspace."""

from __future__ import annotations

import asyncio


class AgentLoopRunRegistry:
    """Maps each AgentLoop run UUID to its parent conversation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._run_to_conversation: dict[str, str] = {}

    async def register(self, run_id: str, conversation_id: str) -> None:
        async with self._lock:
            self._run_to_conversation[run_id] = conversation_id

    async def resolve_conversation(self, run_id: str) -> str | None:
        async with self._lock:
            return self._run_to_conversation.get(run_id)
