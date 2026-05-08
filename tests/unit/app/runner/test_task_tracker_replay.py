# -*- coding: utf-8 -*-
"""Finished-run SSE replay for late attach (AgentLoop GET /events after POST /run)."""
from __future__ import annotations

import asyncio

from qwenpaw.app.runner.task_tracker import TaskTracker


def test_attach_replays_buffer_after_run_finishes():
    async def _run():
        async def stream_one(_payload):
            yield 'data: {"ok":true}\n\n'

        tracker = TaskTracker()
        queue, is_new = await tracker.attach_or_start("chat-uuid", {}, stream_one)
        assert is_new is True

        pieces: list[str] = []
        async for chunk in tracker.stream_from_queue(queue, "chat-uuid"):
            pieces.append(chunk)

        await asyncio.sleep(0.05)

        late = await tracker.attach("chat-uuid")
        assert late is not None
        replayed: list[str] = []
        while True:
            item = await asyncio.wait_for(late.get(), timeout=2.0)
            if item is None:
                break
            replayed.append(item)

        assert len(replayed) >= 1
        assert "ok" in "".join(replayed)

    asyncio.run(_run())


def test_second_attach_after_replay_returns_none():
    async def _run():
        async def stream_one(_payload):
            yield "data: 1\n\n"

        tracker = TaskTracker()
        q, _ = await tracker.attach_or_start("k2", {}, stream_one)
        async for _ in tracker.stream_from_queue(q, "k2"):
            pass
        await asyncio.sleep(0.05)
        first = await tracker.attach("k2")
        assert first is not None
        while True:
            item = await first.get()
            if item is None:
                break
        second = await tracker.attach("k2")
        assert second is None

    asyncio.run(_run())
