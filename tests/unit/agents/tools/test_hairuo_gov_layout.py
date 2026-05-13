# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import patch

import pytest

from qwenpaw.agents.tools.hairuo_gov_layout import (
    GovLayoutHttpError,
    fetch_layout_template_result_list,
)


@patch("qwenpaw.agents.tools.hairuo_gov_layout._request_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout.load_layout_runtime_config")
def test_fetch_layout_template_result_list_success(mock_load, mock_req) -> None:
    mock_load.return_value = {
        "base_url": "https://example.test",
        "cookie": "session=1",
        "verify_ssl": False,
    }
    mock_req.return_value = {
        "code": 0,
        "data": {"total": 1, "list": [{"id": "t1", "name": "上行文"}]},
    }
    out = fetch_layout_template_result_list("上行文", page=1, page_size=10)
    assert out == {"templates": [{"id": "t1"}]}
    assert "recommended" not in out


@patch("qwenpaw.agents.tools.hairuo_gov_layout._request_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout.load_layout_runtime_config")
def test_fetch_layout_template_array_data_shape(mock_load, mock_req) -> None:
    """New HaiRuo API: ``data`` is a JSON array (not ``{list, total}``)."""
    mock_load.return_value = {
        "base_url": "https://example.test",
        "cookie": "session=1",
        "verify_ssl": False,
    }
    mock_req.return_value = {
        "code": 0,
        "data": [
            {
                "id": "a",
                "templateTitle": "市局党委上行文",
                "industryTag": "昆明",
                "templateType": "红头",
                "count": 10,
            },
            {
                "id": "b",
                "templateTitle": "行政",
                "industryTag": "通用",
                "templateType": "红头",
                "count": 5,
            },
        ],
    }
    out = fetch_layout_template_result_list("场景", page=1, page_size=1)
    assert len(out["templates"]) == 2
    assert [x["id"] for x in out["templates"]] == ["a", "b"]

    out2 = fetch_layout_template_result_list("场景", page=99, page_size=1)
    assert len(out2["templates"]) == 2
    assert [x["id"] for x in out2["templates"]] == ["a", "b"]


@patch("qwenpaw.agents.tools.hairuo_gov_layout._post_layout_recommend_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout._request_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout.load_layout_runtime_config")
def test_fetch_layout_template_with_recommendation(
    mock_load,
    mock_req,
    mock_post,
) -> None:
    mock_load.return_value = {
        "base_url": "https://example.test",
        "cookie": "session=1",
        "verify_ssl": False,
        "recommend_chat_url": "http://llm/v1/chat/completions",
        "recommend_model": "Qwen3-235B-A22B-FP8",
    }
    mock_req.return_value = {
        "code": 0,
        "data": {
            "total": 2,
            "list": [
                {
                    "id": "a",
                    "templateTitle": "市局党委上行文",
                    "industryTag": "昆明",
                    "templateType": "红头",
                    "count": 10,
                },
                {
                    "id": "b",
                    "templateTitle": "行政通知",
                    "industryTag": "通用",
                    "templateType": "红头",
                    "count": 5,
                },
            ],
        },
    }
    mock_post.return_value = {
        "choices": [
            {"message": {"content": '[{"template":"市局党委上行文","id":"a"}]'}}
        ],
    }
    out = fetch_layout_template_result_list("国庆节放假通知", page=1, page_size=10)
    assert len(out["templates"]) == 2
    assert out["recommended"] == [
        {
            "id": "a",
            "templateTitle": "市局党委上行文",
            "industryTag": "昆明",
            "templateType": "红头",
            "count": 10,
        }
    ]
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][1]
    assert payload["model"] == "Qwen3-235B-A22B-FP8"
    assert payload["messages"][1]["content"] == "国庆节放假通知"


@patch("qwenpaw.agents.tools.hairuo_gov_layout._post_layout_recommend_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout._request_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout.load_layout_runtime_config")
def test_fetch_layout_recommend_post_fails_returns_empty_recommended(
    mock_load,
    mock_req,
    mock_post,
) -> None:
    mock_load.return_value = {
        "base_url": "https://example.test",
        "cookie": "session=1",
        "verify_ssl": False,
        "recommend_chat_url": "http://llm/v1/chat/completions",
    }
    mock_req.return_value = {
        "code": 0,
        "data": {
            "total": 1,
            "list": [{"id": "x", "templateTitle": "T", "count": 1}],
        },
    }
    mock_post.return_value = None
    out = fetch_layout_template_result_list("query", page=1, page_size=10)
    assert out["recommended"] == []


def test_parse_recommendation_entries_strips_markdown_fence() -> None:
    import qwenpaw.agents.tools.hairuo_gov_layout as h

    raw = '```json\n[{"template": "A", "id": "1"}]\n```'
    assert h._parse_recommendation_entries(raw) == [("A", "1")]


@patch("qwenpaw.agents.tools.hairuo_gov_layout._request_json")
@patch("qwenpaw.agents.tools.hairuo_gov_layout.load_layout_runtime_config")
def test_fetch_layout_template_api_error(mock_load, mock_req) -> None:
    mock_load.return_value = {
        "base_url": "https://example.test",
        "cookie": "session=1",
        "verify_ssl": True,
    }
    mock_req.return_value = {"code": 1, "message": "bad request"}
    with pytest.raises(GovLayoutHttpError, match="bad request"):
        fetch_layout_template_result_list("x")


def test_fetch_requires_non_empty_title() -> None:
    with pytest.raises(GovLayoutHttpError, match="template_title"):
        fetch_layout_template_result_list("   ", explicit_defaults_path="")


def test_load_layout_runtime_config_overlay_overrides_file(monkeypatch) -> None:
    import qwenpaw.agents.tools.hairuo_gov_layout as h

    monkeypatch.setattr(
        h,
        "_load_first_layout_defaults_file",
        lambda **kw: {"base_url": "https://from-file", "cookie": "file=1", "verify_ssl": False},
    )
    monkeypatch.setattr(
        h,
        "_project_hairuo_overlay",
        lambda: {"base_url": "https://from-config"},
    )
    merged = h.load_layout_runtime_config()
    assert merged["base_url"] == "https://from-config"
    assert merged["cookie"] == "file=1"
    assert merged["verify_ssl"] is False
