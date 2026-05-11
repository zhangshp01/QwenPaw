# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from unittest.mock import patch

from qwenpaw.agents.tools.doc_retrieval import doc_retrieval
from qwenpaw.config.context import (
    clear_agent_tool_step_for_running_task,
    set_agent_tool_step_for_running_task,
)


def _json_block(r) -> dict:
    block = r.content[0]
    assert isinstance(block, dict)
    assert block.get("type") == "json"
    assert "json" in block
    return block["json"]


def _payload(r) -> dict:
    """Flat tool body (skillName, displayText, …) inside the json block."""
    return _json_block(r)


def test_doc_retrieval_requires_query():
    async def _run():
        r = await doc_retrieval(query="   ")
        p = _payload(r)
        assert p["errorDetail"]
        assert "query" in p["errorDetail"]
        assert p["normalizedResult"] is None
        assert p["stepIndex"] == 1

    asyncio.run(_run())


def test_doc_retrieval_uses_workspace_skill_scripts(tmp_path):
    async def _run():
        scripts = tmp_path / "skills" / "doc-retrieval" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "search.py").write_text(
            """
def search(query, dataset_ids=None, page=1, page_size=10, title="", keywords="", **kwargs):
    return {
        "query": query,
        "dataset_ids": [],
        "chunks": [
            {
                "document_name": "a.txt",
                "content": "body",
                "highlight": "",
                "preview": "prev",
            }
        ],
        "count": 1,
        "total": 1,
    }

def main(query, **kwargs):
    return "legacy"
""",
            encoding="utf-8",
        )
        r = await doc_retrieval(
            query="hello",
            output_format="json",
            workspace_dir=str(tmp_path),
        )
        p = _json_block(r)
        assert p["skillName"] == "doc-retrieval"
        assert p["stepIndex"] == 1
        assert "知识库" in p["displayText"]
        assert p["errorDetail"] is None
        nr = p["normalizedResult"]
        assert nr["source"] == "legacy_success"
        assert nr["itemsTotal"] == 1
        assert nr["items"][0]["title"] == "a.txt"
        assert "body" in nr["items"][0]["description"]
        # structured block, not a stringified JSON inside text
        assert r.content[0].get("text") is None

    asyncio.run(_run())


def test_doc_retrieval_step_index_from_task_mapping():
    """Mirrors agent toolkit middleware: stepIndex follows per-task assignment."""

    async def _run():
        set_agent_tool_step_for_running_task(4)
        try:
            r = await doc_retrieval(query="   ")
            p = _payload(r)
            assert p["stepIndex"] == 4
        finally:
            clear_agent_tool_step_for_running_task()

    asyncio.run(_run())


def test_doc_retrieval_missing_scripts_message(tmp_path):
    async def _run():
        with patch(
            "qwenpaw.agents.tools.doc_retrieval._find_doc_retrieval_scripts_dir",
            return_value=None,
        ):
            r = await doc_retrieval(
                query="x",
                workspace_dir=str(tmp_path),
            )
        p = _payload(r)
        assert "未找到" in (p.get("errorDetail") or "")

    asyncio.run(_run())
