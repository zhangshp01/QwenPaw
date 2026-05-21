# -*- coding: utf-8 -*-
"""Tests for workspace binary write path resolution."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from qwenpaw.app.workspace_paths import (
    create_workspace_document,
    create_workspace_folder,
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


def test_create_workspace_folder_under_parent_dir(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)

    entry = create_workspace_folder(str(gov), "目录2", ws)

    assert entry["type"] == "directory"
    assert entry["filename"] == "目录2"
    assert (gov / "目录2").is_dir()
    assert entry["relative_path"] == "目录2"


def test_create_workspace_folder_rejects_nested_name(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)

    with pytest.raises(HTTPException) as exc_info:
        create_workspace_folder("govdocs", "a/b", ws)
    assert exc_info.value.status_code == 400


def test_create_workspace_folder_uses_copy_suffix_when_duplicate(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    target = gov / "exists"
    target.mkdir(parents=True)

    entry = create_workspace_folder("govdocs", "exists", ws)

    assert entry["filename"] == "exists - 副本"
    assert (gov / "exists - 副本").is_dir()


def test_create_workspace_document_docx_under_parent_dir(tmp_path: Path):
    ws = tmp_path / "agent"
    parent = ws / "govdocs" / "目录2"
    parent.mkdir(parents=True)

    detail = create_workspace_document(
        str(parent),
        "测试文档1.docx",
        ws,
        content_html="<p></p>",
        content_text="",
        source="manual",
    )

    target = parent / "测试文档1.docx"
    assert target.is_file()
    assert detail["filename"] == "测试文档1.docx"
    assert detail["source"] == "manual"
    assert detail["suffix"] == ".docx"


def test_create_workspace_document_with_text_content(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)

    detail = create_workspace_document(
        "govdocs",
        "note.txt",
        ws,
        content_text="hello\nworld",
    )

    assert (gov / "note.txt").read_text(encoding="utf-8") == "hello\nworld"
    assert detail["filename"] == "note.txt"


def test_create_workspace_document_uses_copy_suffix_when_duplicate(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    (gov / "exists.docx").write_bytes(b"x")

    detail = create_workspace_document("govdocs", "exists.docx", ws)

    assert detail["filename"] == "exists - 副本.docx"
    assert (gov / "exists - 副本.docx").is_file()
