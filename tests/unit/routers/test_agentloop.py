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
from qwenpaw.app.routers.agentloop_workflow_sse import (
    AgentLoopWorkflowSseTransformer,
    workflow_message_start_sse,
)
from qwenpaw.app.runner.models import ChatSpec
from qwenpaw.app.runner.runner import _query_opens_with_skill_tag


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
    assert not block["text"].startswith("/plan ")
    assert block["text"].startswith("[skill:writing]")
    assert block["text"].endswith("hello")


def test_build_run_input_prefixes_plan_when_missing():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(content="写一份通知")
    d = _build_run_input_dict(body, chat=chat)
    text = d["input"][0]["content"][0]["text"]
    assert text.startswith("/plan ")
    assert "写一份通知" in text


def test_build_run_input_empty_skill_still_prefixes_plan_for_gov():
    """Whitespace-only skill → treat as no skill; gov-like text still gets /plan."""
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(content="写一份通知", skill="   ")
    text = _build_run_input_dict(body, chat=chat)["input"][0]["content"][0]["text"]
    assert text.startswith("/plan ")
    assert "[skill:" not in text


def test_build_run_input_no_auto_plan_for_non_gov_plain_chat():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(content="hi")
    text = _build_run_input_dict(body, chat=chat)["input"][0]["content"][0]["text"]
    assert not text.startswith("/plan ")
    assert text == "hi"


def test_build_run_input_skill_strips_manual_plan_prefix():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(
        content="/plan 写一份地震通知",
        skill="gov-document-writer",
    )
    text = _build_run_input_dict(body, chat=chat)["input"][0]["content"][0]["text"]
    assert text.startswith("[skill:gov-document-writer]")
    assert "/plan" not in text
    assert "写一份地震通知" in text


def test_query_opens_with_skill_tag():
    assert _query_opens_with_skill_tag("[skill:x]\nhello")
    assert _query_opens_with_skill_tag("  [skill:gov-document-writer]\n")
    assert not _query_opens_with_skill_tag("/plan hello")
    assert not _query_opens_with_skill_tag("")


def test_build_run_input_does_not_double_plan_prefix():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(content="  /PLAN  已有描述  ")
    d = _build_run_input_dict(body, chat=chat)
    assert d["input"][0]["content"][0]["text"] == "  /PLAN  已有描述  "


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


def _parse_sse_data_lines(s: str) -> list[dict]:
    out = []
    for block in s.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: {"):
            continue
        out.append(json.loads(block[6:].strip()))
    return out


def test_workflow_message_start_line():
    line = workflow_message_start_sse().strip()
    assert json.loads(line[6:]) == {"type": "message_start"}


def test_workflow_transformer_create_plan_and_retrieval():
    wf = AgentLoopWorkflowSseTransformer()
    plan_obj = {
        "subtasks": [
            {"name": "资料检索", "description": "检索材料"},
            {"name": "公文写作", "description": "起草正文"},
        ],
    }
    create_plan_msg = {
        "object": "message",
        "status": "completed",
        "type": "plugin_call",
        "role": "assistant",
        "content": [
            {
                "object": "content",
                "type": "data",
                "data": {
                    "call_id": "p1",
                    "name": "create_plan",
                    "arguments": json.dumps(plan_obj, ensure_ascii=False),
                },
            },
        ],
    }
    batch = "data: " + json.dumps(create_plan_msg, ensure_ascii=False) + "\n\n"
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    assert any(x.get("type") == "content_block_start" for x in lines)
    stops = [x for x in lines if x.get("type") == "content_block_stop"]
    assert stops
    plan = stops[0]["payload"]["plan"]
    assert plan["intent"] == "document_workflow"
    assert len(plan["steps"]) == 2
    assert plan["steps"][0]["skillName"] == "retrieval"
    assert plan["steps"][1]["dependsOn"] == ["step_01_retrieval"]

    retrieval_body = {
        "skillName": "doc-retrieval",
        "stepIndex": 1,
        "displayText": "已完成：知识库文档检索",
        "normalizedResult": {
            "source": "legacy_success",
            "items": [{"title": "a.txt", "description": "d"}],
            "itemsTotal": 1,
        },
    }
    retrieval_msg = {
        "object": "message",
        "status": "completed",
        "type": "plugin_call_output",
        "role": "tool",
        "content": [
            {
                "object": "content",
                "type": "data",
                "data": {
                    "call_id": "r1",
                    "name": "doc_retrieval",
                    "output": json.dumps(retrieval_body, ensure_ascii=False),
                },
            },
        ],
    }
    batch2 = "data: " + json.dumps(retrieval_msg, ensure_ascii=False) + "\n\n"
    out2 = wf.consume_passthrough_batch(batch2)
    lines2 = _parse_sse_data_lines(out2)
    stop2 = [x for x in lines2 if x.get("type") == "content_block_stop"][-1]
    assert stop2["payload"]["skillName"] == "retrieval"
    assert stop2["payload"]["normalizedResult"]["itemsTotal"] == 1
    assert stop2["payload"]["result"]["itemsTotal"] == 1
    assert "resultList" not in stop2["payload"]

    tail = wf.finish()
    assert "message_stop" in tail
    assert "[DONE]" in tail


def test_workflow_transformer_doc_reviewer_merges_result_list():
    wf = AgentLoopWorkflowSseTransformer()
    review_body = {
        "skillName": "doc_reviewer",
        "stepIndex": 7,
        "displayText": "已完成：文档审核",
        "retryable": True,
        "sourceState": "model_success",
        "errorDetail": None,
        "normalizedResult": {
            "document": "标题\n\n正文",
            "source": "model_success",
        },
        "resultList": [
            {
                "reason": "序号错误",
                "offsets": 1,
                "errorType": "表述问题",
                "errorWord": "一",
                "contextOffset": 0,
                "context": "一",
                "rightWord": "二",
            },
        ],
    }
    msg = {
        "object": "message",
        "status": "completed",
        "type": "plugin_call_output",
        "role": "tool",
        "content": [
            {
                "object": "content",
                "type": "data",
                "data": {
                    "call_id": "rv1",
                    "name": "doc_reviewer",
                    "output": json.dumps(review_body, ensure_ascii=False),
                },
            },
        ],
    }
    out = wf.consume_passthrough_batch(
        "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n",
    )
    stop = [x for x in _parse_sse_data_lines(out) if x.get("type") == "content_block_stop"][
        -1
    ]
    p = stop["payload"]
    assert p["normalizedResult"]["document"] == "标题\n\n正文"
    assert p["normalizedResult"]["resultList"][0]["reason"] == "序号错误"
    assert p["result"]["document"] == "标题\n\n正文"
    assert p["result"]["resultList"][0]["errorType"] == "表述问题"
    assert len(p["resultList"]) == 1


def test_workflow_transformer_gov_layout_root_result_list_and_save_path():
    wf = AgentLoopWorkflowSseTransformer()
    layout_body = {
        "skillName": "gov_document_layout",
        "stepIndex": 4,
        "displayText": "已完成：公文排版",
        "retryable": True,
        "sourceState": "model_success",
        "errorDetail": None,
        "savePath": "C:\\tmp\\out.docx",
        "resultList": {"recommended": [{"id": "x", "templateTitle": "市局"}]},
    }
    msg = {
        "object": "message",
        "status": "completed",
        "type": "plugin_call_output",
        "role": "tool",
        "content": [
            {
                "object": "content",
                "type": "data",
                "data": {
                    "call_id": "ly1",
                    "name": "gov_document_layout",
                    "output": json.dumps(layout_body, ensure_ascii=False),
                },
            },
        ],
    }
    out = wf.consume_passthrough_batch(
        "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n",
    )
    stop = [x for x in _parse_sse_data_lines(out) if x.get("type") == "content_block_stop"][
        -1
    ]
    p = stop["payload"]
    assert p["savePath"] == "C:\\tmp\\out.docx"
    assert p["normalizedResult"]["savePath"] == "C:\\tmp\\out.docx"
    assert p["result"]["savePath"] == "C:\\tmp\\out.docx"
    assert p["normalizedResult"]["resultList"]["recommended"][0]["id"] == "x"
    assert p["result"]["resultList"]["recommended"][0]["templateTitle"] == "市局"


def test_tool_result_root_accepts_content_json_block():
    from qwenpaw.app.routers.agentloop_workflow_sse import _tool_result_root

    inner = {"skillName": "doc_reviewer", "sourceState": "ok", "stepIndex": 1}
    wrapped = {"content": [{"type": "json", "json": inner}]}
    assert _tool_result_root(wrapped) == inner
