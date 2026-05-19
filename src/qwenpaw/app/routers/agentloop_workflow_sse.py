# -*- coding: utf-8 -*-
"""Map AgentScope console SSE payloads to the AgentLoop workflow protocol.

Protocol summary (SSE ``text/event-stream``):
After ``message_start``, emit **paired** blocks only: each unit is
``content_block_start`` → optional ``content_block_delta`` (assistant ``type:
text`` streams deltas inside that single block) → ``content_block_stop``, then
the next block's ``content_block_start``, and so on. Close the assistant text
block before opening a ``tool_use`` block; finish each ``tool_use`` with
``content_block_stop`` before resuming assistant text. End with ``message_stop``
→ ``[DONE]``.

Whenever upstream sends ``status: in_progress`` for a tool/plan row, emit
``content_block_start`` (``tool_use``) so sub-agents / long steps are visible early;
for ``gov_document_writer``, also emit ``content_block_delta``
(``text_delta``) with incremental ``document`` text parsed from streaming tool
``arguments`` or ``output`` before ``content_block_stop``. ``doc_reviewer`` does
not stream document text (completion payload only).
``content_block_stop`` still carries the payload. If only ``completed`` is seen, start
and stop are emitted back-to-back as before. The workflow SSE uses ``compact=false`` passthrough so ``content``/``data`` tool **completed**
rows (with full ``output``) are not stripped before mapping. Identical repeat
completions are dropped by a content fingerprint. Shallow scaffold completions
(same ``call_id``, trivial JSON) may be deferred until a fuller completion
arrives or the stream ends. Empty plan completions are skipped once a non-empty
plan has been sent in the same run. After ``content_block_stop`` for a given
``(tool name, call_id)``, further ``in_progress`` rows for that key are ignored
to avoid duplicate orphan ``content_block_start`` lines from upstream reordering.
Stale deferred placeholders for the same tool name are dropped at stream end when
a substantive completion for that tool was already emitted (different ``call_id``).
Deferrable completions are also skipped when registering pending state if a
substantial completion for that tool name was already emitted (handles thin rows
that arrive after the rich one). At assistant paragraph completion and at stream
terminal, pending tool placeholders are flushed before the assistant text block is
closed so tool ``content_block_stop`` lines do not appear after the assistant summary.
"""

from __future__ import annotations

import json
from typing import Any

from qwenpaw.agents.tools.gov_document_stream import (
    document_from_tool_arguments,
    document_from_tool_root,
    document_stream_tool_names,
)

# Tools that should not surface as standalone workflow skills.
_SKIP_WORKFLOW_TOOL_NAMES = frozenset(
    {
        "update_subtask_state",
        "finish_subtask",
        "finish_plan",
    },
)

_PLANNING_TOOL_NAMES = frozenset({"create_plan", "revise_current_plan"})

# Default ``skillName`` for inferred gov-doc pipeline steps (positional fallback).
_GOV_ORDER_SKILLS: tuple[str, ...] = (
    "doc_retrieval",
    "gov_document_writer",
    "doc_reviewer",
    "gov_document_layout",
)

_DOCUMENT_STREAM_TOOLS = document_stream_tool_names()


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


def _completion_fingerprint(row: dict[str, Any]) -> str:
    """Stable string for deduping duplicate SSE envelopes (same tool + same body)."""
    name = row.get("name")
    if name in _PLANNING_TOOL_NAMES:
        raw = row.get("arguments")
        if isinstance(raw, str):
            return raw
        try:
            return json.dumps(raw, sort_keys=True, default=str)
        except TypeError:
            return str(raw)
    out = row.get("output")
    root = _tool_result_root(out)
    if root is not None:
        try:
            return json.dumps(root, sort_keys=True, ensure_ascii=False)
        except TypeError:
            return str(root)
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    try:
        return json.dumps(out, sort_keys=True, default=str)
    except TypeError:
        return str(out)


def _tool_payload_richness(root: dict[str, Any] | None) -> int:
    """Heuristic size score for tool JSON (higher => real payload, not scaffold)."""
    if not isinstance(root, dict):
        return 0
    score = 0
    for k in ("document", "items", "savePath"):
        v = root.get(k)
        if v is None:
            continue
        if k == "document" and isinstance(v, str) and v.strip():
            score += min(len(v), 20_000)
        elif k == "items" and isinstance(v, list) and len(v) > 0:
            score += 500 + 50 * len(v)
        elif k == "savePath" and isinstance(v, str) and v.strip():
            score += 1000
    nr = root.get("normalizedResult")
    if isinstance(nr, dict):
        for k in ("document", "items", "resultList", "templates"):
            v = nr.get(k)
            if v is None or v == [] or v == {}:
                continue
            if k == "document" and isinstance(v, str) and v.strip():
                score += min(len(v), 20_000)
            elif k == "items" and isinstance(v, list) and len(v) > 0:
                score += 500 + 50 * len(v)
            elif k in ("resultList", "templates"):
                if isinstance(v, list) and len(v) > 0:
                    score += 300 + 30 * len(v)
                elif isinstance(v, dict) and v:
                    score += 200
    rl = root.get("resultList")
    if isinstance(rl, list) and len(rl) > 0:
        score += 300 + 30 * len(rl)
    elif isinstance(rl, dict) and rl:
        score += 200
    return score


def _is_deferrable_placeholder_tool(name: str, root: dict[str, Any] | None) -> bool:
    """Hold this completion if a fuller ``plugin_call_output`` may follow (same call_id)."""
    if name in _SKIP_WORKFLOW_TOOL_NAMES or name in _PLANNING_TOOL_NAMES:
        return False
    if _tool_payload_richness(root) >= 80:
        return False
    if not isinstance(root, dict):
        return True
    try:
        si = int(root.get("stepIndex", 0))
    except (TypeError, ValueError):
        si = 0
    if si > 0:
        return False
    dt = str(root.get("displayText") or "").strip()
    generic = f"已完成：{name}"
    if dt and dt != generic:
        return False
    return True


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


def _normalize_declared_skill(raw: str) -> str | None:
    s = raw.strip().replace("-", "_").lower()
    if not s:
        return None
    for k in _GOV_ORDER_SKILLS:
        if s == k or s.endswith(k) or k in s:
            return k
    return None


def _skill_from_text(text: str) -> str | None:
    """Match gov pipeline skills; order matters (e.g. 审核 before 起草 in compound text)."""
    if not text:
        return None
    t = text.lower()
    if "doc_reviewer" in t or "审核" in text or "校对" in text:
        return "doc_reviewer"
    if "gov_document_layout" in t or "版式" in text or "排版" in text or "layout" in t:
        return "gov_document_layout"
    if "gov_document_writer" in t or "写作" in text or "起草" in text:
        return "gov_document_writer"
    if "doc_retrieval" in t or "检索" in text:
        return "doc_retrieval"
    return None


def _infer_protocol_skill_for_subtask(
    st: dict[str, Any],
    index_one_based: int,
) -> str:
    for key in ("skillName", "skill", "tool", "agent_tool"):
        v = st.get(key)
        if isinstance(v, str):
            hit = _normalize_declared_skill(v)
            if hit:
                return hit
    title = str(st.get("name") or st.get("title") or "")
    hit = _skill_from_text(title)
    if hit:
        return hit
    desc = str(st.get("description") or st.get("expected_outcome") or "")
    hit = _skill_from_text(desc)
    if hit:
        return hit
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
# that clients expect merged into workflow ``payload.normalizedResult``.
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
    on the root.     ``gov_document_layout`` sets ``normalizedResult.resultList`` to a flat list of
    template dicts and may keep ``savePath`` at the root. Produce one object for SSE ``payload.normalizedResult``.
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


def _document_from_stream_tool_row(row: dict[str, Any]) -> str:
    """Extract streamable document text from tool arguments or partial output."""
    root = _tool_result_root(row.get("output"))
    doc = document_from_tool_root(root)
    if doc:
        return doc
    if "arguments" in row:
        return document_from_tool_arguments(row.get("arguments"))
    return ""


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
    }
    return payload


class AgentLoopWorkflowSseTransformer:
    """Convert one AgentLoop passthrough batch (``data: {...}\\n\\n``) into
    workflow-protocol SSE text. Call :meth:`finish` when the upstream queue ends.
    """

    def __init__(self) -> None:
        self._terminal_emitted = False
        self._text_block_open = False
        self._last_text_key: str | None = None
        # Drop only bit-identical completion repeats (content envelope + plugin message).
        self._seen_completed_sigs: set[str] = set()
        # Scaffold completions (same call_id) may precede real tool output; defer weak ones.
        self._pending_tool_completion: dict[tuple[str, str], dict[str, Any]] = {}
        # Suppress trailing empty plan completions after a non-empty plan in the same run.
        self._emitted_nonempty_plan: bool = False
        # (tool_name, call_id): tool_use start already sent; completed must emit stop only.
        self._tool_use_started: set[tuple[str, str]] = set()
        # After emitting content_block_stop for a tool row, drop duplicate in_progress upstream.
        self._tool_completed_keys: set[tuple[str, str]] = set()
        # Tool names that already emitted a non-placeholder completion; stale deferred
        # placeholders for another call_id must not flush at stream end (duplicate stop).
        self._tool_emitted_substantial_completion: set[str] = set()
        # Per (tool name, call_id): chars of ``document`` already sent as deltas.
        self._tool_document_emitted_len: dict[tuple[str, str], int] = {}

    def _emit_error_content_block_sse(self, error_detail: str) -> str:
        """One paired ``tool_use`` block for passthrough errors (start + stop)."""
        parts: list[str] = []
        parts.append(self._emit_text_stop())
        parts.append(
            self._emit_tool_use_start(
                skill_name="error",
                display_text="运行错误",
            ),
        )
        parts.append(
            _wf_line(
                {
                    "type": "content_block_stop",
                    "payload": {
                        "tool": "error",
                        "skillName": "error",
                        "displayText": "运行错误",
                        "retryable": False,
                        "sourceState": "error",
                        "errorDetail": error_detail,
                        "normalizedResult": {
                            "source": "error",
                            "errorDetail": error_detail,
                        },
                    },
                },
            ),
        )
        return "".join(parts)

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

    def _emit_tool_document_deltas(self, *, tool_key: tuple[str, str], document: str) -> str:
        """Emit one ``text_delta`` per new Unicode character inside an open tool_use block."""
        if not document:
            return ""
        prev = self._tool_document_emitted_len.get(tool_key, 0)
        if len(document) <= prev:
            return ""
        parts: list[str] = []
        for ch in document[prev:]:
            parts.append(self._emit_text_delta(ch))
        self._tool_document_emitted_len[tool_key] = len(document)
        return "".join(parts)

    def _ensure_tool_use_started(
        self,
        *,
        tool_key: tuple[str, str],
        protocol_skill: str,
        root: dict[str, Any] | None,
        parts: list[str],
    ) -> None:
        if tool_key in self._tool_use_started:
            return
        start_label = self._tool_use_start_label(protocol_skill, root)
        parts.append(
            self._emit_tool_use_start(
                skill_name=protocol_skill,
                display_text=start_label,
            ),
        )
        self._tool_use_started.add(tool_key)

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

    def _emit_tool_use_start(self, *, skill_name: str, display_text: str) -> str:
        return _wf_line(
            {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "skillName": skill_name,
                    "displayText": display_text,
                },
            },
        )

    @staticmethod
    def _tool_use_start_label(protocol_skill: str, root: dict[str, Any] | None) -> str:
        if isinstance(root, dict):
            st = root.get("stepTitle")
            if isinstance(st, str) and st.strip() and st.strip() != protocol_skill:
                return f"进行中：{st.strip()}"
            dt = root.get("displayText")
            if isinstance(dt, str) and "已完成：" in dt:
                return dt.replace("已完成：", "进行中：", 1)
        return f"进行中：{protocol_skill}"

    def _format_completed_tool_sse(self, row: dict[str, Any]) -> str:
        """Emit ``content_block_stop``; add ``content_block_start`` if not sent at in_progress."""
        name = row["name"]
        call_id = row.get("call_id")
        cid = call_id if isinstance(call_id, str) and call_id else f"anon:{name}"
        protocol_skill = name
        tool_key = (name, cid)
        fp = _completion_fingerprint(row)
        sig = f"{name}:{cid}:{fp}"
        if sig in self._seen_completed_sigs:
            return ""
        self._seen_completed_sigs.add(sig)
        parts: list[str] = []
        parts.append(self._emit_text_stop())
        root = _tool_result_root(row.get("output"))
        fallback_started_key: tuple[str, str] | None = None
        if tool_key in self._tool_use_started:
            self._tool_use_started.discard(tool_key)
        else:
            # Upstream sometimes uses one ``call_id`` on ``in_progress`` and another on
            # ``completed``; avoid synthesizing a second ``content_block_start``.
            same_skill_open = [k for k in self._tool_use_started if k[0] == name]
            if len(same_skill_open) == 1:
                fallback_started_key = same_skill_open[0]
                self._tool_use_started.discard(fallback_started_key)
            else:
                start_label = self._tool_use_start_label(protocol_skill, root)
                parts.append(
                    self._emit_tool_use_start(
                        skill_name=protocol_skill,
                        display_text=start_label,
                    ),
                )
        if name in _DOCUMENT_STREAM_TOOLS:
            doc = _document_from_stream_tool_row(row)
            doc_key = tool_key
            if fallback_started_key is not None:
                doc_key = fallback_started_key
            parts.append(
                self._emit_tool_document_deltas(
                    tool_key=doc_key,
                    document=doc,
                ),
            )
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
        self._tool_completed_keys.add(tool_key)
        if fallback_started_key is not None:
            self._tool_completed_keys.add(fallback_started_key)
        if not _is_deferrable_placeholder_tool(name, root):
            self._tool_emitted_substantial_completion.add(name)
        return "".join(parts)

    def _flush_pending_tool_placeholders(self) -> str:
        """Emit deferred weak completions if no richer one arrived (stream end)."""
        if not self._pending_tool_completion:
            return ""
        chunks: list[str] = []
        for _k, row in list(self._pending_tool_completion.items()):
            name = row["name"]
            prow = _tool_result_root(row.get("output"))
            if (
                name in self._tool_emitted_substantial_completion
                and _is_deferrable_placeholder_tool(name, prow)
            ):
                continue
            chunks.append(self._format_completed_tool_sse(row))
        self._pending_tool_completion.clear()
        return "".join(chunks)

    def _close_skill_blocks(self, except_call_id: str | None) -> str:  # noqa: ARG002
        # Do not emit synthetic "interrupted" tool stops: we no longer track
        # in_progress call_ids (compact elision previously left orphan opens).
        return ""

    def _handle_tool_row(self, row: dict[str, Any]) -> str:
        name = row["name"]
        if name in _SKIP_WORKFLOW_TOOL_NAMES:
            return ""

        call_id = row.get("call_id")
        cid = call_id if isinstance(call_id, str) and call_id else f"anon:{name}"

        status = row.get("status")
        parts: list[str] = []
        tool_key = (name, cid)

        if name in _PLANNING_TOOL_NAMES:
            if status == "in_progress":
                if tool_key in self._tool_completed_keys:
                    return ""
                parts.append(self._emit_text_stop())
                if tool_key not in self._tool_use_started:
                    plan_label = (
                        "进行中：执行规划"
                        if name == "create_plan"
                        else "进行中：修订规划"
                    )
                    parts.append(
                        self._emit_tool_use_start(
                            skill_name="a2a_planning",
                            display_text=plan_label,
                        ),
                    )
                    self._tool_use_started.add(tool_key)
            if status == "completed":
                subtasks = _subtasks_from_create_plan_args(row.get("arguments"))
                if (
                    len(subtasks) == 0
                    and self._emitted_nonempty_plan
                ):
                    return ""
                fp = _completion_fingerprint(row)
                sig = f"{name}:{cid}:{fp}"
                if sig in self._seen_completed_sigs:
                    return ""
                self._seen_completed_sigs.add(sig)
                parts.append(self._emit_text_stop())
                plan_label = (
                    "进行中：执行规划"
                    if name == "create_plan"
                    else "进行中：修订规划"
                )
                if tool_key in self._tool_use_started:
                    self._tool_use_started.discard(tool_key)
                else:
                    parts.append(
                        self._emit_tool_use_start(
                            skill_name="a2a_planning",
                            display_text=plan_label,
                        ),
                    )
                plan_payload = _build_plan_payload(
                    tool_name=name,
                    subtasks=subtasks,
                )
                if plan_payload.get("steps", 0) > 0:
                    self._emitted_nonempty_plan = True
                parts.append(
                    _wf_line(
                        {"type": "content_block_stop", "payload": plan_payload},
                    ),
                )
                self._tool_completed_keys.add(tool_key)
            return "".join(parts)

        protocol_skill = name
        if status == "in_progress":
            if tool_key in self._tool_completed_keys:
                return ""
            parts.append(self._emit_text_stop())
            root_tip = _tool_result_root(row.get("output"))
            self._ensure_tool_use_started(
                tool_key=tool_key,
                protocol_skill=protocol_skill,
                root=root_tip,
                parts=parts,
            )
            if name in _DOCUMENT_STREAM_TOOLS:
                doc = _document_from_stream_tool_row(row)
                parts.append(
                    self._emit_tool_document_deltas(
                        tool_key=tool_key,
                        document=doc,
                    ),
                )
            return "".join(parts)

        if status == "completed":
            root = _tool_result_root(row.get("output"))
            if _is_deferrable_placeholder_tool(name, root):
                if name not in self._tool_emitted_substantial_completion:
                    self._pending_tool_completion[tool_key] = row
            else:
                self._pending_tool_completion.pop(tool_key, None)
                parts.append(self._format_completed_tool_sse(row))
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
            # Upstream may send only ``completed`` without ``in_progress``; keep
            # ``content_block_start`` paired before any ``content_block_delta``.
            if not self._text_block_open:
                parts.append(self._emit_text_start())
            parts.append(self._emit_text_delta(text))
            parts.append(self._flush_pending_tool_placeholders())
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
                out_chunks.append(
                    self._emit_error_content_block_sse(inner["error"]),
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
        # Unfinished planning is no longer tracked via call_id (see _close_skill_blocks).
        return ""

    def _terminal_payload(self) -> str:
        if self._terminal_emitted:
            return ""
        self._terminal_emitted = True
        tail = self._flush_pending_tool_placeholders()
        tail += self._emit_text_stop()
        tail += self._close_skill_blocks(except_call_id=None)
        tail += self._flush_unfinished_planning()
        tail += _wf_line({"type": "message_stop"})
        tail += "data: [DONE]\n\n"
        return tail

    def finish(self) -> str:
        """Flush open blocks and emit terminal markers if not already sent."""
        if self._terminal_emitted:
            return ""
        parts = self._flush_pending_tool_placeholders()
        parts += self._emit_text_stop()
        parts += self._close_skill_blocks(except_call_id=None)
        parts += self._flush_unfinished_planning()
        self._terminal_emitted = True
        parts += _wf_line({"type": "message_stop"})
        parts += "data: [DONE]\n\n"
        return parts
