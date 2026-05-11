# -*- coding: utf-8 -*-
"""HaiRuo knowledge-base retrieval (``doc_retrieval`` / ``doc-retrieval``).

Loads ``search`` from the ``doc-retrieval`` skill ``scripts/`` folder when
present under workspace skills, working dir, or project ``doc-retrieval/``.

Tool output is one content block ``{"type": "json", "json": {skillName, ...}}``
(flat object, no extra ``payload`` wrapper).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from agentscope.tool import ToolResponse

from ...constant import WORKING_DIR

_SKILL_NAME_EN = "doc-retrieval"
_DISPLAY_TEXT_ZH = "已完成：知识库文档检索"
_DISPLAY_TEXT_ERR_ZH = "知识库文档检索失败"
_LEGACY_SOURCE_OK = "legacy_success"
_DESC_MAX_CHARS = 8000

_search_module_cache: dict[str, Any] = {}


def _find_doc_retrieval_scripts_dir(workspace_dir: Path | None) -> Path | None:
    """Return ``.../doc-retrieval/scripts`` if ``search.py`` exists."""
    candidates: list[Path] = []
    if workspace_dir:
        ws = Path(workspace_dir).resolve()
        candidates.append(ws / "skills" / "doc-retrieval" / "scripts")
        candidates.append(ws / "doc-retrieval" / "scripts")
    wd = Path(WORKING_DIR).resolve()
    candidates.append(wd / "skills" / "doc-retrieval" / "scripts")
    candidates.append(wd / "doc-retrieval" / "scripts")
    here = Path(__file__).resolve()
    if len(here.parents) >= 5:
        candidates.append(here.parents[4] / "doc-retrieval" / "scripts")
    for p in candidates:
        if (p / "search.py").is_file():
            return p
    return None


def _get_search_module(scripts_dir: Path) -> Any:
    key = str(scripts_dir.resolve())
    if key in _search_module_cache:
        return _search_module_cache[key]
    path = scripts_dir / "search.py"
    mod_name = f"qwenpaw_doc_retrieval_{hash(key) & 0xFFFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _search_module_cache[key] = module
    return module


def _strip_em_tags(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"</?em>", "", text, flags=re.IGNORECASE)


def _one_line(text: str) -> str:
    """Collapse whitespace so JSON output has no literal ``\\n`` in strings."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip())


def _chunk_description(chunk: dict[str, Any]) -> str:
    raw = (
        (chunk.get("highlight") or chunk.get("content") or chunk.get("preview") or "")
        .strip()
    )
    raw = _strip_em_tags(raw)
    raw = _one_line(raw)
    if len(raw) > _DESC_MAX_CHARS:
        raw = raw[: _DESC_MAX_CHARS - 3] + "..."
    return raw


def _result_body(
    *,
    ok: bool,
    error_detail: str | None,
    items: list[dict[str, str]] | None,
    items_total: int | None,
) -> dict[str, Any]:
    if ok:
        total = items_total if items_total is not None else len(items or [])
        return {
            "skillName": _SKILL_NAME_EN,
            "displayText": _DISPLAY_TEXT_ZH,
            "errorDetail": None,
            "normalizedResult": {
                "source": _LEGACY_SOURCE_OK,
                "items": items or [],
                "itemsTotal": int(total),
            },
        }
    return {
        "skillName": _SKILL_NAME_EN,
        "displayText": _DISPLAY_TEXT_ERR_ZH,
        "errorDetail": _one_line(error_detail or "unknown"),
        "normalizedResult": None,
    }


def _json_tool_response(root: dict[str, Any]) -> ToolResponse:
    """AgentScope allows list[dict] blocks in practice; ``type: json`` carries a dict."""
    return ToolResponse(
        content=[{"type": "json", "json": root}],
    )


def _parse_total(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _result_to_items(result: dict[str, Any]) -> tuple[list[dict[str, str]], int | None]:
    chunks = result.get("chunks")
    if not isinstance(chunks, list):
        return [], _parse_total(result.get("total"))
    items: list[dict[str, str]] = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("document_name") or "unknown").strip() or "unknown"
        desc = _chunk_description(ch)
        items.append({"title": title, "description": desc})
    return items, _parse_total(result.get("total"))


async def doc_retrieval(
    query: str = "",
    dataset_ids: str = "",
    page: int = 1,
    page_size: int = 10,
    title: str = "",
    keywords: str = "",
    output_format: str = "text",
    workspace_dir: str | None = None,  # set via register_tool_function preset_kwargs
) -> ToolResponse:
    """Search the HaiRuo knowledge base (natural-language retrieval).

    Returns ``[{"type": "json", "json": {skillName, displayText, ...}}]`` — the
    ``json`` object holds ``skillName``, ``displayText``, ``errorDetail``, and
    ``normalizedResult`` directly (no ``payload`` key).
    The ``output_format`` parameter is kept for schema stability only.

    Configuration is read from ``kb_defaults.json`` next to the skill scripts.
    """
    del output_format  # envelope is fixed; kept in signature for tool schema stability

    q = (query or "").strip()
    if not q:
        return _json_tool_response(
            _result_body(
                ok=False,
                error_detail="缺少必填参数 query（检索语句）。",
                items=None,
                items_total=None,
            ),
        )

    ws_path = Path(workspace_dir).resolve() if workspace_dir else None
    scripts = _find_doc_retrieval_scripts_dir(ws_path)
    if scripts is None:
        return _json_tool_response(
            _result_body(
                ok=False,
                error_detail=(
                    "未找到 doc-retrieval 技能脚本（需要 doc-retrieval/scripts/search.py）。"
                    "请将技能放到工作区 skills/doc-retrieval/ 或工作目录 doc-retrieval/scripts/。"
                ),
                items=None,
                items_total=None,
            ),
        )

    try:
        mod = _get_search_module(scripts)
    except Exception as e:
        return _json_tool_response(
            _result_body(
                ok=False,
                error_detail=f"加载知识库检索脚本失败：{e}",
                items=None,
                items_total=None,
            ),
        )

    kwargs: dict[str, Any] = {
        "dataset_ids": dataset_ids.strip() if (dataset_ids or "").strip() else None,
        "page": int(page) if page else 1,
        "page_size": int(page_size) if page_size else 10,
        "title": title or "",
        "keywords": keywords or "",
    }

    def _run_dict() -> dict[str, Any]:
        search_fn = getattr(mod, "search", None)
        if callable(search_fn):
            return search_fn(
                q,
                dataset_ids=kwargs["dataset_ids"],
                page=kwargs["page"],
                page_size=kwargs["page_size"],
                title=kwargs["title"],
                keywords=kwargs["keywords"],
            )
        main_fn = getattr(mod, "main", None)
        if not callable(main_fn):
            raise RuntimeError("search.py has neither search() nor main()")
        raw = main_fn(
            q,
            dataset_ids=kwargs["dataset_ids"],
            page=kwargs["page"],
            page_size=kwargs["page_size"],
            title=kwargs["title"],
            keywords=kwargs["keywords"],
            output_format="json",
        )
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            if raw.startswith("❌"):
                raise RuntimeError(raw)
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(raw[:500] if raw else str(exc)) from exc
        raise RuntimeError(f"Unexpected main() return type: {type(raw)}")

    try:
        result_dict = await asyncio.to_thread(_run_dict)
    except Exception as e:
        return _json_tool_response(
            _result_body(
                ok=False,
                error_detail=str(e),
                items=None,
                items_total=None,
            ),
        )

    if not isinstance(result_dict, dict):
        return _json_tool_response(
            _result_body(
                ok=False,
                error_detail="检索返回了非预期的数据类型。",
                items=None,
                items_total=None,
            ),
        )

    items, items_total = _result_to_items(result_dict)
    return _json_tool_response(
        _result_body(
            ok=True,
            error_detail=None,
            items=items,
            items_total=items_total,
        ),
    )
