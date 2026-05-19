# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

from qwenpaw.plan.hints import (
    _GOV_DOC_PIPELINE_NO_PLAN,
    _infer_gov_tool_from_text,
    _gov_pipeline_tool_reminder,
)


def test_infer_gov_tool_review_before_write():
    assert _infer_gov_tool_from_text("审核修改内容") == "doc_reviewer"
    assert _infer_gov_tool_from_text("起草修改建议") == "gov_document_writer"
    assert _infer_gov_tool_from_text("检索相关文档") == "doc_retrieval"
    assert _infer_gov_tool_from_text("推荐模板版式") == "gov_document_layout"


def test_no_plan_hint_does_not_require_four_steps():
    assert "恰好 4" not in _GOV_DOC_PIPELINE_NO_PLAN
    assert "必须恰好" not in _GOV_DOC_PIPELINE_NO_PLAN
    assert "仅审核" in _GOV_DOC_PIPELINE_NO_PLAN
    assert "1 个子任务" in _GOV_DOC_PIPELINE_NO_PLAN


def test_tool_reminder_follows_subtask_text_not_index():
    plan = SimpleNamespace(
        subtasks=[
            SimpleNamespace(
                name="审核修改内容",
                description="对用户提供的信息进行审核",
                expected_outcome="输出审核后正文",
            ),
        ],
    )
    nb = SimpleNamespace(_plan_skip_doc_retrieval=False)
    msg = _gov_pipeline_tool_reminder(0, plan, nb)
    assert "doc_reviewer" in msg
    assert "doc_retrieval" in msg
    assert "禁止" in msg


def test_tool_reminder_skips_retrieval_when_materials_present():
    plan = SimpleNamespace(
        subtasks=[
            SimpleNamespace(
                name="检索背景资料",
                description="doc_retrieval",
                expected_outcome="items",
            ),
        ],
    )
    nb = SimpleNamespace(_plan_skip_doc_retrieval=True)
    msg = _gov_pipeline_tool_reminder(0, plan, nb)
    assert "不要" in msg
    assert "doc_retrieval" in msg
