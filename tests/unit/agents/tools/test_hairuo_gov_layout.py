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
    assert out == {"total": 1, "list": [{"id": "t1", "name": "上行文"}]}


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
