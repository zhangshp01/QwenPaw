# -*- coding: utf-8 -*-
"""Tests for workspace binary write path resolution."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from qwenpaw.app.workspace_paths import (
    normalize_path_argument,
    resolve_workspace_write_path,
)


def test_normalize_path_argument_strips_leading_slash_on_windows_drive():
    assert normalize_path_argument("/C:/foo/bar.doc") == "C:/foo/bar.doc"


def test_resolve_relative_path_under_workspace(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    name = "地震-20260519103658680171.docx"
    target = resolve_workspace_write_path(f"govdocs/{name}", ws)
    assert target == (ws / "govdocs" / name).resolve()


def test_resolve_absolute_path_inside_workspace(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    inner = ws / "govdocs" / "x.doc"
    inner.parent.mkdir(parents=True)
    target = resolve_workspace_write_path(str(inner), ws)
    assert target == inner.resolve()


def test_resolve_rejects_path_outside_workspace(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    outside = tmp_path / "outside.doc"
    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_write_path(str(outside), ws)
    assert exc_info.value.status_code == 403


def test_resolve_rejects_empty_path(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_write_path("  ", ws)
    assert exc_info.value.status_code == 400


def test_resolve_rejects_parent_traversal(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_write_path("../../etc/passwd", ws)
    assert exc_info.value.status_code == 403
