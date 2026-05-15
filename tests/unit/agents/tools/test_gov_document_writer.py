# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from qwenpaw.agents.tools.gov_document_writer import (
    _parse_document_review_response,
    doc_reviewer,
    gov_document_layout,
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
        assert "缺少待审阅" in detail or "file_path" in detail
        assert p.get("resultList") is None

    asyncio.run(_run())
    async def _run():
        revised = "para1-fixed.\npara2-fixed."
        fake = AsyncMock(return_value=(revised, []))
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
        assert p["resultList"] == []
        fake.assert_awaited_once()
        assert "para1" in (fake.await_args.args[0] or "")

    asyncio.run(_run())


def test_doc_reviewer_reads_file():
    async def _run():
        fake = AsyncMock(return_value=("hello-revised", []))
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


def test_doc_reviewer_prefers_normalized_result_over_file():
    async def _run():
        fake = AsyncMock(return_value=("revised-from-prior", []))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            fp = root / "draft.md"
            fp.write_text("from-file-body", encoding="utf-8")
            with patch(_DOC_REVIEW_PATCH, fake):
                r = await doc_reviewer(
                    content="",
                    file_path="draft.md",
                    normalizedResult={
                        "document": "prior-step-body\n第二段。",
                        "source": "model_success",
                    },
                )
            payload = _json_payload(r)
            assert payload["sourceState"] == "model_success"
            fake.assert_awaited_once()
            sent = fake.await_args.args[0] or ""
            assert "prior-step-body" in sent
            assert "from-file-body" not in sent

    asyncio.run(_run())


def test_doc_reviewer_unwraps_prior_writer_tool_json_array():
    """Accept AgentLoop-style ``[{type: json, json: {...}}]`` from the writer step."""

    async def _run():
        fake = AsyncMock(return_value=("定稿", []))
        blob = json.dumps(
            [
                {
                    "type": "json",
                    "json": {
                        "skillName": "gov-document-writer",
                        "normalizedResult": {
                            "document": "关于测试的通知\n\n仅一段正文。",
                            "source": "model_success",
                        },
                    },
                },
            ],
            ensure_ascii=False,
        )
        with patch(_DOC_REVIEW_PATCH, fake):
            r = await doc_reviewer(content=blob)
        payload = _json_payload(r)
        assert payload["sourceState"] == "model_success"
        fake.assert_awaited_once()
        sent = fake.await_args.args[0] or ""
        assert "关于测试的通知" in sent
        assert "仅一段正文" in sent

    asyncio.run(_run())


def test_doc_reviewer_step_index_from_task_mapping():
    async def _run():
        fake = AsyncMock(return_value=("final-body", []))
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
        assert p.get("resultList") is None

    asyncio.run(_run())


def test_doc_reviewer_result_list_from_llm_tuple():
    async def _run():
        items = [
            {
                "reason": "错别字",
                "offsets": 25,
                "errorType": "错别字错误",
                "errorWord": "法展",
                "contextOffset": 10,
                "context": "随着大数据技术的迅速法展，",
                "rightWord": "发展",
            },
        ]
        fake = AsyncMock(return_value=("定稿正文", items))
        with patch(_DOC_REVIEW_PATCH, fake):
            r = await doc_reviewer(content="x")
        p = _json_payload(r)
        assert p["normalizedResult"]["document"] == "定稿正文"
        assert len(p["resultList"]) == 1
        row = p["resultList"][0]
        assert row["errorWord"] == "法展"
        assert row["rightWord"] == "发展"
        assert row["offsets"] == 25
        assert row["contextOffset"] == 10

    asyncio.run(_run())


def test_parse_document_review_response_json():
    raw = json.dumps(
        {
            "document": "后文",
            "resultList": [
                {
                    "reason": "r",
                    "offsets": 1,
                    "errorType": "t",
                    "errorWord": "错",
                    "contextOffset": 0,
                    "context": "上下文",
                    "rightWord": "对",
                },
            ],
        },
        ensure_ascii=False,
    )
    doc, items = _parse_document_review_response(raw)
    assert doc == "后文"
    assert len(items) == 1
    assert items[0]["errorWord"] == "错"


def test_parse_document_review_response_plain_text_legacy():
    doc, items = _parse_document_review_response("  仅正文无JSON  ")
    assert doc == "仅正文无JSON"
    assert items == []


def test_parse_document_review_response_invalid_document_key():
    with pytest.raises(RuntimeError, match="document"):
        _parse_document_review_response('{"document": ""}')


def test_gov_document_writer_requires_content():
    async def _run():
        r = await gov_document_writer(content="")
        p = _json_payload(r)
        assert p["normalizedResult"] is None
        assert p["sourceState"] == "error"
        assert p["retryable"] is True
        assert "savePath" not in p
        assert p["skillName"] == "gov-document-writer"
        assert "content" in (p.get("errorDetail") or "").lower()

    asyncio.run(_run())


def test_gov_document_writer_returns_json_no_file_no_save_path():
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
            assert "savePath" not in p
            nr = p["normalizedResult"]
            assert nr["source"] == "model_success"
            assert "Body line one." in nr["document"]
            assert "**类型**" not in nr["document"]
            assert "notice" not in nr["document"]
            assert not (root / "govdocs").exists() or not list(
                (root / "govdocs").glob("*.docx"),
            )

    asyncio.run(_run())


def test_gov_document_writer_accepts_body_synonyms_in_extra():
    """Models may still pass ``text`` / ``body`` etc. as overflow kwargs."""

    async def _run():
        r = await gov_document_writer(title="T", text="Line from text kwarg.")
        p = _json_payload(r)
        assert p["sourceState"] == "model_success"
        assert "Line from text kwarg." in p["normalizedResult"]["document"]

    asyncio.run(_run())


def test_gov_document_layout_requires_template_title():
    async def _run():
        r = await gov_document_layout(
            content="some body",
            keyword="",
            text="",
            document="",
            body="",
        )
        p = _json_payload(r)
        assert p["normalizedResult"] is None
        assert p["sourceState"] == "error"
        assert p["skillName"] == "gov_document_layout"
        assert "template_title" in (p.get("errorDetail") or "")

    asyncio.run(_run())


def test_gov_document_layout_mocked_fetch_returns_pending_save_path_no_file():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            with patch(
                "qwenpaw.agents.tools.gov_document_writer.fetch_layout_template_result_list",
                return_value={"templates": [{"id": "1"}]},
            ) as mock_fetch:
                r = await gov_document_layout(template_title="上行文")
            mock_fetch.assert_called_once()
            p = _json_payload(r)
            assert p["sourceState"] == "model_success"
            sp = p.get("savePath") or ""
            assert sp
            assert sp.lower().endswith(".docx")
            assert "govdocs" in sp.replace("\\", "/")
            assert not Path(sp).exists()
            assert "resultList" not in p

    asyncio.run(_run())


def test_gov_document_layout_template_title_ignores_body_no_write():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            with patch(
                "qwenpaw.agents.tools.gov_document_writer.fetch_layout_template_result_list",
                return_value={"templates": [{"id": "a"}]},
            ):
                r = await gov_document_layout(
                    template_title="关于加强数据安全管理的通知",
                    content="正文不应触发写盘。",
                    title="IgnoredTitle",
                )
            p = _json_payload(r)
            assert p["sourceState"] == "model_success"
            sp = p.get("savePath") or ""
            assert sp and not Path(sp).exists()
            assert not list((root / "govdocs").glob("*.docx"))

    asyncio.run(_run())


def test_gov_document_layout_template_title_bad_file_still_recommends():
    """``template_title`` alone: missing ``file_path`` file is irrelevant."""

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            set_current_workspace_dir(Path(td))
            with patch(
                "qwenpaw.agents.tools.gov_document_writer.fetch_layout_template_result_list",
                return_value={"templates": [{"id": "1", "templateTitle": "T"}]},
            ):
                r = await gov_document_layout(
                    template_title="关于加强数据安全的通知",
                    file_path="no_such_file_xyz.md",
                )
            p = _json_payload(r)
            assert p["sourceState"] == "model_success"
            assert p.get("savePath")
            assert not Path(p["savePath"]).exists()

    asyncio.run(_run())


def test_gov_document_layout_client_json_omits_templates_keeps_recommended():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            set_current_workspace_dir(Path(td))
            rec = [{"id": "a", "templateTitle": "T"}]
            with patch(
                "qwenpaw.agents.tools.gov_document_writer.fetch_layout_template_result_list",
                return_value={"templates": [{"id": "a", "templateTitle": "T"}], "recommended": rec},
            ):
                r = await gov_document_layout(template_title="场景")
            p = _json_payload(r)
            assert p["resultList"] == {"recommended": rec}
            assert "templates" not in p["resultList"]
            assert p.get("savePath")

    asyncio.run(_run())


def test_gov_document_layout_explicit_templates_skips_fetch():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_current_workspace_dir(root)
            templates = json.dumps(
                [
                    {
                        "id": "7eac05530af3445fa3dccf343e8c4e36",
                        "templateTitle": "市局党委上行文",
                        "industryTag": "昆明",
                        "share": True,
                        "templateType": "红头",
                        "layoutContent": "",
                        "count": 10,
                    },
                ],
                ensure_ascii=False,
            )
            with patch(
                "qwenpaw.agents.tools.gov_document_writer.fetch_layout_template_result_list",
            ) as mock_fetch:
                r = await gov_document_layout(
                    template_title="示例标题",
                    templates=templates,
                )
            mock_fetch.assert_not_called()
            p = _json_payload(r)
            assert p["sourceState"] == "model_success"
            assert p.get("savePath")
            assert not Path(p["savePath"]).exists()
            assert "resultList" not in p

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
