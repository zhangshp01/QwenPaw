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
from qwenpaw.config.context import (
    clear_agent_tool_step_for_running_task,
    set_agent_tool_step_for_running_task,
    set_current_workspace_dir,
)


def _json_payload(r):
    block = r.content[0]
    assert isinstance(block, dict)
    assert block.get("type") == "json"
    return block["json"]


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
        p = _json_payload(r)
        assert p["normalizedResult"] is None
        assert p["sourceState"] == "error"
        assert p["retryable"] is True
        assert p["savePath"] is None
        assert p["skillName"] == "gov-document-writer"
        assert "content" in (p.get("errorDetail") or "").lower()

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
            p = _json_payload(r)
            assert p["skillName"] == "gov-document-writer"
            assert p["sourceState"] == "model_success"
            assert p["errorDetail"] is None
            assert p["retryable"] is True
            assert p["stepIndex"] == 1
            sp = (p.get("savePath") or "").replace("\\", "/")
            assert "govdocs" in sp
            assert sp.lower().endswith(".docx")
            nr = p["normalizedResult"]
            assert nr["source"] == "model_success"
            assert "Body line one." in nr["document"]
            assert "**类型**" not in nr["document"]
            assert "notice" not in nr["document"]
            docx_files = list((root / "govdocs").glob("Holiday Notice-*.docx"))
            assert len(docx_files) == 1
            assert Path(p["savePath"]).resolve() == docx_files[0].resolve()
            from docx import Document as DocxDocument

            d = DocxDocument(str(docx_files[0]))
            combined = "\n".join(para.text for para in d.paragraphs)
            assert "Holiday Notice" in combined
            assert "Body line one." in combined

    asyncio.run(_run())


def test_gov_document_writer_step_index_from_task_mapping():
    async def _run():
        set_agent_tool_step_for_running_task(3)
        try:
            r = await gov_document_writer(content="")
            p = _json_payload(r)
            assert p["stepIndex"] == 3
        finally:
            clear_agent_tool_step_for_running_task()

    asyncio.run(_run())
