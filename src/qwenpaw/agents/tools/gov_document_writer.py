# -*- coding: utf-8 -*-
"""Formal / gov-style document draft (``gov_document_writer``) and review
(``doc_reviewer``) helpers for AgentLoop.

``gov_document_writer`` and ``doc_reviewer`` return one
``{"type": "json", "json": {...}}`` block (``skillName``, ``stepIndex``,
``displayText``, ``normalizedResult``, …) aligned with doc-retrieval style
envelopes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agentscope.tool import ToolResponse

from ...config.context import (
    get_agent_tool_step_for_running_task,
    get_current_recent_max_bytes,
)
from .file_io import _resolve_file_path, read_file
from .utils import DEFAULT_MAX_BYTES, truncate_text_output

_SKILL_NAME_EN = "gov-document-writer"
_SKILL_NAME_DOC_REVIEWER_EN = "doc_reviewer"
_DISPLAY_OK_ZH = "已完成：公文写作"
_DISPLAY_ERR_ZH = "公文写作失败"
_DISPLAY_OK_DOC_REVIEW_ZH = "已完成：文档审核"
_DISPLAY_ERR_DOC_REVIEW_ZH = "文档审核失败"
_SOURCE_OK = "model_success"
_SOURCE_ERR = "error"
_FALLBACK_STEP_INDEX = 1

logger = logging.getLogger(__name__)

_DOC_REVIEW_LLM_TIMEOUT = 120.0
_DOC_REVIEW_MAX_USER_CHARS = 48_000
_DOC_REVIEW_SYSTEM_ZH = (
    "你是中文文档校对与润色助手。用户将提供一段待审正文（可能含错别字或表述问题）。\n"
    "请只做一件事：输出**审核修改后的完整正文**（可直接替换原文使用的定稿）。\n"
    "要求：\n"
    "1）修正错别字、明显用词与标点错误；可在不改变原意的前提下轻微润色；\n"
    "2）尽量保留原文段落换行与层次，不要擅自改成分析报告或条目清单；\n"
    "3）不要输出「审核说明」「修改意见」等元话语，不要复述任务要求；\n"
    "4）不要使用 Markdown 代码围栏（不要输出 ```）。"
)


def _content_block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("text")
    return getattr(block, "text", None)


def _safe_stem(name: str, max_len: int = 80) -> str:
    name = (name or "").strip() or "document"
    name = Path(name).name
    cleaned = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
    cleaned = cleaned.strip(" .") or "document"
    return cleaned[:max_len]


def _docx_basename(heading: str) -> str:
    """``{公文标题}-{时间戳}.docx`` (timestamp: local ``%Y%m%d%H%M%S`` + microseconds)."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{_safe_stem(heading)}-{ts}.docx"


def _write_gov_docx(
    path: Path,
    heading: str,
    body: str,
    *,
    type_label: str = "",
) -> None:
    """Blocking: build a minimal .docx (requires ``python-docx``).

    Optional ``type_label`` (e.g. 中文「通知」) is shown as one line
    ``类型：…``; omitted when empty so machine tokens like ``notice`` are not
    injected into formal text.
    """
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "缺少依赖 python-docx，无法生成 .docx。请在运行环境中安装：pip install python-docx",
        ) from exc

    doc = Document()
    doc.add_heading(heading, level=0)
    label = (type_label or "").strip()
    if label:
        doc.add_paragraph(f"类型：{label}")
    for line in body.split("\n"):
        doc.add_paragraph(line.rstrip("\r"))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def _writer_step_index() -> int:
    """1-based tool order in the current agent ``reply`` (toolkit middleware)."""
    step = get_agent_tool_step_for_running_task()
    return int(step) if step is not None else _FALLBACK_STEP_INDEX


def _gov_writer_json(root: dict[str, Any]) -> ToolResponse:
    return ToolResponse(content=[{"type": "json", "json": root}])


def _gov_writer_error(detail: str) -> ToolResponse:
    detail = (detail or "").strip() or "unknown"
    return _gov_writer_json(
        {
            "skillName": _SKILL_NAME_EN,
            "stepIndex": _writer_step_index(),
            "displayText": _DISPLAY_ERR_ZH,
            "retryable": True,
            "sourceState": _SOURCE_ERR,
            "errorDetail": detail,
            "savePath": None,
            "normalizedResult": None,
        },
    )


def _doc_reviewer_json(root: dict[str, Any]) -> ToolResponse:
    return ToolResponse(content=[{"type": "json", "json": root}])


def _doc_reviewer_error(detail: str) -> ToolResponse:
    detail = (detail or "").strip() or "unknown"
    return _doc_reviewer_json(
        {
            "skillName": _SKILL_NAME_DOC_REVIEWER_EN,
            "stepIndex": _writer_step_index(),
            "displayText": _DISPLAY_ERR_DOC_REVIEW_ZH,
            "retryable": True,
            "sourceState": _SOURCE_ERR,
            "errorDetail": detail,
            "normalizedResult": None,
        },
    )


def _doc_reviewer_ok(document: str) -> ToolResponse:
    return _doc_reviewer_json(
        {
            "skillName": _SKILL_NAME_DOC_REVIEWER_EN,
            "stepIndex": _writer_step_index(),
            "displayText": _DISPLAY_OK_DOC_REVIEW_ZH,
            "retryable": True,
            "sourceState": _SOURCE_OK,
            "errorDetail": None,
            "normalizedResult": {
                "document": document,
                "source": _SOURCE_OK,
            },
        },
    )


def _strip_outer_code_fence(text: str) -> str:
    """If the model wrapped the body in a Markdown fence, return inner text."""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if not lines:
        return t
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def _run_document_review_llm(draft: str) -> str:
    """Call the active chat model once to produce the revised document body."""
    from agentscope_runtime.engine.schemas.exception import AppBaseException

    from ...app.agent_context import get_current_agent_id
    from ...app.runner.title_generator import _consume_model_response
    from ..model_factory import create_model_and_formatter

    trimmed = (draft or "").strip()
    if len(trimmed) > _DOC_REVIEW_MAX_USER_CHARS:
        trimmed = trimmed[:_DOC_REVIEW_MAX_USER_CHARS].rstrip() + "\n\n[…正文已截断…]"

    agent_id = get_current_agent_id()
    try:
        model, _ = create_model_and_formatter(agent_id=agent_id)
    except (ValueError, AppBaseException, OSError) as exc:
        raise RuntimeError(
            "未配置可用的对话模型，无法生成修改后正文。",
        ) from exc

    messages = [
        {"role": "system", "content": _DOC_REVIEW_SYSTEM_ZH},
        {
            "role": "user",
            "content": f"以下为待审正文，请只输出修改定稿后的完整正文：\n\n{trimmed}",
        },
    ]
    try:
        revised = await asyncio.wait_for(
            _consume_model_response(model, messages),
            timeout=_DOC_REVIEW_LLM_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"文档审核超时（{_DOC_REVIEW_LLM_TIMEOUT:.0f}s）。",
        ) from exc

    revised = _strip_outer_code_fence(revised or "")
    if not revised.strip():
        raise RuntimeError("模型未返回修改后正文。")
    return revised.strip()


async def doc_reviewer(
    content: str = "",
    file_path: str = "",
    path: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResponse:
    """Inline or file-based draft for review (校对、审阅、定稿).

    Exposed to the model as ``doc_reviewer``. Pass ``content`` and/or
    ``file_path`` / ``path`` (same resolution as :func:`read_file`).

    Returns a JSON envelope; ``normalizedResult.document`` is the
    **审核修改后正文** (LLM-revised full text), not a separate audit memo.
    """
    body = (content or "").strip()
    max_bytes = get_current_recent_max_bytes() or DEFAULT_MAX_BYTES
    if body:
        total_lines = body.count("\n") + (1 if body else 0)
        draft = truncate_text_output(
            body,
            start_line=1,
            total_lines=total_lines,
            max_bytes=max_bytes,
            file_path="<inline-review>",
        )
    else:
        target = (file_path or path or "").strip()
        if not target:
            return _doc_reviewer_error(
                "错误：缺少待审阅内容。请传入 content（正文），"
                "或 file_path / path（工作区文稿路径）。",
            )
        read_resp = await read_file(target, start_line=start_line, end_line=end_line)
        block0 = read_resp.content[0] if read_resp.content else None
        draft = _content_block_text(block0) or ""
        if draft.startswith("Error:"):
            return _doc_reviewer_error(draft)

    try:
        revised = await _run_document_review_llm(draft)
    except Exception as exc:  # noqa: BLE001
        logger.warning("doc_reviewer: revise LLM failed: %s", exc)
        return _doc_reviewer_error(str(exc))

    out_lines = revised.count("\n") + (1 if revised else 0)
    document_out = truncate_text_output(
        revised,
        start_line=1,
        total_lines=out_lines,
        max_bytes=max_bytes,
        file_path="<revised-document>",
    )
    return _doc_reviewer_ok(document_out)


async def gov_document_writer(
    content: str = "",
    title: str = "",
    file_name: str = "",
    document_type: str = "",
    type: str = "",  # noqa: A002  # API / model payload uses key "type"
) -> ToolResponse:
    """Draft a formal notice-style document and save it under ``govdocs/``.

    Use when the user asks for 公文、通知、公告等正式文稿。Writes a ``.docx`` file
    named ``{标题}-{时间戳}.docx`` under ``govdocs/`` and returns the JSON envelope
    (``savePath`` points at the ``.docx``; ``normalizedResult.document`` is
    plain ``标题 + 正文`` without Markdown or machine ``type`` lines).

    Args:
        content: Body text (headings and lists allowed).
        title: Document title shown at the top (optional if ``file_name`` set).
        file_name: Preferred base name for the file (optional).
        document_type: Category label e.g. ``通知`` (optional).
        type: Machine-oriented kind e.g. ``notice`` (optional; from some models).

    Returns:
        ``ToolResponse`` with one ``type: json`` block (``skillName``,
        ``stepIndex``, ``savePath``, ``normalizedResult.document``, …).
    """
    body = (content or "").strip()
    if not body:
        return _gov_writer_error(
            "错误：缺少正文 content。请传入 title（可选）、"
            "type 或 document_type（可选）、content（必填）。",
        )

    heading = (title or "").strip()
    if not heading and (file_name or "").strip():
        heading = Path(file_name.strip()).stem
    if not heading:
        heading = "公文草稿"

    # Human-facing type line (docx + optional UX): prefer Chinese document_type;
    # do not default to English ``notice`` in body output—that was only legacy
    # Markdown metadata and reads wrong in 公文.
    doc_type_zh = (document_type or "").strip()
    machine_type = (type or "").strip()
    if doc_type_zh:
        type_label = doc_type_zh
    elif machine_type and machine_type.lower() != "notice":
        type_label = machine_type
    else:
        type_label = ""

    document_plain = f"{heading}\n\n{body}".strip() + "\n"

    rel_path = f"govdocs/{_docx_basename(heading)}"
    resolved = Path(_resolve_file_path(rel_path))

    try:
        await asyncio.to_thread(
            _write_gov_docx,
            resolved,
            heading,
            body,
            type_label=type_label,
        )
    except Exception as e:
        return _gov_writer_error(str(e))
    save_path = str(resolved.resolve())
    return _gov_writer_json(
        {
            "skillName": _SKILL_NAME_EN,
            "stepIndex": _writer_step_index(),
            "displayText": _DISPLAY_OK_ZH,
            "retryable": True,
            "sourceState": _SOURCE_OK,
            "errorDetail": None,
            "savePath": save_path,
            "normalizedResult": {
                "document": document_plain,
                "source": _SOURCE_OK,
            },
        },
    )
