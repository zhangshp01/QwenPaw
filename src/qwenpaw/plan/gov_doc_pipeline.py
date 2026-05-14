# -*- coding: utf-8 -*-
"""Heuristics for the gov-document ``/plan`` auto-run pipeline."""

from __future__ import annotations

# 文种 / 公文语境（命中其一可作为「公文类任务」信号的一部分）
_GOV_DOC_TYPE_KEYWORDS: frozenset[str] = frozenset(
    {
        "公文",
        "通知",
        "请示",
        "报告",
        "批复",
        "函",
        "决定",
        "意见",
        "纪要",
        "通告",
        "通报",
        "议案",
        "命令",
        "令",
        "上行文",
        "下行文",
        "平行文",
        "红头",
        "红头文件",
        "行政公文",
        "机关文书",
        "正式文件",
    },
)

# 写作 / 发文动作（与文种组合，或单独与「公文」等强词组合）
_WRITE_INTENT_FRAGMENTS: frozenset[str] = frozenset(
    {
        "写",
        "撰写",
        "草拟",
        "起草",
        "编写",
        "编制",
        "拟写",
        "发文",
        "印发",
        "出具",
        "拟文",
        "成文",
        "撰稿",
        "代拟",
        "完稿",
        "输出",
        "生成",
    },
)


def text_triggers_gov_doc_pipeline(text: str) -> bool:
    """True when *text* looks like a gov-document drafting request.

    Used for ``/plan <description>`` after the ``/plan`` prefix is stripped.
    Reduces false positives like「通知我开会」: requires a write-intent
    fragment together with a doc-type keyword, **or** a strong phrase
    containing「公文」等.
    """
    if not text or not text.strip():
        return False
    t = text.strip()

    if any(k in t for k in ("公文", "红头文件", "行政公文", "机关文书")):
        return True

    has_type = any(kw in t for kw in _GOV_DOC_TYPE_KEYWORDS)
    if not has_type:
        return False

    has_write = any(w in t for w in _WRITE_INTENT_FRAGMENTS)
    return bool(has_write)
