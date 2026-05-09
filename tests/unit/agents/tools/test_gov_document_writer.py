# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from qwenpaw.agents.tools.gov_document_writer import (
    _content_block_text,
    doc_reviewer,
    gov_document_writer,
)
from qwenpaw.config.context import set_current_workspace_dir


def test_doc_reviewer_requires_input():
    async def _run():
        r = await doc_reviewer(content="", file_path="", path="")
        text = _content_block_text(r.content[0])
        assert text
        assert (
            ("file_path" in text or "path" in text)
            and "content" in text
        )

    asyncio.run(_run())


def test_doc_reviewer_accepts_content():
    async def _run():
        r = await doc_reviewer(content="第一段。\n第二段。")
        text = _content_block_text(r.content[0])
        assert text
        assert "第一段" in text

    asyncio.run(_run())


def test_doc_reviewer_reads_file():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            p = root / "draft.md"
            p.write_text("# T\n\nhello", encoding="utf-8")
            r = await doc_reviewer(file_path="draft.md")
            text = _content_block_text(r.content[0])
            assert text
            assert "hello" in text

    asyncio.run(_run())


def test_gov_document_writer_requires_content():
    async def _run():
        r = await gov_document_writer(content="")
        text = _content_block_text(r.content[0])
        assert text
        assert "content" in text.lower()

    asyncio.run(_run())


def test_gov_document_writer_writes_file():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            r = await gov_document_writer(
                title="Holiday Notice",
                content="Body line one.",
                type="notice",
            )
            text = _content_block_text(r.content[0])
            assert text
            assert "govdocs" in text
            md = root / "govdocs" / "Holiday Notice.md"
            assert md.is_file()
            raw = md.read_text(encoding="utf-8")
            assert "Holiday Notice" in raw
            assert "Body line one." in raw

    asyncio.run(_run())
