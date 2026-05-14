# -*- coding: utf-8 -*-
from qwenpaw.plan.gov_doc_pipeline import text_triggers_gov_doc_pipeline


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
