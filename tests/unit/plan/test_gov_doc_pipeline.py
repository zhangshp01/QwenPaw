# -*- coding: utf-8 -*-
from qwenpaw.app.runner.command_dispatch import (
    _last_user_message_has_file_attachment,
)
from qwenpaw.plan.gov_doc_pipeline import (
    text_triggers_gov_doc_pipeline,
    user_brought_gov_doc_materials,
)

# ``text_triggers_gov_doc_pipeline`` is forced True for all /plan arming.


def test_pipeline_heuristic_always_true():
    samples = [
        "/plan 编写一篇大数据相关的公文",
        "写一篇关于加强数据安全的通知",
        (
            "帮我审核下如下公文：关于加强大数据的指导意见\n\n"
            "各有关单位：\n……"
        ),
        "起草红头文件",
        "通知我明天下午开会",
        "关于劳动法的通知条款摘要",
    ]
    for s in samples:
        assert text_triggers_gov_doc_pipeline(s)


def test_pipeline_heuristic_empty_or_whitespace_true():
    assert text_triggers_gov_doc_pipeline("")
    assert text_triggers_gov_doc_pipeline("   ")


def test_user_materials_file_attachment():
    assert user_brought_gov_doc_materials("写一篇通知", has_file_attachment=True)


def test_user_materials_marker_in_text():
    assert user_brought_gov_doc_materials(
        "写一篇通知，附件如下：……",
        has_file_attachment=False,
    )


def test_user_materials_review_inline_body():
    assert user_brought_gov_doc_materials(
        "请帮我审核如下信息：省大数据局负责……",
        has_file_attachment=False,
    )


def test_user_materials_no_marker_no_file():
    assert not user_brought_gov_doc_materials(
        "写一篇关于加强数据安全的通知",
        has_file_attachment=False,
    )


def test_last_user_message_has_file_dict():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "按附件写一份请示"},
                {"type": "file", "file_url": "https://example.com/a.pdf"},
            ],
        },
    ]
    assert _last_user_message_has_file_attachment(msgs)


def test_last_user_message_no_file_dict():
    msgs = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "写一篇通知"}],
        },
    ]
    assert not _last_user_message_has_file_attachment(msgs)
