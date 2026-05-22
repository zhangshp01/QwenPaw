# -*- coding: utf-8 -*-
"""Unit tests for govdoc-compatible AgentLoop API helpers."""
from __future__ import annotations

import json

from agentscope_runtime.engine.schemas.agent_schemas import (
    DataContent,
    FunctionCall,
    FunctionCallOutput,
    Message,
    MessageType,
    TextContent,
)

import asyncio

from qwenpaw.app.routers.agentloop import (
    AgentLoopResponse,
    ConversationRunRequest,
    _AgentLoopSseTerminalDeduper,
    _AgentLoopSseToolStreamDeduper,
    _build_run_input_dict,
    _last_run_id_from_chat,
    _passthrough_agentloop_sse_chunk,
    _passthrough_console_sse_chunk,
    _run_belongs_to_conversation,
    _slim_agentloop_sse_payload,
)
from qwenpaw.app.workspace.agentloop_run_registry import AgentLoopRunRegistry
from qwenpaw.app.routers.agentloop_conversation_detail import (
    build_conversation_detail,
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


def test_build_run_input_prefixes_plan_when_skill_empty():
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
    assert text.endswith("写一份通知")


def test_build_run_input_whitespace_skill_treated_as_empty_skill_gets_plan_prefix():
    """Whitespace-only skill → same as no skill: ``/plan`` prefix."""
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
    assert "写一份通知" in text


def test_build_run_input_plain_chat_gets_plan_prefix_when_no_skill():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
    )
    body = ConversationRunRequest(content="hi")
    text = _build_run_input_dict(body, chat=chat)["input"][0]["content"][0]["text"]
    assert text.startswith("/plan ")
    assert text.endswith("hi")


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
            {"name": "审核公文", "description": "审核起草的公文，确保格式正确"},
            {"name": "推荐版式", "description": "推荐排版"},
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
    starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
    ]
    assert starts and starts[0]["content_block"]["skillName"] == "a2a_planning"
    stops = [x for x in lines if x.get("type") == "content_block_stop"]
    assert stops
    plan = stops[0]["payload"]["plan"]
    assert plan["intent"] == "document_workflow"
    assert len(plan["steps"]) == 4
    assert plan["steps"][0]["skillName"] == "doc_retrieval"
    assert plan["steps"][1]["skillName"] == "gov_document_writer"
    assert plan["steps"][2]["skillName"] == "doc_reviewer"
    assert plan["steps"][3]["skillName"] == "gov_document_layout"
    assert plan["steps"][1]["dependsOn"] == ["step_01_doc_retrieval"]

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
    tool_starts2 = [
        x
        for x in lines2
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
    ]
    assert len(tool_starts2) == 1
    assert tool_starts2[0]["content_block"]["skillName"] == "doc_retrieval"
    stop2 = [x for x in lines2 if x.get("type") == "content_block_stop"][-1]
    assert stop2["payload"]["skillName"] == "doc_retrieval"
    assert stop2["payload"]["normalizedResult"]["itemsTotal"] == 1
    assert "result" not in stop2["payload"]
    assert "resultList" not in stop2["payload"]

    tail = wf.finish()
    assert "message_stop" in tail
    assert "[DONE]" in tail


def test_tool_use_start_label_maps_gov_skills_to_chinese():
    assert (
        AgentLoopWorkflowSseTransformer._tool_use_start_label("doc_retrieval", None)
        == "进行中：资料检索"
    )
    assert (
        AgentLoopWorkflowSseTransformer._tool_use_start_label(
            "gov_document_writer",
            None,
        )
        == "进行中：公文写作"
    )
    assert (
        AgentLoopWorkflowSseTransformer._tool_use_start_label("doc_reviewer", None)
        == "进行中：公文审核"
    )
    assert (
        AgentLoopWorkflowSseTransformer._tool_use_start_label(
            "gov_document_layout",
            None,
        )
        == "进行中：公文排版"
    )


def test_workflow_tool_emits_tool_use_start_on_in_progress_before_completed():
    """Long-running tools: first in_progress exposes tool_use start; completed only closes."""
    wf = AgentLoopWorkflowSseTransformer()
    cid = "rvp1"
    in_prog = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "data": {"call_id": cid, "name": "doc_reviewer", "arguments": "{}"},
    }
    body = {
        "skillName": "doc_reviewer",
        "stepIndex": 3,
        "displayText": "已完成：文档审核",
        "normalizedResult": {"document": "正文", "source": "model_success"},
    }
    done = {
        "object": "content",
        "status": "completed",
        "type": "data",
        "data": {
            "call_id": cid,
            "name": "doc_reviewer",
            "output": json.dumps(body, ensure_ascii=False),
        },
    }
    batch = (
        "data: " + json.dumps(in_prog, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(done, ensure_ascii=False) + "\n\n"
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    tu_starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
        and (x.get("content_block") or {}).get("skillName") == "doc_reviewer"
    ]
    assert len(tu_starts) == 1
    assert tu_starts[0]["content_block"]["displayText"] == "进行中：公文审核"
    stops = [
        x
        for x in lines
        if x.get("type") == "content_block_stop"
        and x.get("payload", {}).get("tool") == "doc_reviewer"
    ]
    assert len(stops) == 1
    assert stops[0]["payload"]["normalizedResult"]["document"] == "正文"


def test_workflow_tool_no_second_start_when_completed_call_id_differs_from_in_progress():
    """Mismatched ``call_id`` on in_progress vs completed must not synthesize another start."""
    wf = AgentLoopWorkflowSseTransformer()
    cid_ip = "call_in_progress"
    cid_done = "call_on_output"
    in_prog = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "data": {"call_id": cid_ip, "name": "doc_retrieval", "arguments": "{}"},
    }
    body = {
        "skillName": "doc_retrieval",
        "stepIndex": 3,
        "displayText": "已完成：知识库文档检索",
        "normalizedResult": {"items": [], "itemsTotal": 0, "source": "legacy_success"},
    }
    done = {
        "object": "content",
        "status": "completed",
        "type": "data",
        "data": {
            "call_id": cid_done,
            "name": "doc_retrieval",
            "output": json.dumps(body, ensure_ascii=False),
        },
    }
    batch = (
        "data: " + json.dumps(in_prog, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(done, ensure_ascii=False) + "\n\n"
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    tu_starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
        and (x.get("content_block") or {}).get("skillName") == "doc_retrieval"
    ]
    assert len(tu_starts) == 1
    stops = [
        x
        for x in lines
        if x.get("type") == "content_block_stop"
        and x.get("payload", {}).get("tool") == "doc_retrieval"
    ]
    assert len(stops) == 1


def test_workflow_planning_emits_tool_use_start_on_in_progress_before_completed():
    wf = AgentLoopWorkflowSseTransformer()
    plan_obj = {
        "subtasks": [
            {"name": "资料检索", "description": "检索材料"},
        ],
    }
    in_prog = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "data": {
            "call_id": "p1",
            "name": "create_plan",
            "arguments": json.dumps(plan_obj, ensure_ascii=False),
        },
    }
    done = {
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
    batch = (
        "data: " + json.dumps(in_prog, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(done, ensure_ascii=False) + "\n\n"
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    plan_starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
        and (x.get("content_block") or {}).get("skillName") == "a2a_planning"
    ]
    assert len(plan_starts) == 1
    plan_stops = [
        x
        for x in lines
        if x.get("type") == "content_block_stop"
        and x.get("payload", {}).get("tool") == "a2a_planning"
    ]
    assert len(plan_stops) == 1


def test_workflow_planning_ignores_late_in_progress_after_completed():
    """Upstream may replay in_progress after plan completed — no orphan tool_use start."""
    wf = AgentLoopWorkflowSseTransformer()
    plan_obj = {"subtasks": [{"name": "检索", "description": "d"}]}
    cid = "p1"
    in_prog = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "data": {
            "call_id": cid,
            "name": "create_plan",
            "arguments": json.dumps(plan_obj, ensure_ascii=False),
        },
    }
    done = {
        "object": "message",
        "status": "completed",
        "type": "plugin_call",
        "role": "assistant",
        "content": [
            {
                "object": "content",
                "type": "data",
                "data": {
                    "call_id": cid,
                    "name": "create_plan",
                    "arguments": json.dumps(plan_obj, ensure_ascii=False),
                },
            },
        ],
    }
    batch = (
        "data: " + json.dumps(in_prog, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(done, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(dict(in_prog), ensure_ascii=False) + "\n\n"
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    plan_starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("type") == "tool_use"
        and (x.get("content_block") or {}).get("skillName") == "a2a_planning"
    ]
    assert len(plan_starts) == 1


def test_workflow_tool_ignores_late_in_progress_after_completed():
    wf = AgentLoopWorkflowSseTransformer()
    cid = "t1"
    body = {
        "skillName": "doc_retrieval",
        "stepIndex": 1,
        "displayText": "已完成：知识库文档检索",
        "normalizedResult": {"items": [], "itemsTotal": 0, "source": "legacy_success"},
    }
    done = {
        "object": "content",
        "status": "completed",
        "type": "data",
        "data": {
            "call_id": cid,
            "name": "doc_retrieval",
            "output": json.dumps(body, ensure_ascii=False),
        },
    }
    late_ip = {
        "object": "content",
        "status": "in_progress",
        "type": "data",
        "data": {"call_id": cid, "name": "doc_retrieval", "arguments": "{}"},
    }
    batch = (
        "data: " + json.dumps(done, ensure_ascii=False) + "\n\n"
        "data: " + json.dumps(late_ip, ensure_ascii=False) + "\n\n"
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    starts = [
        x
        for x in lines
        if x.get("type") == "content_block_start"
        and (x.get("content_block") or {}).get("skillName") == "doc_retrieval"
    ]
    assert len(starts) == 1


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
    assert p["normalizedResult"]["resultList"][0]["errorType"] == "表述问题"
    assert "result" not in p
    assert len(p["normalizedResult"]["resultList"]) == 1


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
        "normalizedResult": {
            "resultList": [{"id": "x", "templateTitle": "市局"}],
        },
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
    assert p["normalizedResult"]["savePath"] == "C:\\tmp\\out.docx"
    assert p["normalizedResult"]["resultList"][0]["id"] == "x"
    assert p["normalizedResult"]["resultList"][0]["templateTitle"] == "市局"
    assert "result" not in p


def test_workflow_skips_placeholder_doc_reviewer_when_rich_follows():
    wf = AgentLoopWorkflowSseTransformer()
    stub = {
        "skillName": "doc_reviewer",
        "stepIndex": 0,
        "displayText": "已完成：doc_reviewer",
        "normalizedResult": {"source": "model_success"},
    }
    rich = {
        "skillName": "doc_reviewer",
        "stepIndex": 9,
        "displayText": "已完成：文档审核",
        "normalizedResult": {"document": "正文", "source": "model_success"},
        "resultList": [],
    }
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "same",
                                "name": "doc_reviewer",
                                "output": json.dumps(stub, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "same",
                                "name": "doc_reviewer",
                                "output": json.dumps(rich, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch)
    stops = [x for x in _parse_sse_data_lines(out) if x.get("type") == "content_block_stop"]
    doc_stops = [s for s in stops if s.get("payload", {}).get("tool") == "doc_reviewer"]
    assert len(doc_stops) == 1
    assert doc_stops[0]["payload"]["normalizedResult"]["document"] == "正文"


def test_workflow_drops_orphan_placeholder_gov_writer_after_rich_other_call_id():
    """Weak writer completion for call_id A stays pending; rich for B must not flush A at ``finish``."""
    wf = AgentLoopWorkflowSseTransformer()
    stub = {
        "skillName": "gov_document_writer",
        "stepIndex": 0,
        "displayText": "已完成：gov_document_writer",
        "normalizedResult": {"source": "model_success"},
    }
    rich = {
        "skillName": "gov_document_writer",
        "stepIndex": 3,
        "displayText": "已完成：公文写作",
        "normalizedResult": {
            "document": "关于做好地震防范工作的通知\n\n地震防范工作细则与说明段落。",
            "source": "model_success",
        },
    }
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "early",
                                "name": "gov_document_writer",
                                "output": json.dumps(stub, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "final",
                                "name": "gov_document_writer",
                                "output": json.dumps(rich, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch) + wf.finish()
    lines = _parse_sse_data_lines(out)
    writer_stops = [
        s
        for s in lines
        if s.get("type") == "content_block_stop"
        and s.get("payload", {}).get("tool") == "gov_document_writer"
    ]
    assert len(writer_stops) == 1
    assert writer_stops[0]["payload"]["normalizedResult"]["document"].startswith(
        "关于做好地震防范",
    )


def test_workflow_drops_orphan_placeholder_doc_retrieval_after_rich_other_call_id():
    """Weak doc_retrieval for call_id A stays pending; rich for B must not flush A at ``finish``."""
    wf = AgentLoopWorkflowSseTransformer()
    stub = {
        "skillName": "doc_retrieval",
        "stepIndex": 0,
        "displayText": "已完成：doc_retrieval",
        "normalizedResult": {"source": "model_success"},
    }
    rich = {
        "skillName": "doc_retrieval",
        "stepIndex": 3,
        "displayText": "已完成：知识库文档检索",
        "normalizedResult": {
            "source": "legacy_success",
            "items": [],
            "itemsTotal": 0,
        },
    }
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "early",
                                "name": "doc_retrieval",
                                "output": json.dumps(stub, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "final",
                                "name": "doc_retrieval",
                                "output": json.dumps(rich, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch) + wf.finish()
    lines = _parse_sse_data_lines(out)
    retrieval_stops = [
        s
        for s in lines
        if s.get("type") == "content_block_stop"
        and s.get("payload", {}).get("tool") == "doc_retrieval"
    ]
    assert len(retrieval_stops) == 1
    assert retrieval_stops[0]["payload"]["stepIndex"] == 3
    assert retrieval_stops[0]["payload"]["normalizedResult"]["itemsTotal"] == 0


def test_workflow_ignores_late_thin_doc_retrieval_after_rich_other_call_id():
    """Rich completion first; deferrable thin for another call_id must not emit a second stop."""
    wf = AgentLoopWorkflowSseTransformer()
    stub = {
        "skillName": "doc_retrieval",
        "stepIndex": 0,
        "displayText": "已完成：doc_retrieval",
        "normalizedResult": {"source": "model_success"},
    }
    rich = {
        "skillName": "doc_retrieval",
        "stepIndex": 3,
        "displayText": "已完成：知识库文档检索",
        "normalizedResult": {
            "source": "legacy_success",
            "items": [],
            "itemsTotal": 0,
        },
    }
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "final",
                                "name": "doc_retrieval",
                                "output": json.dumps(rich, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call_output",
                    "role": "tool",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "early",
                                "name": "doc_retrieval",
                                "output": json.dumps(stub, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch) + wf.finish()
    lines = _parse_sse_data_lines(out)
    retrieval_stops = [
        s
        for s in lines
        if s.get("type") == "content_block_stop"
        and s.get("payload", {}).get("tool") == "doc_retrieval"
    ]
    assert len(retrieval_stops) == 1


def test_workflow_skips_empty_plan_after_nonempty():
    """Second upstream plan completion with 0 subtasks is suppressed after a real plan."""
    wf = AgentLoopWorkflowSseTransformer()
    full = {"subtasks": [{"name": "检索", "description": "d"}]}
    empty = {"subtasks": []}
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
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
                                "arguments": json.dumps(full, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "message",
                    "status": "completed",
                    "type": "plugin_call",
                    "role": "assistant",
                    "content": [
                        {
                            "object": "content",
                            "type": "data",
                            "data": {
                                "call_id": "p2",
                                "name": "revise_current_plan",
                                "arguments": json.dumps(empty, ensure_ascii=False),
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    plan_stops = [
        x
        for x in lines
        if x.get("type") == "content_block_stop"
        and x.get("payload", {}).get("tool") == "a2a_planning"
    ]
    assert len(plan_stops) == 1
    assert plan_stops[0]["payload"]["steps"] == 1


def test_tool_result_root_accepts_content_json_block():
    from qwenpaw.app.routers.agentloop_workflow_sse import _tool_result_root

    inner = {"skillName": "doc_reviewer", "sourceState": "ok", "stepIndex": 1}
    wrapped = {"content": [{"type": "json", "json": inner}]}
    assert _tool_result_root(wrapped) == inner


def test_workflow_text_completed_without_in_progress_pairs_start_delta_stop():
    """Upstream may omit text ``in_progress``; emit ``content_block_start`` before deltas."""
    wf = AgentLoopWorkflowSseTransformer()
    msg = {
        "object": "content",
        "type": "text",
        "status": "completed",
        "text": "仅此一句",
    }
    out = wf.consume_passthrough_batch(
        "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n",
    )
    lines = _parse_sse_data_lines(out)
    types = [x.get("type") for x in lines]
    assert types == ["content_block_start", "content_block_delta", "content_block_stop"]
    assert lines[0]["content_block"]["type"] == "text"
    assert lines[1]["delta"]["text"] == "仅此一句"
    assert lines[2]["payload"]["tool"] == "assistant"


def test_workflow_passthrough_error_emits_tool_use_start_then_stop():
    wf = AgentLoopWorkflowSseTransformer()
    err_obj = {"error": "boom"}
    out = wf.consume_passthrough_batch(
        "data: " + json.dumps(err_obj, ensure_ascii=False) + "\n\n",
    )
    lines = _parse_sse_data_lines(out)
    assert len(lines) >= 2
    assert lines[0]["type"] == "content_block_start"
    assert lines[0]["content_block"]["type"] == "tool_use"
    assert lines[0]["content_block"]["skillName"] == "error"
    assert lines[1]["type"] == "content_block_stop"
    assert lines[1]["payload"]["sourceState"] == "error"
    assert lines[1]["payload"]["errorDetail"] == "boom"


def test_workflow_streams_document_deltas_from_tool_output_in_progress():
    wf = AgentLoopWorkflowSseTransformer()
    cid = "writer-stream"
    partial = {
        "skillName": "gov_document_writer",
        "displayText": "进行中：公文写作",
        "normalizedResult": {"document": "大数据", "source": "model_success"},
    }
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "content",
                    "status": "in_progress",
                    "type": "data",
                    "data": {
                        "call_id": cid,
                        "name": "gov_document_writer",
                        "output": json.dumps(partial, ensure_ascii=False),
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "content",
                    "status": "in_progress",
                    "type": "data",
                    "data": {
                        "call_id": cid,
                        "name": "gov_document_writer",
                        "output": json.dumps(
                            {
                                **partial,
                                "normalizedResult": {
                                    "document": "大数据局",
                                    "source": "model_success",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch)
    lines = _parse_sse_data_lines(out)
    starts = [x for x in lines if x.get("type") == "content_block_start"]
    deltas = [x for x in lines if x.get("type") == "content_block_delta"]
    assert len(starts) == 1
    assert starts[0]["content_block"]["skillName"] == "gov_document_writer"
    assert [d["delta"]["text"] for d in deltas] == ["大", "数", "据", "局"]


def test_workflow_streams_document_deltas_from_tool_arguments_in_progress():
    wf = AgentLoopWorkflowSseTransformer()
    cid = "writer-args"
    batch = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {
                    "object": "content",
                    "status": "in_progress",
                    "type": "data",
                    "data": {
                        "call_id": cid,
                        "name": "gov_document_writer",
                        "arguments": '{"content":"标题',
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "object": "content",
                    "status": "in_progress",
                    "type": "data",
                    "data": {
                        "call_id": cid,
                        "name": "gov_document_writer",
                        "arguments": '{"content":"标题正文"}',
                    },
                },
                ensure_ascii=False,
            )
            + "\n\n",
        ]
    )
    out = wf.consume_passthrough_batch(batch)
    deltas = [
        x["delta"]["text"]
        for x in _parse_sse_data_lines(out)
        if x.get("type") == "content_block_delta"
    ]
    assert deltas == ["标", "题", "正", "文"]


def test_workflow_completed_doc_reviewer_does_not_emit_document_deltas():
    """doc_reviewer is non-streaming; only content_block_stop carries the final payload."""
    wf = AgentLoopWorkflowSseTransformer()
    cid = "review-done"
    body = {
        "skillName": "doc_reviewer",
        "stepIndex": 9,
        "displayText": "已完成：文档审核",
        "normalizedResult": {"document": "审核稿", "source": "model_success"},
        "resultList": [],
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
                    "call_id": cid,
                    "name": "doc_reviewer",
                    "output": json.dumps(body, ensure_ascii=False),
                },
            },
        ],
    }
    out = wf.consume_passthrough_batch(
        "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n",
    )
    lines = _parse_sse_data_lines(out)
    deltas = [
        x["delta"]["text"]
        for x in lines
        if x.get("type") == "content_block_delta"
    ]
    stops = [x for x in lines if x.get("type") == "content_block_stop"]
    assert deltas == []
    assert stops[-1]["payload"]["normalizedResult"]["document"] == "审核稿"


def _msg(
    *,
    role: str,
    mtype: MessageType,
    metadata: dict | None = None,
) -> Message:
    m = Message(type=mtype, role=role)
    m.metadata = metadata or {}
    return m


def test_build_conversation_detail_user_and_plain_assistant():
    from datetime import datetime, timezone

    chat = ChatSpec(
        id="59fb02af-2b68-411d-bb9b-bee412223d94",
        name="测试会话",
        session_id="agentloop:59fb02af",
        user_id="u1",
        channel="console",
        created_at=datetime(2026, 4, 30, 0, 51, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 30, 0, 52, 40, tzinfo=timezone.utc),
    )
    user = _msg(role="user", mtype=MessageType.MESSAGE, metadata={"original_id": "msg_u1"})
    user.add_content(TextContent(delta=False, index=None, text="11"))
    assistant = _msg(
        role="assistant",
        mtype=MessageType.MESSAGE,
        metadata={"original_id": "msg_a1"},
    )
    assistant.add_content(
        TextContent(delta=False, index=None, text="您好，有什么可以帮您？"),
    )
    detail = build_conversation_detail(
        chat,
        [user.completed(), assistant.completed()],
        model="Qwen3-235B-A22B-FP8",
    )
    assert detail["id"] == chat.id
    assert detail["title"] == "测试会话"
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][0]["content"] == {"text": "11"}
    assert detail["messages"][1]["content"]["text"].startswith("您好")
    assert detail["messages"][1]["content"]["model"] == "Qwen3-235B-A22B-FP8"
    assert detail["artifacts"] == []


def test_build_conversation_detail_pipeline_steps():
    from datetime import datetime, timezone

    chat = ChatSpec(
        id="59fb02af-2b68-411d-bb9b-bee412223d94",
        name="帮我写一篇关于大数据局的公文",
        session_id="agentloop:59fb02af",
        user_id="u1",
        channel="console",
        created_at=datetime(2026, 4, 30, 0, 51, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 30, 0, 52, 40, tzinfo=timezone.utc),
    )
    user = _msg(role="user", mtype=MessageType.MESSAGE, metadata={"original_id": "msg_u1"})
    user.add_content(
        TextContent(
            delta=False,
            index=None,
            text="/plan 帮我写一篇关于大数据局的公文",
        ),
    )
    plan_call = _msg(role="assistant", mtype=MessageType.PLUGIN_CALL)
    plan_call.add_content(
        DataContent(
            delta=False,
            index=None,
            data=FunctionCall(
                call_id="plan-1",
                name="create_plan",
                arguments=json.dumps(
                    {
                        "subtasks": [
                            {
                                "name": "资料检索",
                                "description": "检索大数据局职责资料",
                            },
                            {
                                "name": "公文写作",
                                "description": "撰写公文正文",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
            ).model_dump(),
        ),
    )
    retrieval_out = _msg(role="system", mtype=MessageType.PLUGIN_CALL_OUTPUT)
    retrieval_body = {
        "skillName": "doc_retrieval",
        "stepIndex": 1,
        "sourceState": "legacy_success",
        "normalizedResult": {
            "source": "legacy_success",
            "items": [{"title": "大数据局职责.txt", "description": "职责摘要"}],
            "itemsTotal": 1,
        },
    }
    retrieval_out.add_content(
        DataContent(
            delta=False,
            index=None,
            data=FunctionCallOutput(
                call_id="r1",
                name="doc_retrieval",
                output=json.dumps(
                    [{"type": "json", "json": retrieval_body}],
                    ensure_ascii=False,
                ),
            ).model_dump(exclude_none=True),
        ),
    )
    writer_out = _msg(role="system", mtype=MessageType.PLUGIN_CALL_OUTPUT)
    writer_body = {
        "skillName": "gov_document_writer",
        "stepIndex": 2,
        "sourceState": "model_success",
        "normalizedResult": {
            "document": "关于数据工作的通知\n\n正文内容。",
            "source": "model_success",
        },
    }
    writer_out.add_content(
        DataContent(
            delta=False,
            index=None,
            data=FunctionCallOutput(
                call_id="w1",
                name="gov_document_writer",
                output=json.dumps(
                    [{"type": "json", "json": writer_body}],
                    ensure_ascii=False,
                ),
            ).model_dump(exclude_none=True),
        ),
    )
    detail = build_conversation_detail(
        chat,
        [
            user.completed(),
            plan_call.completed(),
            retrieval_out.completed(),
            writer_out.completed(),
        ],
        model="Qwen3-235B-A22B-FP8",
    )
    assert len(detail["messages"]) == 2
    assistant = detail["messages"][1]
    assert assistant["role"] == "assistant"
    content = assistant["content"]
    assert content["model"] == "Qwen3-235B-A22B-FP8"
    assert content["planIntent"] == "document_workflow"
    assert "2 个 sub-agent 步骤" in content["planSummary"]
    steps = content["steps"]
    assert len(steps) == 2
    assert steps[0]["skillName"] == "retrieval"
    assert steps[0]["title"] == "资料检索"
    assert steps[0]["taskId"].startswith("task_59fb02af_")
    assert steps[0]["normalizedResult"]["items"][0]["title"] == "大数据局职责.txt"
    assert steps[1]["skillName"] == "writing"
    assert "document" in steps[1]["normalizedResult"]


def test_build_conversation_detail_review_only_step():
    from datetime import datetime, timezone

    chat = ChatSpec(
        id="adbc179f-1db6-465c-9514-95f2157767d2",
        name="审核",
        session_id="agentloop:adbc179f",
        user_id="u1",
        channel="console",
        created_at=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 30, 1, 1, 0, tzinfo=timezone.utc),
    )
    user = _msg(role="user", mtype=MessageType.MESSAGE)
    user.add_content(
        TextContent(
            delta=False,
            index=None,
            text="[skill:doc-reviewer]\n请帮我审核如下信息：原文。",
        ),
    )
    review_out = _msg(role="system", mtype=MessageType.PLUGIN_CALL_OUTPUT)
    review_body = {
        "skillName": "doc_reviewer",
        "stepIndex": 1,
        "sourceState": "model_success",
        "normalizedResult": {
            "document": "修订后正文。",
            "source": "model_success",
        },
        "resultList": [{"errorWord": "错", "rightWord": "对"}],
    }
    review_out.add_content(
        DataContent(
            delta=False,
            index=None,
            data=FunctionCallOutput(
                call_id="rev1",
                name="doc_reviewer",
                output=json.dumps(
                    [{"type": "json", "json": review_body}],
                    ensure_ascii=False,
                ),
            ).model_dump(exclude_none=True),
        ),
    )
    assistant = _msg(role="assistant", mtype=MessageType.MESSAGE)
    assistant.add_content(
        TextContent(delta=False, index=None, text="审核完成，请查看。"),
    )
    detail = build_conversation_detail(
        chat,
        [user.completed(), review_out.completed(), assistant.completed()],
    )
    assert detail["messages"][0]["content"]["text"] == "请帮我审核如下信息：原文。"
    assert len(detail["messages"]) == 3
    pipeline = detail["messages"][1]["content"]["steps"]
    assert len(pipeline) == 1
    assert pipeline[0]["skillName"] == "review"
    assert detail["messages"][2]["content"]["text"] == "审核完成，请查看。"


def test_agentloop_run_registry_register_and_resolve():
    async def _exercise() -> None:
        registry = AgentLoopRunRegistry()
        await registry.register("run-a", "conv-1")
        assert await registry.resolve_conversation("run-a") == "conv-1"
        assert await registry.resolve_conversation("missing") is None

    asyncio.run(_exercise())


def test_last_run_id_from_chat_meta():
    chat = ChatSpec(
        id="c1",
        name="t",
        session_id="agentloop:c1",
        user_id="u1",
        channel="console",
        meta={"lastRunId": "  run-xyz  "},
    )
    assert _last_run_id_from_chat(chat) == "run-xyz"
    assert _last_run_id_from_chat(
        ChatSpec(
            id="c2",
            name="t",
            session_id="s",
            user_id="u",
            channel="console",
        ),
    ) is None


def test_run_belongs_to_conversation_registry_meta_and_history():
    chat = ChatSpec(
        id="conv-1",
        name="t",
        session_id="agentloop:conv-1",
        user_id="u1",
        channel="console",
        meta={
            "lastRunId": "run-latest",
            "agentloopRunIds": ["run-old", "run-latest"],
        },
    )
    assert _run_belongs_to_conversation(
        chat,
        "run-latest",
        registry_conversation_id="conv-1",
    )
    assert _run_belongs_to_conversation(
        chat,
        "run-old",
        registry_conversation_id=None,
    )
    assert not _run_belongs_to_conversation(
        chat,
        "run-other",
        registry_conversation_id=None,
    )
