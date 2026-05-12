# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from qwenpaw.agents.tools.gov_document_writer import (
    doc_reviewer,
    gov_document_writer,
)
from qwenpaw.config.context import (
    clear_agent_tool_step_for_running_task,
    set_agent_tool_step_for_running_task,
    set_current_workspace_dir,
)

_DOC_REVIEW_PATCH = "qwenpaw.agents.tools.gov_document_writer._run_document_review_llm"


def _json_payload(r):
    block = r.content[0]
    assert isinstance(block, dict)
    assert block.get("type") == "json"
    return block["json"]


def test_doc_reviewer_requires_input():
    async def _run():
        r = await doc_reviewer(content="", file_path="", path="")
        p = _json_payload(r)
        assert p["skillName"] == "doc_reviewer"
        assert p["sourceState"] == "error"
        assert p["normalizedResult"] is None
        assert p["displayText"] == "文档审核失败"
        detail = p.get("errorDetail") or ""
        assert "content" in detail and ("file_path" in detail or "path" in detail)

    asyncio.run(_run())
    async def _run():
        revised = "para1-fixed.\npara2-fixed."
        fake = AsyncMock(return_value=revised)
        with patch(_DOC_REVIEW_PATCH, fake):
            r = await doc_reviewer(content="para1.\npara2.")
        p = _json_payload(r)
        assert p["skillName"] == "doc_reviewer"
        assert p["sourceState"] == "model_success"
        assert p["displayText"] == "已完成：文档审核"
        assert p["stepIndex"] == 1
        nr = p["normalizedResult"]
        assert nr["source"] == "model_success"
        assert nr["document"] == revised
        fake.assert_awaited_once()
        assert "para1" in (fake.await_args.args[0] or "")

    asyncio.run(_run())


def test_doc_reviewer_reads_file():
    async def _run():
        fake = AsyncMock(return_value="hello-revised")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            p = root / "draft.md"
            p.write_text("# T\n\nhello", encoding="utf-8")
            with patch(_DOC_REVIEW_PATCH, fake):
                r = await doc_reviewer(file_path="draft.md")
            payload = _json_payload(r)
            assert payload["skillName"] == "doc_reviewer"
            assert payload["displayText"] == "已完成：文档审核"
            assert payload["normalizedResult"]["document"] == "hello-revised"
            fake.assert_awaited_once()
            assert "hello" in (fake.await_args.args[0] or "")

    asyncio.run(_run())


def test_doc_reviewer_step_index_from_task_mapping():
    async def _run():
        fake = AsyncMock(return_value="final-body")
        set_agent_tool_step_for_running_task(2)
        try:
            with patch(_DOC_REVIEW_PATCH, fake):
                r = await doc_reviewer(content="x")
            p = _json_payload(r)
            assert p["stepIndex"] == 2
        finally:
            clear_agent_tool_step_for_running_task()

    asyncio.run(_run())


def test_doc_reviewer_llm_failure_returns_error():
    async def _run():
        fake = AsyncMock(side_effect=RuntimeError("model-unavailable"))
        with patch(_DOC_REVIEW_PATCH, fake):
            r = await doc_reviewer(content="one line.")
        p = _json_payload(r)
        assert p["sourceState"] == "error"
        assert p["normalizedResult"] is None
        assert "model-unavailable" in (p.get("errorDetail") or "")

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
