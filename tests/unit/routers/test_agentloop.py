# -*- coding: utf-8 -*-
"""Unit tests for govdoc-compatible AgentLoop API helpers."""
from __future__ import annotations

import json

from qwenpaw.app.routers.agentloop import (
    AgentLoopResponse,
    ConversationRunRequest,
    _AgentLoopSseTerminalDeduper,
    _AgentLoopSseToolStreamDeduper,
    _build_run_input_dict,
    _passthrough_agentloop_sse_chunk,
    _passthrough_console_sse_chunk,
    _slim_agentloop_sse_payload,
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


def test_slim_agentloop_sse_payload_drops_nulls_and_empty_metadata():
    d = {
        "object": "message",
        "error": None,
        "metadata": {},
        "usage": None,
        "content": None,
        "x": {"metadata": {}, "y": 1},
    }
    slim = _slim_agentloop_sse_payload(d)
    assert slim == {"object": "message", "x": {"y": 1}}


def test_tool_stream_deduper_skips_identical_in_progress_arguments_and_output():
    deduper = _AgentLoopSseToolStreamDeduper()
    base = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "msg_id": "m1",
        "data": {"call_id": "c1", "name": "t", "arguments": "{}"},
    }
    assert deduper.should_skip(base) is False
    assert deduper.should_skip(dict(base)) is True
    assert deduper.should_skip({**base, "data": {**base["data"], "arguments": "x"}}) is False
    out_base = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "msg_id": "m2",
        "data": {"call_id": "c2", "name": "t", "output": ""},
    }
    assert deduper.should_skip(out_base) is False
    assert deduper.should_skip(dict(out_base)) is True


def test_compact_drops_response_in_progress_and_flattens_plugin_message():
    events = [
        {"object": "response", "status": "in_progress", "id": "r1"},
        {
            "object": "message",
            "status": "completed",
            "id": "m1",
            "type": "plugin_call",
            "role": "assistant",
            "content": [
                {
                    "object": "content",
                    "type": "data",
                    "data": {
                        "call_id": "c1",
                        "name": "gov",
                        "arguments": '{"a": 1}',
                    },
                },
            ],
        },
    ]
    raw = "".join(
        "data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events
    )
    term = _AgentLoopSseTerminalDeduper()
    tool_d = _AgentLoopSseToolStreamDeduper()
    out = _passthrough_agentloop_sse_chunk(raw, term, tool_d, compact=True)
    parts = [
        json.loads(b[6:].strip())
        for b in out.split("\n\n")
        if b.startswith("data: {")
    ]
    assert len(parts) == 1
    assert parts[0]["type"] == "plugin_call"
    assert "content" not in parts[0]
    assert parts[0]["tool"]["call_id"] == "c1"
    assert parts[0]["tool"]["arguments"] == '{"a": 1}'


def test_compact_false_keeps_response_in_progress():
    raw = 'data: {"object":"response","status":"in_progress","id":"r"}\n\n'
    term = _AgentLoopSseTerminalDeduper()
    tool_d = _AgentLoopSseToolStreamDeduper()
    out = _passthrough_agentloop_sse_chunk(raw, term, tool_d, compact=False)
    inner = json.loads(out.split("data: ", 1)[1].strip())
    assert inner["status"] == "in_progress"


def test_passthrough_agentloop_collapses_duplicate_tool_args_and_strips_output():
    chunk = "\n\n".join(
        [
            'data: {"object":"content","status":"in_progress","type":"data","msg_id":"m1","error":null,"metadata":{},"data":{"call_id":"c1","name":"t","arguments":"{}"}}',
            'data: {"object":"content","status":"in_progress","type":"data","msg_id":"m1","error":null,"metadata":{},"data":{"call_id":"c1","name":"t","arguments":"{}"}}',
            'data: {"object":"response","status":"completed","error":null,"output":[{"k":1}],"usage":{"total_tokens":3}}',
        ]
    ) + "\n\n"
    term = _AgentLoopSseTerminalDeduper()
    plug = _AgentLoopSseToolStreamDeduper()
    out = _passthrough_agentloop_sse_chunk(chunk, term, plug, compact=True)
    lines = [json.loads(x[6:].strip()) for x in out.split("\n\n") if x.startswith("data: {")]
    assert len(lines) == 2
    assert lines[0]["data"]["arguments"] == "{}"
    assert "output" not in lines[1]
    assert lines[1]["usage"] == {"total_tokens": 3}


def test_passthrough_agentloop_compact_false_keeps_output():
    chunk = 'data: {"object":"response","status":"completed","output":[1]}\n\n'
    term = _AgentLoopSseTerminalDeduper()
    plug = _AgentLoopSseToolStreamDeduper()
    out = _passthrough_agentloop_sse_chunk(chunk, term, plug, compact=False)
    inner = json.loads(out.split("data: ", 1)[1].strip())
    assert inner["output"] == [1]
