# -*- coding: utf-8 -*-
from qwenpaw.app.runner.command_dispatch import (
    _last_user_message_has_file_attachment,
)
from qwenpaw.plan.gov_doc_pipeline import (
    text_triggers_gov_doc_pipeline,
    user_brought_gov_doc_materials,
)


def test_triggers_on_公文():
    assert text_triggers_gov_doc_pipeline("/plan 编写一篇大数据相关的公文")


def test_triggers_on_write_plus_doc_type():
    assert text_triggers_gov_doc_pipeline("写一篇关于加强数据安全的通知")


def test_triggers_on_红头文件():
    assert text_triggers_gov_doc_pipeline("起草红头文件")


def test_no_trigger_notice_only():
    assert not text_triggers_gov_doc_pipeline("通知我明天下午开会")


def test_no_trigger_empty():
    assert not text_triggers_gov_doc_pipeline("")
    assert not text_triggers_gov_doc_pipeline("   ")


def test_no_trigger_doc_type_without_write():
    assert not text_triggers_gov_doc_pipeline("关于劳动法的通知条款摘要")


def test_user_materials_file_attachment():
    assert user_brought_gov_doc_materials("写一篇通知", has_file_attachment=True)


def test_user_materials_marker_in_text():
    assert user_brought_gov_doc_materials(
        "写一篇通知，附件如下：……",
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
