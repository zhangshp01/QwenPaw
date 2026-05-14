# -*- coding: utf-8 -*-
"""Map AgentScope console SSE payloads to the AgentLoop workflow protocol.

Protocol summary (SSE ``text/event-stream``):
``message_start`` → ``content_block_*`` (planning + sub-agents) →
``message_stop`` → ``[DONE]``.
"""

from __future__ import annotations

import json
from typing import Any

# Tools that should not surface as standalone workflow skills.
_SKIP_WORKFLOW_TOOL_NAMES = frozenset(
    {
        "update_subtask_state",
        "finish_subtask",
        "finish_plan",
    },
)

_PLANNING_TOOL_NAMES = frozenset({"create_plan", "revise_current_plan"})

# Map internal tool names to protocol ``skillName`` / ``tool`` short ids.
_PROTOCOL_SKILL_BY_TOOL: dict[str, str] = {
    "create_plan": "a2a_planning",
    "revise_current_plan": "a2a_planning",
    "doc_retrieval": "retrieval",
    "gov_document_writer": "writing",
    "doc_reviewer": "doc_reviewer",
    "gov_document_layout": "gov_document_layout",
}

_GOV_ORDER_SKILLS: tuple[str, ...] = (
    "retrieval",
    "writing",
    "doc_reviewer",
    "gov_document_layout",
)


def _wf_line(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def workflow_message_start_sse() -> str:
    """First SSE line for a workflow-protocol run."""
    return _wf_line({"type": "message_start"})


def _parse_json_loose(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return None


def _normalize_plugin_message(d: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``plugin_call`` / ``plugin_call_output`` like compact AgentLoop."""
    if (
        d.get("object") != "message"
        or d.get("status") != "completed"
        or d.get("type") not in {"plugin_call", "plugin_call_output"}
    ):
        return d
    if isinstance(d.get("tool"), dict):
        return d
    raw_content = d.get("content")
    if not isinstance(raw_content, list) or not raw_content:
        return d
    first = raw_content[0]
    if not isinstance(first, dict):
        return d
    blob = first.get("data")
    if not isinstance(blob, dict):
        return d
    out = {k: v for k, v in d.items() if k != "content"}
    tool = {
        k: blob[k]
        for k in ("call_id", "name", "arguments", "output")
        if k in blob
    }
    if tool:
        out["tool"] = tool
    return out


def _extract_tool_row(d: dict[str, Any]) -> dict[str, Any] | None:
    """Return a uniform tool row for content ``data`` or flattened messages."""
    d = _normalize_plugin_message(d)
    if d.get("object") == "content" and d.get("type") == "data":
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return None
        return {
            "call_id": data.get("call_id"),
            "name": name,
            "arguments": data.get("arguments"),
            "output": data.get("output"),
            "status": d.get("status"),
        }
    if d.get("object") == "message" and isinstance(d.get("tool"), dict):
        t = d["tool"]
        name = t.get("name")
        if not isinstance(name, str) or not name:
            return None
        return {
            "call_id": t.get("call_id"),
            "name": name,
            "arguments": t.get("arguments"),
            "output": t.get("output"),
            "status": d.get("status"),
        }
    return None


def _tool_result_root(output: Any) -> dict[str, Any] | None:
    """Parse tool JSON body (object or text) from plugin / content output."""
    if isinstance(output, dict):
        if isinstance(output.get("json"), dict):
            return output["json"]
        content = output.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "json" and isinstance(item.get("json"), dict):
                    return item["json"]
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    inner = _parse_json_loose(item["text"])
                    if isinstance(inner, dict):
                        return inner
        if any(
            k in output
            for k in (
                "skillName",
                "normalizedResult",
                "sourceState",
                "resultList",
                "savePath",
                "displayText",
                "stepIndex",
            )
        ):
            return output
    parsed = _parse_json_loose(output)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "json" and isinstance(item.get("json"), dict):
                return item["json"]
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                inner = _parse_json_loose(item["text"])
                if isinstance(inner, dict):
                    return inner
    return None


def _subtasks_from_create_plan_args(arguments: Any) -> list[dict[str, Any]]:
    args = _parse_json_loose(arguments)
    if not isinstance(args, dict):
        return []
    raw = args.get("subtasks") or args.get("subtask")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for x in raw:
            if isinstance(x, dict):
                out.append(x)
            elif isinstance(x, str):
                inner = _parse_json_loose(x)
                if isinstance(inner, dict):
                    out.append(inner)
        return out
    return []


def _infer_protocol_skill_for_subtask(
    st: dict[str, Any],
    index_one_based: int,
) -> str:
    name = str(st.get("name") or st.get("title") or "").lower()
    desc = str(st.get("description") or "").lower()
    blob = name + " " + desc
    if "doc_retrieval" in blob or "检索" in blob:
        return "retrieval"
    if "gov_document_writer" in blob or "写作" in blob or "起草" in blob:
        return "writing"
    if "doc_reviewer" in blob or "审核" in blob or "校对" in blob:
        return "doc_reviewer"
    if "gov_document_layout" in blob or "layout" in blob or "版式" in blob:
        return "gov_document_layout"
    if 1 <= index_one_based <= len(_GOV_ORDER_SKILLS):
        return _GOV_ORDER_SKILLS[index_one_based - 1]
    return f"step_{index_one_based}"


def _build_plan_payload(
    *,
    tool_name: str,
    subtasks: list[dict[str, Any]],
) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    prev_ids: list[str] = []
    summary = (
        f"主 Agent 已规划 {len(subtasks)} 个 sub-agent 步骤。"
        if subtasks
        else "主 Agent 已完成任务规划。"
    )
    for i, st in enumerate(subtasks, start=1):
        skill = _infer_protocol_skill_for_subtask(st, i)
        title = str(st.get("name") or st.get("title") or f"步骤{i}")[:120]
        display_title = str(
            st.get("description") or st.get("expected_outcome") or title,
        )[:240]
        step_id = f"step_{i:02d}_{skill}"
        step = {
            "index": i,
            "skillName": skill,
            "title": title,
            "dependsOn": list(prev_ids),
            "subtaskRole": "skill_worker",
            "displayTitle": display_title,
        }
        steps_out.append(step)
        prev_ids = [step_id]

    return {
        "tool": "a2a_planning",
        "displayText": f"已规划 {len(steps_out)} 个执行步骤",
        "steps": len(steps_out),
        "plan": {
            "intent": "document_workflow",
            "summary": summary,
            "steps": steps_out,
        },
    }


# Fields stored on the gov tool JSON root (siblings of ``normalizedResult``)
# that clients expect in the workflow payload / ``result`` envelope.
_WORKFLOW_RESULT_KEYS_FROM_ROOT: frozenset[str] = frozenset(
    {
        "document",
        "resultList",
        "savePath",
        "items",
        "itemsTotal",
        "templates",
    },
)


def _build_workflow_result_envelope(root: dict[str, Any]) -> dict[str, Any]:
    """Merge ``normalizedResult`` with top-level tool fields (doc_reviewer, layout).

    ``doc_reviewer`` puts ``document`` under ``normalizedResult`` but ``resultList``
    on the root. ``gov_document_layout`` puts ``savePath`` and ``resultList`` on
    the root without ``normalizedResult``. Expose one consistent object for
    ``normalizedResult`` and ``result``.
    """
    out: dict[str, Any] = {}
    nr = root.get("normalizedResult")
    if isinstance(nr, dict):
        out.update(nr)
    src = str(root.get("sourceState") or root.get("source") or "model_success")
    for key in _WORKFLOW_RESULT_KEYS_FROM_ROOT:
        if key not in root:
            continue
        val = root[key]
        if val is not None and key not in out:
            out[key] = val
    out.setdefault("source", src)
    return out


def _stop_payload_from_tool_root(
    *,
    tool_name: str,
    protocol_skill: str,
    root: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(root, dict):
        root = {}
    raw_si = root.get("stepIndex")
    if raw_si is None:
        step_index_out: Any = 0
    else:
        try:
            step_index_out = int(raw_si)
        except (TypeError, ValueError):
            step_index_out = raw_si
    step_title = str(root.get("stepTitle") or protocol_skill)
    display_text = str(
        root.get("displayText") or f"已完成：{protocol_skill}",
    )
    retryable = bool(root.get("retryable", False))
    source_state = str(
        root.get("sourceState") or root.get("source") or "model_success",
    )
    err = root.get("errorDetail")
    if err is not None and not isinstance(err, str):
        err = str(err)

    envelope = _build_workflow_result_envelope(root)
    env_copy = dict(envelope)

    payload: dict[str, Any] = {
        "tool": protocol_skill,
        "skillName": protocol_skill,
        "stepIndex": step_index_out,
        "stepTitle": step_title,
        "displayText": display_text,
        "retryable": retryable,
        "sourceState": source_state,
        "errorDetail": err,
        "normalizedResult": env_copy,
        "result": dict(envelope),
    }
    if "resultList" in root and root.get("resultList") is not None:
        payload["resultList"] = root["resultList"]
    if "savePath" in root and root.get("savePath") is not None:
        payload["savePath"] = root["savePath"]
    return payload


class AgentLoopWorkflowSseTransformer:
    """Convert one AgentLoop passthrough batch (``data: {...}\\n\\n``) into
    workflow-protocol SSE text. Call :meth:`finish` when the upstream queue ends.
    """

    def __init__(self) -> None:
        self._terminal_emitted = False
        self._open_planning_call_ids: set[str] = set()
        self._open_skill_call_ids: dict[str, str] = {}  # call_id -> protocol skill
        self._text_block_open = False
        self._last_text_key: str | None = None

    def _emit_text_start(self) -> str:
        if self._text_block_open:
            return ""
        self._text_block_open = True
        return _wf_line(
            {
                "type": "content_block_start",
                "content_block": {
                    "type": "text",
                    "skillName": "assistant",
                    "displayText": "助手输出",
                },
            },
        )

    def _emit_text_delta(self, text: str) -> str:
        if not text:
            return ""
        return _wf_line(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _emit_text_stop(self) -> str:
        if not self._text_block_open:
            return ""
        self._text_block_open = False
        return _wf_line(
            {
                "type": "content_block_stop",
                "payload": {
                    "tool": "assistant",
                    "skillName": "assistant",
                    "displayText": "已完成：文本输出",
                    "retryable": False,
                    "sourceState": "model_success",
                    "errorDetail": None,
                    "normalizedResult": {"source": "model_success"},
                },
            },
        )

    def _close_skill_blocks(self, except_call_id: str | None) -> str:
        parts: list[str] = []
        for cid, skill in list(self._open_skill_call_ids.items()):
            if except_call_id and cid == except_call_id:
                continue
            parts.append(
                _wf_line(
                    {
                        "type": "content_block_stop",
                        "payload": _stop_payload_from_tool_root(
                            tool_name=skill,
                            protocol_skill=skill,
                            root={
                                "displayText": f"已中断：{skill}",
                                "sourceState": "error",
                                "errorDetail": "stream_end",
                                "normalizedResult": {
                                    "source": "error",
                                    "errorDetail": "stream_end",
                                },
                            },
                        ),
                    },
                ),
            )
            del self._open_skill_call_ids[cid]
        return "".join(parts)

    def _handle_tool_row(self, row: dict[str, Any]) -> str:
        name = row["name"]
        if name in _SKIP_WORKFLOW_TOOL_NAMES:
            return ""

        call_id = row.get("call_id")
        cid = call_id if isinstance(call_id, str) and call_id else f"anon:{name}"

        status = row.get("status")
        parts: list[str] = []

        if name in _PLANNING_TOOL_NAMES:
            proto = "a2a_planning"
            if status == "in_progress" and cid not in self._open_planning_call_ids:
                self._open_planning_call_ids.add(cid)
                parts.append(self._emit_text_stop())
                label = (
                    "进行中：执行规划"
                    if name == "create_plan"
                    else "进行中：修订规划"
                )
                parts.append(
                    _wf_line(
                        {
                            "type": "content_block_start",
                            "content_block": {
                                "type": "tool_use",
                                "skillName": proto,
                                "displayText": label,
                            },
                        },
                    ),
                )
            if status == "completed":
                if cid not in self._open_planning_call_ids:
                    parts.append(self._emit_text_stop())
                    label = (
                        "进行中：执行规划"
                        if name == "create_plan"
                        else "进行中：修订规划"
                    )
                    parts.append(
                        _wf_line(
                            {
                                "type": "content_block_start",
                                "content_block": {
                                    "type": "tool_use",
                                    "skillName": proto,
                                    "displayText": label,
                                },
                            },
                        ),
                    )
                    self._open_planning_call_ids.add(cid)
                subtasks = _subtasks_from_create_plan_args(row.get("arguments"))
                plan_payload = _build_plan_payload(
                    tool_name=name,
                    subtasks=subtasks,
                )
                parts.append(
                    _wf_line(
                        {"type": "content_block_stop", "payload": plan_payload},
                    ),
                )
                self._open_planning_call_ids.discard(cid)
            return "".join(parts)

        protocol_skill = _PROTOCOL_SKILL_BY_TOOL.get(name, name)
        if status == "in_progress" and cid not in self._open_skill_call_ids:
            parts.append(self._emit_text_stop())
            parts.append(
                _wf_line(
                    {
                        "type": "content_block_start",
                        "content_block": {
                            "type": "tool_use",
                            "skillName": protocol_skill,
                            "displayText": f"进行中：{protocol_skill}",
                        },
                    },
                ),
            )
            self._open_skill_call_ids[cid] = protocol_skill

        if status == "completed":
            if cid not in self._open_skill_call_ids:
                parts.append(self._emit_text_stop())
                parts.append(
                    _wf_line(
                        {
                            "type": "content_block_start",
                            "content_block": {
                                "type": "tool_use",
                                "skillName": protocol_skill,
                                "displayText": f"进行中：{protocol_skill}",
                            },
                        },
                    ),
                )
                self._open_skill_call_ids[cid] = protocol_skill
            root = _tool_result_root(row.get("output"))
            self._open_skill_call_ids.pop(cid, None)
            parts.append(
                _wf_line(
                    {
                        "type": "content_block_stop",
                        "payload": _stop_payload_from_tool_root(
                            tool_name=name,
                            protocol_skill=protocol_skill,
                            root=root,
                        ),
                    },
                ),
            )
        return "".join(parts)

    def _handle_content_text(self, d: dict[str, Any]) -> str:
        if d.get("object") != "content" or d.get("type") != "text":
            return ""
        status = d.get("status")
        text = d.get("text")
        if not isinstance(text, str) or not text:
            return ""

        parts: list[str] = []
        if status == "in_progress":
            msg_id = str(d.get("msg_id") or "")
            key = f"{msg_id}:{hash(text) & 0xFFFF}"
            if self._last_text_key != key:
                parts.append(self._emit_text_start())
                self._last_text_key = key
            parts.append(self._emit_text_delta(text))
        elif status == "completed":
            parts.append(self._emit_text_delta(text))
            parts.append(self._emit_text_stop())
            self._last_text_key = None
        return "".join(parts)

    def consume_passthrough_batch(self, batch: str) -> str:
        """Translate a passthrough chunk (may contain ``data: [DONE]``)."""
        out_chunks: list[str] = []
        saw_done = False
        for block in batch.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            if block == "data: [DONE]" or (
                block.startswith("data:") and "[DONE]" in block
            ):
                saw_done = True
                continue
            if not block.startswith("data:"):
                continue
            payload_s = block[5:].strip()
            try:
                inner: Any = json.loads(payload_s)
            except json.JSONDecodeError:
                continue
            if not isinstance(inner, dict):
                continue
            if isinstance(inner.get("error"), str):
                out_chunks.append(self._emit_text_stop())
                out_chunks.append(
                    _wf_line(
                        {
                            "type": "content_block_stop",
                            "payload": {
                                "tool": "error",
                                "skillName": "error",
                                "displayText": "运行错误",
                                "retryable": False,
                                "sourceState": "error",
                                "errorDetail": inner.get("error"),
                                "normalizedResult": {
                                    "source": "error",
                                    "errorDetail": inner.get("error"),
                                },
                            },
                        },
                    ),
                )
                continue

            row = _extract_tool_row(inner)
            if row:
                out_chunks.append(self._handle_tool_row(row))
            else:
                out_chunks.append(self._handle_content_text(inner))

        if saw_done:
            out_chunks.append(self._terminal_payload())
        return "".join(out_chunks)

    def _flush_unfinished_planning(self) -> str:
        if not self._open_planning_call_ids:
            return ""
        self._open_planning_call_ids.clear()
        return _wf_line(
            {
                "type": "content_block_stop",
                "payload": {
                    "tool": "a2a_planning",
                    "displayText": "规划阶段已结束（流中断）",
                    "steps": 0,
                    "plan": {
                        "intent": "document_workflow",
                        "summary": "上游连接在规划完成前结束。",
                        "steps": [],
                    },
                },
            },
        )

    def _terminal_payload(self) -> str:
        if self._terminal_emitted:
            return ""
        self._terminal_emitted = True
        tail = self._emit_text_stop()
        tail += self._close_skill_blocks(except_call_id=None)
        tail += self._flush_unfinished_planning()
        tail += _wf_line({"type": "message_stop"})
        tail += "data: [DONE]\n\n"
        return tail

    def finish(self) -> str:
        """Flush open blocks and emit terminal markers if not already sent."""
        if self._terminal_emitted:
            return ""
        parts = self._emit_text_stop()
        parts += self._close_skill_blocks(except_call_id=None)
        parts += self._flush_unfinished_planning()
        self._terminal_emitted = True
        parts += _wf_line({"type": "message_stop"})
        parts += "data: [DONE]\n\n"
        return parts
