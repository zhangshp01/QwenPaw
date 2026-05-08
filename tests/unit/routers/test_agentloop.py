# -*- coding: utf-8 -*-
"""Unit tests for govdoc-compatible AgentLoop API helpers."""
from __future__ import annotations

import json

from qwenpaw.app.routers.agentloop import (
    AgentLoopResponse,
    ConversationRunRequest,
    _build_run_input_dict,
    _passthrough_console_sse_chunk,
)
from qwenpaw.app.runner.models import ChatSpec


def test_agent_loop_response_envelope():
    body = AgentLoopResponse(code=0, message="ok", data={"a": 1}).model_dump()
    assert body == {"code": 0, "message": "ok", "data": {"a": 1}}


def test_passthrough_console_sse_emits_inner_json():
    raw = 'data: {"object":"content","type":"text","text":"hi"}\n\n'
    out = _passthrough_console_sse_chunk(raw)
    assert "data: " in out
    inner = json.loads(out.split("data: ", 1)[1].strip())
    assert inner["object"] == "content"
    assert inner["text"] == "hi"


def test_passthrough_done_unchanged():
    out = _passthrough_console_sse_chunk("data: [DONE]\n\n")
    assert "[DONE]" in out


def test_build_run_input_includes_skill_and_model():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(
        content="hello",
        model="p:m1",
        skill="writing",
    )
    d = _build_run_input_dict(body, chat=chat)
    assert d["model"] == "p:m1"
    assert d["user_id"] == "u1"
    assert d["session_id"] == "agentloop:c1"
    block = d["input"][0]["content"][0]
    assert block["type"] == "text"
    assert "[skill:writing]" in block["text"]
