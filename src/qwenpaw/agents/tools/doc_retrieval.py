# -*- coding: utf-8 -*-
"""HaiRuo knowledge-base retrieval (``doc_retrieval`` / ``doc-retrieval``).

Loads ``search.main`` from the ``doc-retrieval`` skill ``scripts/`` folder when
present under workspace skills, working dir, or project ``doc-retrieval/``.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, Callable

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...constant import WORKING_DIR

_search_main_cache: dict[str, Callable[..., str]] = {}


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


def _get_search_main(scripts_dir: Path) -> Callable[..., str]:
    key = str(scripts_dir.resolve())
    if key in _search_main_cache:
        return _search_main_cache[key]
    path = scripts_dir / "search.py"
    mod_name = f"qwenpaw_doc_retrieval_{hash(key) & 0xFFFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = getattr(module, "main", None)
    if main is None or not callable(main):
        raise RuntimeError(f"{path} has no callable main()")
    _search_main_cache[key] = main
    return main


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

    Configuration is read from ``kb_defaults.json`` next to the skill scripts
    (``base_url``, ``cookie`` or ``api_key``, ``default_dataset_ids``), unless
    overridden by the script's environment variables.

    Args:
        query: Search query (required).
        dataset_ids: Comma-separated dataset ids (optional if configured).
        page: Page number (default 1).
        page_size: Page size (default 10, capped by the API script).
        title: Optional document title filter.
        keywords: Comma-separated keywords.
        output_format: ``text`` or ``json``.
        workspace_dir: Injected by the agent (not in the tool schema): workspace
            root used first when locating ``skills/doc-retrieval/scripts``.
    """
    q = (query or "").strip()
    if not q:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="错误：缺少必填参数 query（检索语句）。",
                ),
            ],
        )

    fmt = (output_format or "text").strip().lower()
    if fmt not in ("text", "json"):
        fmt = "text"

    ws_path = Path(workspace_dir).resolve() if workspace_dir else None
    scripts = _find_doc_retrieval_scripts_dir(ws_path)
    if scripts is None:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "未找到 doc-retrieval 技能脚本（需要包含 "
                        "doc-retrieval/scripts/search.py）。"
                        "请将技能放到工作区 skills/doc-retrieval/，"
                        "或工作目录下的 doc-retrieval/scripts/。"
                    ),
                ),
            ],
        )

    try:
        main_fn = _get_search_main(scripts)
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"加载知识库检索脚本失败：{e}",
                ),
            ],
        )

    kwargs: dict[str, Any] = {
        "page": int(page) if page else 1,
        "page_size": int(page_size) if page_size else 10,
        "title": title or "",
        "keywords": keywords or "",
        "output_format": fmt,
    }
    if (dataset_ids or "").strip():
        kwargs["dataset_ids"] = dataset_ids.strip()

    def _run() -> str:
        return main_fn(q, **kwargs)

    try:
        text = await asyncio.to_thread(_run)
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"知识库检索执行异常：{e}",
                ),
            ],
        )

    return ToolResponse(content=[TextBlock(type="text", text=text)])
