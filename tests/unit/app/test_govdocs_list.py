# -*- coding: utf-8 -*-
"""Tests for govdocs listing and pagination."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from qwenpaw.app.workspace_paths import (
    annotate_govdocs_user,
    delete_govdocs_file,
    delete_govdocs_files_batch,
    filter_govdocs_by_filename,
    govdocs_file_detail,
    list_govdocs_files,
    paginate_govdocs_list,
    rename_govdocs_file,
    resolve_govdocs_file_path,
    resolve_unique_govdocs_dest,
    resolve_workspace_upload_directory,
    save_upload_to_workspace_directory,
    sort_govdocs_by_modified_time,
)


def test_list_govdocs_files_empty_when_missing_dir(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    assert list_govdocs_files(ws) == []


def test_sort_govdocs_by_modified_time(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    older = gov / "a.docx"
    newer = gov / "b.docx"
    older.write_bytes(b"1")
    newer.write_bytes(b"22")
    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    files = list_govdocs_files(ws)
    desc = [f["filename"] for f in sort_govdocs_by_modified_time(files, "desc")]
    asc = [f["filename"] for f in sort_govdocs_by_modified_time(files, "asc")]
    assert desc == ["b.docx", "a.docx"]
    assert asc == ["a.docx", "b.docx"]


def test_filter_govdocs_by_filename():
    items = [
        {"filename": "关于大数据局职责的通知.docx"},
        {"filename": "公文排版稿.docx"},
        {"filename": "OTHER.DOCX"},
    ]
    assert len(filter_govdocs_by_filename(items, None)) == 3
    assert len(filter_govdocs_by_filename(items, "")) == 3
    matched = filter_govdocs_by_filename(items, "大数据")
    assert [i["filename"] for i in matched] == ["关于大数据局职责的通知.docx"]
    assert len(filter_govdocs_by_filename(items, "docx")) == 3


def test_annotate_govdocs_user():
    items = [{"filename": "a.docx"}]
    out = annotate_govdocs_user(items, "o7VWjb")
    assert out[0]["user"] == "o7VWjb"
    assert out[0]["filename"] == "a.docx"


def test_sort_govdocs_invalid_order():
    with pytest.raises(HTTPException) as exc_info:
        sort_govdocs_by_modified_time([], "invalid")
    assert exc_info.value.status_code == 400


def test_paginate_govdocs_list():
    items = [{"filename": f"f{i}.docx"} for i in range(5)]
    page1 = paginate_govdocs_list(items, page=1, page_size=2)
    assert page1["total"] == 5
    assert page1["totalPages"] == 3
    assert page1["page"] == 1
    assert page1["pageSize"] == 2
    assert len(page1["list"]) == 2
    assert page1["list"][0]["filename"] == "f0.docx"

    page3 = paginate_govdocs_list(items, page=3, page_size=2)
    assert len(page3["list"]) == 1


def test_paginate_invalid_page():
    with pytest.raises(HTTPException) as exc_info:
        paginate_govdocs_list([], page=0, page_size=10)
    assert exc_info.value.status_code == 400


def test_paginate_invalid_page_size():
    with pytest.raises(HTTPException) as exc_info:
        paginate_govdocs_list([], page=1, page_size=0)
    assert exc_info.value.status_code == 400


def test_resolve_govdocs_file_path_rejects_outside_govdocs(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    outside = ws / "other.docx"
    outside.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc_info:
        resolve_govdocs_file_path(str(outside), ws)
    assert exc_info.value.status_code == 403


def test_govdocs_file_detail_includes_content(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    target = gov / "sample.docx"
    target.write_bytes(b"not-a-real-docx")
    target = resolve_govdocs_file_path("govdocs/sample.docx", ws)
    detail = govdocs_file_detail(target)
    assert detail["contentBase64"] != ""
    assert detail["contentType"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert detail["contentOmitted"] is False


def test_govdocs_file_detail_empty_file(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    empty = gov / "empty.docx"
    empty.write_bytes(b"")
    detail = govdocs_file_detail(resolve_govdocs_file_path(str(empty), ws))
    assert detail["size"] == 0
    assert detail["contentBase64"] == ""
    assert detail["contentText"] in ("", None)


def test_rename_and_delete_govdocs_file(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    src = gov / "old.docx"
    src.write_bytes(b"content")

    target = resolve_govdocs_file_path("govdocs/old.docx", ws)
    new_path = rename_govdocs_file(target, "new.docx")
    assert new_path.name == "new.docx"
    assert govdocs_file_detail(new_path)["size"] == 7

    delete_govdocs_file(new_path)
    assert not new_path.exists()


def test_resolve_unique_govdocs_dest_copy_suffixes(tmp_path: Path):
    gov = tmp_path / "govdocs"
    gov.mkdir(parents=True)
    (gov / "target.docx").write_bytes(b"1")
    (gov / "target - 副本.docx").write_bytes(b"2")

    dest = resolve_unique_govdocs_dest(gov, "target.docx")
    assert dest.name == "target - 副本 (2).docx"


def test_batch_delete_govdocs_files(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    a = gov / "a.docx"
    b = gov / "b.docx"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    missing = "govdocs/missing.docx"

    result = delete_govdocs_files_batch(
        ws,
        ["govdocs/a.docx", "govdocs/b.docx", missing],
    )
    assert result["deletedCount"] == 2
    assert result["failedCount"] == 1
    assert not a.exists()
    assert not b.exists()
    assert result["failed"][0]["path"] == missing


def test_save_upload_to_workspace_directory(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    entry = save_upload_to_workspace_directory(
        resolve_workspace_upload_directory(str(gov), ws),
        "sample.docx",
        b"docx-content",
    )
    assert entry["filename"] == "sample.docx"
    assert (gov / "sample.docx").read_bytes() == b"docx-content"
    assert entry["suffix"] == ".docx"


def test_save_upload_unique_name_when_exists(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    (gov / "dup.docx").write_bytes(b"old")
    target_dir = resolve_workspace_upload_directory(str(gov), ws)
    entry = save_upload_to_workspace_directory(target_dir, "dup.docx", b"new")
    assert entry["filename"] == "dup - 副本.docx"
    assert (gov / "dup - 副本.docx").read_bytes() == b"new"


def test_resolve_workspace_upload_directory_rejects_outside(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_upload_directory(str(outside), ws)
    assert exc_info.value.status_code == 403


def test_rename_uses_copy_when_target_exists(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    (gov / "existing.docx").write_bytes(b"exists")
    src = gov / "source.docx"
    src.write_bytes(b"source")

    new_path = rename_govdocs_file(
        resolve_govdocs_file_path("govdocs/source.docx", ws),
        "existing.docx",
    )
    assert new_path.name == "existing - 副本.docx"
    assert src.exists() is False
