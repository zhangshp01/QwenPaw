# -*- coding: utf-8 -*-
"""Build govdoc-style conversation detail from AgentScope runtime messages."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscope_runtime.engine.schemas.agent_schemas import Message

_MSG_TYPE_MESSAGE = "message"
_MSG_TYPE_PLUGIN_CALL = "plugin_call"
_MSG_TYPE_PLUGIN_OUTPUT = "plugin_call_output"

from ..runner.models import ChatSpec
from .agentloop_workflow_sse import (
    _PLANNING_TOOL_NAMES,
    _SKIP_WORKFLOW_TOOL_NAMES,
    _build_workflow_result_envelope,
    _infer_protocol_skill_for_subtask,
    _subtasks_from_create_plan_args,
    _tool_payload_richness,
    _tool_result_root,
)

_GOV_PIPELINE_TOOLS = frozenset(
    {
        "doc_retrieval",
        "doc-retrieval",
        "gov_document_writer",
        "gov-document-writer",
        "doc_reviewer",
        "gov_document_layout",
    },
)

_SKILL_ALIASES: dict[str, tuple[str, str]] = {
    "doc_retrieval": ("retrieval", "资料检索"),
    "doc-retrieval": ("retrieval", "资料检索"),
    "gov_document_writer": ("writing", "公文写作"),
    "gov-document-writer": ("writing", "公文写作"),
    "doc_reviewer": ("review", "文档审核"),
    "gov_document_layout": ("layout", "公文排版"),
}

_PLAN_PREFIX_RE = re.compile(r"^\s*/plan(?:\s+|$)", re.IGNORECASE)
_SKILL_PREFIX_RE = re.compile(r"^\s*\[skill:[^\]]+\]\s*", re.IGNORECASE)
_CONTINUATION_RE = re.compile(r"\n\[continuation:[^\]]+\]\s*$")


def _message_type_value(message: Message) -> str:
    raw = getattr(message, "type", None)
    if raw is None:
        return ""
    return raw.value if hasattr(raw, "value") else str(raw)


def _message_metadata(message: Message) -> dict[str, Any]:
    meta = getattr(message, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _message_id(message: Message, *, fallback: str) -> str:
    meta = _message_metadata(message)
    raw = meta.get("original_id") or getattr(message, "id", None) or fallback
    text = str(raw)
    if text.startswith("msg_"):
        text = text[4:]
    return text or fallback


def _first_data_blob(message: Message) -> dict[str, Any] | None:
    for block in message.content or []:
        data = getattr(block, "data", None)
        if isinstance(data, dict):
            return data
    return None


def _message_plain_text(message: Message) -> str:
    chunks: list[str] = []
    for block in message.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _clean_user_display_text(text: str) -> str:
    s = (text or "").strip()
    s = _SKILL_PREFIX_RE.sub("", s)
    while True:
        m = _PLAN_PREFIX_RE.match(s)
        if not m:
            break
        s = s[m.end() :].lstrip()
    s = _CONTINUATION_RE.sub("", s)
    return s.strip()


def _format_created_at(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _skill_alias(tool_name: str) -> tuple[str, str]:
    key = (tool_name or "").strip()
    if key in _SKILL_ALIASES:
        return _SKILL_ALIASES[key]
    return key.replace("-", "_"), key


def _plan_titles_by_skill(
    subtasks: list[dict[str, Any]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, st in enumerate(subtasks, start=1):
        skill = _infer_protocol_skill_for_subtask(st, i)
        title = str(
            st.get("name") or st.get("title") or st.get("description") or "",
        ).strip()
        if title and skill not in out:
            out[skill] = title[:120]
    return out


def _task_id_prefix(conversation_id: str) -> str:
    head = conversation_id.split("-", 1)[0]
    return head or conversation_id[:8]


class _TurnAccumulator:
    def __init__(self, *, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.task_prefix = _task_id_prefix(conversation_id)
        self.task_counter = 0
        self.plan_subtasks: list[dict[str, Any]] = []
        self.plan_titles: dict[str, str] = {}
        self.tool_roots: dict[tuple[str, str], dict[str, Any]] = {}
        self.tool_order: list[tuple[str, str]] = []

    def reset(self) -> None:
        self.plan_subtasks = []
        self.plan_titles = {}
        self.tool_roots = {}
        self.tool_order = []

    def ingest_plugin_call(self, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "")
        if name not in _PLANNING_TOOL_NAMES:
            return
        subtasks = _subtasks_from_create_plan_args(data.get("arguments"))
        if subtasks:
            self.plan_subtasks = subtasks
            self.plan_titles = _plan_titles_by_skill(subtasks)

    def ingest_plugin_output(self, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "")
        if (
            not name
            or name in _SKIP_WORKFLOW_TOOL_NAMES
            or name in _PLANNING_TOOL_NAMES
        ):
            return
        if name not in _GOV_PIPELINE_TOOLS:
            return
        call_id = str(data.get("call_id") or name)
        root = _tool_result_root(data.get("output"))
        if not isinstance(root, dict):
            return
        key = (name, call_id)
        prev = self.tool_roots.get(key)
        if prev is None or _tool_payload_richness(root) >= _tool_payload_richness(
            prev,
        ):
            if key not in self.tool_roots:
                self.tool_order.append(key)
            self.tool_roots[key] = root

    def has_pipeline_steps(self) -> bool:
        return bool(self.tool_roots)

    def build_steps(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for idx, key in enumerate(self.tool_order, start=1):
            name, _call_id = key
            root = self.tool_roots.get(key)
            if not isinstance(root, dict):
                continue
            skill_key = name.replace("-", "_")
            alias, default_title = _skill_alias(name)
            title = self.plan_titles.get(skill_key) or self.plan_titles.get(
                name,
            ) or default_title
            try:
                step_index = int(root.get("stepIndex") or idx)
            except (TypeError, ValueError):
                step_index = idx
            self.task_counter += 1
            source_state = str(
                root.get("sourceState") or root.get("source") or "model_success",
            )
            err = root.get("errorDetail")
            if err is not None and not isinstance(err, str):
                err = str(err)
            envelope = _build_workflow_result_envelope(root)
            steps.append(
                {
                    "index": step_index,
                    "taskId": f"task_{self.task_prefix}_{self.task_counter:04d}",
                    "skillName": alias,
                    "title": title,
                    "normalizedResult": envelope,
                    "sourceState": source_state,
                    "errorDetail": err if err else None,
                },
            )
        steps.sort(key=lambda s: int(s.get("index") or 0))
        for i, step in enumerate(steps, start=1):
            step["index"] = i
        return steps

    def plan_summary(self) -> str | None:
        if self.plan_subtasks:
            n = len(self.plan_subtasks)
            return f"主 Agent 已规划 {n} 个 sub-agent 步骤。"
        if self.tool_roots:
            return f"主 Agent 已规划 {len(self.tool_order)} 个 sub-agent 步骤。"
        return None


def _artifact_from_layout_step(step: dict[str, Any]) -> dict[str, Any] | None:
    nr = step.get("normalizedResult")
    if not isinstance(nr, dict):
        return None
    save_path = nr.get("savePath")
    if not isinstance(save_path, str) or not save_path.strip():
        return None
    path = Path(save_path)
    return {
        "id": str(uuid.uuid4()),
        "title": path.name or "公文写作结果.docx",
        "artifactType": "document",
        "summary": "已生成可继续编辑的公文正文。",
        "workspaceNodeId": None,
    }


def build_conversation_detail(
    chat: ChatSpec,
    runtime_messages: list[Message],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Transform ``/api/chats/{id}`` messages into govdoc conversation detail."""
    out_messages: list[dict[str, Any]] = []
    turn = _TurnAccumulator(conversation_id=chat.id)
    user_ts = _format_created_at(chat.created_at)
    assistant_ts = _format_created_at(chat.updated_at)

    def flush_pipeline_assistant() -> None:
        if not turn.has_pipeline_steps():
            return
        steps = turn.build_steps()
        content: dict[str, Any] = {
            "steps": steps,
        }
        if model:
            content["model"] = model
        summary = turn.plan_summary()
        if summary:
            content["planIntent"] = "document_workflow"
            content["planSummary"] = summary
        out_messages.append(
            {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": content,
                "createdAt": assistant_ts,
            },
        )
        turn.reset()

    for seq, message in enumerate(runtime_messages):
        role = message.role or "assistant"
        mtype = _message_type_value(message)

        if role == "user" and mtype == _MSG_TYPE_MESSAGE:
            flush_pipeline_assistant()
            text = _clean_user_display_text(_message_plain_text(message))
            if not text:
                continue
            out_messages.append(
                {
                    "id": _message_id(message, fallback=f"user-{seq}"),
                    "role": "user",
                    "content": {"text": text},
                    "createdAt": user_ts,
                },
            )
            continue

        if mtype == _MSG_TYPE_PLUGIN_CALL:
            data = _first_data_blob(message)
            if data:
                turn.ingest_plugin_call(data)
            continue

        if mtype == _MSG_TYPE_PLUGIN_OUTPUT:
            data = _first_data_blob(message)
            if data:
                turn.ingest_plugin_output(data)
            continue

        if role == "assistant" and mtype == _MSG_TYPE_MESSAGE:
            text = _message_plain_text(message).strip()
            if turn.has_pipeline_steps():
                flush_pipeline_assistant()
            if text:
                item: dict[str, Any] = {
                    "id": _message_id(message, fallback=f"assistant-{seq}"),
                    "role": "assistant",
                    "content": {"text": text},
                    "createdAt": assistant_ts,
                }
                if model:
                    item["content"]["model"] = model
                out_messages.append(item)

    flush_pipeline_assistant()

    artifacts: list[dict[str, Any]] = []
    for msg in out_messages:
        content = msg.get("content")
        if not isinstance(content, dict):
            continue
        for step in content.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("skillName") != "layout":
                continue
            art = _artifact_from_layout_step(step)
            if art:
                artifacts.append(art)

    return {
        "id": chat.id,
        "title": chat.name,
        "messages": out_messages,
        "artifacts": artifacts,
    }
