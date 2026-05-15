# -*- coding: utf-8 -*-
"""Helpers for the gov-document ``/plan`` pipeline.

:func:`text_triggers_gov_doc_pipeline` currently **always** returns ``True`` so
every non-empty ``/plan <description>`` arms ``_plan_gov_doc_pipeline`` in the
runner (gov-document hints in :mod:`~qwenpaw.plan.hints`). Replace with
heuristics if you need to exclude non-drafting ``/plan`` turns.

:func:`user_brought_gov_doc_materials` still controls skipping ``doc_retrieval``
for subtask 0 when the user turn already carries reference material.
"""

from __future__ import annotations

# User message signals "reference materials are already in this turn" (narrow,
# conservative list to avoid skipping retrieval on generic long prompts).
_USER_SUPPLIED_MATERIAL_MARKERS: frozenset[str] = frozenset(
    {
        "附件如下",
        "见附件",
        "附件见",
        "素材如下",
        "以下素材",
        "以下材料",
        "材料如下",
        "参考文本如下",
        "原文如下",
        "政策原文",
        "背景材料如下",
        "依据材料如下",
        "现将有关材料",
        "附相关材料",
        "无需检索知识库",
        "不用检索知识库",
        "已有素材",
        "素材已提供",
        "自带素材",
        "不调用检索",
        "略过检索",
        "请基于",
        "整理好了素材",
        "严格按照",
    },
)


def text_triggers_gov_doc_pipeline(text: str) -> bool:
    """Whether to arm the gov-document ``/plan`` pipeline from *plan* tail text.

    Forced ``True`` for all inputs (runner only calls this with a non-empty
    ``plan_desc`` after ``/plan ``). Used by :mod:`~qwenpaw.app.runner.runner`.
    """
    _ = text
    return True


def user_brought_gov_doc_materials(
    text: str,
    *,
    has_file_attachment: bool = False,
) -> bool:
    """True when the user turn already carries drafting reference material.

    Used with ``has_file_attachment`` from the last user message (e.g. file
    blocks from the API) plus explicit in-text markers. When true, the
    gov-document plan pipeline skips ``doc_retrieval`` for subtask 0.
    """
    if has_file_attachment:
        return True
    if not text or not text.strip():
        return False
    t = text.strip()
    return any(marker in t for marker in _USER_SUPPLIED_MATERIAL_MARKERS)
