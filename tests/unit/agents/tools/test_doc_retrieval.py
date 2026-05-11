# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from unittest.mock import patch

from qwenpaw.agents.tools.doc_retrieval import doc_retrieval
from qwenpaw.agents.tools.gov_document_writer import _content_block_text


def test_doc_retrieval_requires_query():
    async def _run():
        r = await doc_retrieval(query="   ")
        text = _content_block_text(r.content[0])
        assert text and "query" in text

    asyncio.run(_run())


def test_doc_retrieval_uses_workspace_skill_scripts(tmp_path):
    async def _run():
        scripts = tmp_path / "skills" / "doc-retrieval" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "search.py").write_text(
            "def main(query, **kwargs):\n"
            "    return f'ok:{query}:fmt={kwargs.get(\"output_format\")}'\n",
            encoding="utf-8",
        )
        r = await doc_retrieval(
            query="hello",
            output_format="json",
            workspace_dir=str(tmp_path),
        )
        assert _content_block_text(r.content[0]) == "ok:hello:fmt=json"

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
        body = _content_block_text(r.content[0]) or ""
        assert "未找到" in body

    asyncio.run(_run())
