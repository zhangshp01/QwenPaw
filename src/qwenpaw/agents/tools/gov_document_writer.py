# -*- coding: utf-8 -*-
"""Formal / gov-style document helper for AgentLoop and govdoc-style tool names.

The hosted model often emits tool calls named ``gov-document-writer`` or
``公文写作``. Those names are registered in :class:`QwenPawAgent` via
``func_name=...`` so execution resolves instead of ``FunctionNotFoundError``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .file_io import _resolve_file_path, write_file


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


async def gov_document_writer(
    content: str = "",
    title: str = "",
    file_name: str = "",
    document_type: str = "",
    type: str = "",  # noqa: A002  # API / model payload uses key "type"
) -> ToolResponse:
    """Draft a formal notice-style document and save it under ``govdocs/``.

    Use when the user asks for 公文、通知、公告等正式文稿。Writes Markdown under
    the workspace ``govdocs/`` directory and returns the path plus a short
    preview.

    Args:
        content: Body text (headings and lists allowed).
        title: Document title shown at the top (optional if ``file_name`` set).
        file_name: Preferred base name for the file (optional).
        document_type: Category label e.g. ``通知`` (optional).
        type: Machine-oriented kind e.g. ``notice`` (optional; from some models).

    Returns:
        Path of the written file and a brief preview.
    """
    body = (content or "").strip()
    if not body:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "错误：缺少正文 content。请传入 title（可选）、"
                        "type 或 document_type（可选）、content（必填）。"
                    ),
                ),
            ],
        )

    heading = (title or "").strip()
    if not heading and (file_name or "").strip():
        heading = Path(file_name.strip()).stem
    if not heading:
        heading = "公文草稿"

    kind = (type or document_type or "").strip() or "notice"
    text = f"# {heading}\n\n**类型**: {kind}\n\n{body}\n"

    stem = _safe_stem(file_name or title or heading)
    rel_path = f"govdocs/{stem}.md"
    resolved = Path(_resolve_file_path(rel_path))
    resolved.parent.mkdir(parents=True, exist_ok=True)

    resp = await write_file(rel_path, text)
    preview = text if len(text) <= 4000 else text[:4000] + "\n\n…(已截断预览)"
    note = f"已保存: {resolved}\n\n{preview}"
    first_txt = _content_block_text(
        resp.content[0],
    ) if resp.content else None
    if first_txt and first_txt.startswith("Error"):
        return resp
    return ToolResponse(
        content=[TextBlock(type="text", text=note)],
    )
