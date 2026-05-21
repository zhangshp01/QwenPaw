# -*- coding: utf-8 -*-
"""Tests for govdocs listing and pagination."""
from pathlib import Path

import pytest
from fastapi import HTTPException

import io
import zipfile

from qwenpaw.app.workspace_paths import (
    annotate_govdocs_user,
    build_workspace_files_zip,
    delete_govdocs_file,
    delete_govdocs_files_batch,
    filter_govdocs_by_filename,
    govdocs_file_detail,
    build_govdocs_list_response,
    list_govdocs_files,
    govdocs_path_detail,
    rename_govdocs_file,
    resolve_govdocs_file_path,
    resolve_unique_govdocs_dest,
    resolve_workspace_download_files,
    resolve_workspace_list_directory,
    resolve_workspace_upload_directory,
    save_upload_to_workspace_directory,
    sort_govdocs_by_modified_time,
)


def test_list_govdocs_files_empty_when_missing_dir(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    assert list_govdocs_files(ws) == []


def test_list_govdocs_files_includes_subdirectories_and_folders(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    sub = gov / "reports" / "2026"
    sub.mkdir(parents=True)
    root_file = gov / "root.docx"
    nested_file = sub / "nested.docx"
    root_file.write_bytes(b"root")
    nested_file.write_bytes(b"nested")

    entries = list_govdocs_files(ws)
    by_key = {(e["type"], e["filename"]): e for e in entries}

    assert ("file", "root.docx") in by_key
    assert ("file", "nested.docx") in by_key
    assert ("directory", "reports") in by_key
    assert ("directory", "2026") in by_key
    assert by_key[("file", "nested.docx")]["relative_path"] == "reports/2026/nested.docx"


def test_list_govdocs_files_under_subdirectory(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    sub = gov / "reports" / "2026"
    other = gov / "other"
    sub.mkdir(parents=True)
    other.mkdir(parents=True)
    (sub / "nested.docx").write_bytes(b"nested")
    (other / "skip.docx").write_bytes(b"skip")
    (gov / "root.docx").write_bytes(b"root")

    list_root = resolve_workspace_list_directory("govdocs/reports", ws)
    entries = list_govdocs_files(ws, list_root=list_root)
    filenames = {e["filename"] for e in entries}

    assert filenames == {"2026", "nested.docx"}
    assert all("skip.docx" not in e.get("relative_path", "") for e in entries)
    assert all("root.docx" not in e.get("relative_path", "") for e in entries)


def test_resolve_workspace_list_directory_rejects_file(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    target = gov / "single.docx"
    target.write_bytes(b"x")

    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_list_directory(str(target), ws)
    assert exc_info.value.status_code == 400


def test_resolve_workspace_list_directory_missing_path(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()

    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_list_directory("govdocs/missing", ws)
    assert exc_info.value.status_code == 404


def test_filter_govdocs_by_relative_path():
    items = [
        {"filename": "nested.docx", "relative_path": "reports/2026/nested.docx"},
        {"filename": "2026", "type": "directory", "relative_path": "reports/2026"},
    ]
    matched = filter_govdocs_by_filename(items, "2026")
    assert len(matched) == 2


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


def test_build_govdocs_list_response_returns_full_list():
    items = [{"filename": "a.docx"}, {"filename": "b.docx"}]
    data = build_govdocs_list_response(items)
    assert data == {"list": items}
    assert "page" not in data
    assert "total" not in data


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
    new_path = rename_govdocs_file(target, "new.docx", ws)
    assert new_path.name == "new.docx"
    assert govdocs_file_detail(new_path)["size"] == 7

    delete_govdocs_file(new_path, ws)
    assert not new_path.exists()


def test_rename_and_delete_govdocs_directory(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    folder = gov / "目录2"
    nested = folder / "子目录"
    nested.mkdir(parents=True)
    (nested / "nested.docx").write_bytes(b"nested")

    target = resolve_govdocs_file_path(str(folder), ws)
    new_path = rename_govdocs_file(target, "新目录", ws)
    assert new_path.name == "新目录"
    assert new_path.is_dir()
    assert (new_path / "子目录" / "nested.docx").is_file()

    detail = govdocs_path_detail(new_path, ws)
    assert detail["type"] == "directory"
    assert detail["filename"] == "新目录"

    delete_govdocs_file(new_path, ws)
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


def test_batch_delete_govdocs_directory_recursive(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    folder = gov / "目录2"
    nested = folder / "子目录"
    nested.mkdir(parents=True)
    child = nested / "child.docx"
    child.write_bytes(b"child")

    result = delete_govdocs_files_batch(ws, [str(folder)])
    assert result["deletedCount"] == 1
    assert result["failedCount"] == 0
    assert not folder.exists()


def test_delete_govdocs_root_rejected(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)

    with pytest.raises(HTTPException) as exc_info:
        delete_govdocs_file(gov, ws)
    assert exc_info.value.status_code == 400
    assert gov.exists()


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


def test_resolve_workspace_download_files_single_and_zip(tmp_path: Path):
    ws = tmp_path / "agent"
    gov = ws / "govdocs"
    gov.mkdir(parents=True)
    a = gov / "a.docx"
    b = gov / "b.docx"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")

    one = resolve_workspace_download_files([str(a)], ws)
    assert len(one) == 1
    assert one[0].name == "a.docx"

    both = resolve_workspace_download_files(
        [str(a), "govdocs/b.docx"],
        ws,
    )
    assert len(both) == 2

    buf = build_workspace_files_zip(ws, both)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = sorted(zf.namelist())
    assert names == ["govdocs/a.docx", "govdocs/b.docx"]


def test_resolve_workspace_download_files_missing(tmp_path: Path):
    ws = tmp_path / "agent"
    ws.mkdir()
    with pytest.raises(HTTPException) as exc_info:
        resolve_workspace_download_files(["govdocs/missing.docx"], ws)
    assert exc_info.value.status_code == 404


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
        ws,
    )
    assert new_path.name == "existing - 副本.docx"
    assert src.exists() is False
