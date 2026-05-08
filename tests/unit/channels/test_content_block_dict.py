# -*- coding: utf-8 -*-
"""Debounce helpers: dict-shaped content_parts (e.g. AgentLoop) must count as text."""
from __future__ import annotations

from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    TextContent,
)

from qwenpaw.app.channels.base import BaseChannel


class _MiniChannel(BaseChannel):
    channel = "mini"


async def _noop_process(*_a, **_k):
    return None


def test_content_has_text_recognizes_json_dict_blocks():
    ch = _MiniChannel(process=_noop_process)
    assert ch._content_has_text([{"type": "text", "text": "你好"}])
    assert not ch._content_has_text([{"type": "text", "text": ""}])


def test_content_has_text_still_accepts_typed_content():
    ch = _MiniChannel(process=_noop_process)
    assert ch._content_has_text(
        [TextContent(type=ContentType.TEXT, text="x")],
    )


def test_content_has_audio_recognizes_dict_blocks():
    ch = _MiniChannel(process=_noop_process)
    assert ch._content_has_audio([{"type": "audio", "data": "x"}])
    assert not ch._content_has_audio([{"type": "text", "text": "n"}])
