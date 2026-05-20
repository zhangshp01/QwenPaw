# -*- coding: utf-8 -*-
"""Resolve and validate workspace file write targets."""
from __future__ import annotations

import base64
import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..constant import SECRET_DIR

GOVDOCS_SUBDIR = "govdocs"
DEFAULT_GOVDOCS_PAGE = 1
DEFAULT_GOVDOCS_PAGE_SIZE = 20
MAX_GOVDOCS_PAGE_SIZE = 100
GOVDOCS_SORT_ASC = "asc"
GOVDOCS_SORT_DESC = "desc"
DEFAULT_GOVDOCS_SORT_ORDER = GOVDOCS_SORT_DESC

# Max file size for embedding content in ``file_detail`` (10 MiB).
MAX_GOVDOCS_DETAIL_CONTENT_BYTES = 10 * 1024 * 1024

# Max binary payload for ``PUT /api/agentloop/workspace/files_binary`` (50 MiB).
MAX_WORKSPACE_BINARY_WRITE_BYTES = 50 * 1024 * 1024

# Max files and total bytes per ``GET /api/agentloop/workspace/download``.
MAX_WORKSPACE_DOWNLOAD_FILES = 100
MAX_WORKSPACE_DOWNLOAD_TOTAL_BYTES = 200 * 1024 * 1024

logger = logging.getLogger(__name__)

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_SUFFIX_MIME: dict[str, str] = {
    ".docx": _DOCX_MIME,
    ".doc": "application/msword",
}


def write_bytes_to_path(target: Path, data: bytes) -> None:
    """Create parent directories and write *data* to *target*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def normalize_path_argument(path_str: str) -> str:
    """Normalize path query values (e.g. ``/C:/foo`` → ``C:/foo`` on Windows)."""
    normalized = (path_str or "").strip()
    if (
        len(normalized) >= 4
        and normalized[0] == "/"
        and normalized[2] == ":"
        and normalized[1].isalpha()
    ):
        normalized = normalized[1:]
    return normalized


def resolve_workspace_write_path(path_str: str, workspace_dir: Path) -> Path:
    """Resolve *path_str* to an absolute path under *workspace_dir*.

    Relative paths are resolved from *workspace_dir*. Absolute paths are allowed
    only when they resolve inside the workspace root.

    Raises:
        HTTPException: 400 for empty path, 403 for paths outside workspace or
            sensitive directories.
    """
    raw = normalize_path_argument(path_str)
    if not raw:
        raise HTTPException(status_code=400, detail="Path must not be empty")

    workspace_root = workspace_dir.expanduser().resolve()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (workspace_root / candidate).resolve()

    try:
        target.relative_to(workspace_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Path must be inside the agent workspace",
        ) from exc

    secret_root = SECRET_DIR.resolve()
    try:
        target.relative_to(secret_root)
        raise HTTPException(
            status_code=403,
            detail="Writing to the secrets directory is not allowed",
        )
    except ValueError:
        pass

    return target


def resolve_workspace_upload_directory(
    directory_str: str,
    workspace_dir: Path,
) -> Path:
    """Resolve *directory_str* to a directory path under *workspace_dir*."""
    target = resolve_workspace_write_path(directory_str, workspace_dir)
    if target.exists() and not target.is_dir():
        raise HTTPException(
            status_code=400,
            detail="directory must refer to a directory, not a file",
        )
    return target


def save_upload_to_workspace_directory(
    directory: Path,
    upload_filename: str,
    data: bytes,
) -> dict[str, Any]:
    """Write uploaded bytes into *directory* using a safe, unique file name."""
    safe_name = Path((upload_filename or "").strip()).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Upload filename must not be empty")

    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise HTTPException(
            status_code=400,
            detail="directory must refer to a directory, not a file",
        )

    dest = resolve_unique_govdocs_dest(directory, safe_name)
    write_bytes_to_path(dest, data)
    return _file_entry(dest)


def mime_type_for_suffix(suffix: str) -> str:
    """Return Content-Type for a file suffix."""
    return _mime_for_suffix(suffix)


def resolve_workspace_download_file(
    path_str: str,
    workspace_dir: Path,
) -> Path:
    """Resolve *path_str* to an existing regular file under *workspace_dir*."""
    target = resolve_workspace_write_path(path_str, workspace_dir)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path_str}")
    return target.resolve()


def resolve_workspace_download_files(
    path_strings: list[str],
    workspace_dir: Path,
) -> list[Path]:
    """Resolve and de-duplicate download targets; enforce count and size limits."""
    if not path_strings:
        raise HTTPException(status_code=400, detail="At least one path is required")
    if len(path_strings) > MAX_WORKSPACE_DOWNLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_WORKSPACE_DOWNLOAD_FILES} paths per request",
        )

    seen: set[str] = set()
    files: list[Path] = []
    total_bytes = 0

    for raw in path_strings:
        label = (raw or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Path must not be empty")
        target = resolve_workspace_download_file(label, workspace_dir)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        total_bytes += target.stat().st_size
        if total_bytes > MAX_WORKSPACE_DOWNLOAD_TOTAL_BYTES:
            limit_mb = MAX_WORKSPACE_DOWNLOAD_TOTAL_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=f"Total download size exceeds {limit_mb} MB",
            )
        files.append(target)

    if not files:
        raise HTTPException(status_code=400, detail="No valid file paths provided")
    return files


def build_workspace_files_zip(
    workspace_dir: Path,
    files: list[Path],
) -> io.BytesIO:
    """Zip *files* using paths relative to *workspace_dir* as archive names."""
    workspace_root = workspace_dir.expanduser().resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_arcnames: set[str] = set()
        for file_path in files:
            try:
                arcname = file_path.relative_to(workspace_root).as_posix()
            except ValueError:
                arcname = file_path.name
            if arcname in used_arcnames:
                parent = file_path.parent.name or "files"
                arcname = f"{parent}/{file_path.name}"
            used_arcnames.add(arcname)
            zf.write(file_path, arcname)
    buf.seek(0)
    return buf


def _govdocs_dir(workspace_dir: Path) -> Path:
    return workspace_dir.expanduser().resolve() / GOVDOCS_SUBDIR


def list_govdocs_files(workspace_dir: Path) -> list[dict[str, Any]]:
    """List regular files in ``govdocs/`` (unsorted; use sort/filter helpers)."""
    root = _govdocs_dir(workspace_dir)
    if not root.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        entries.append(_file_entry(path))
    return entries


def filter_govdocs_by_filename(
    items: list[dict[str, Any]],
    filename_query: str | None,
) -> list[dict[str, Any]]:
    """Keep items whose ``filename`` contains *filename_query* (case-insensitive)."""
    if not filename_query or not filename_query.strip():
        return items
    needle = filename_query.strip().casefold()
    return [
        item
        for item in items
        if needle in item.get("filename", "").casefold()
    ]


def sort_govdocs_by_modified_time(
    items: list[dict[str, Any]],
    sort_order: str = DEFAULT_GOVDOCS_SORT_ORDER,
) -> list[dict[str, Any]]:
    """Sort by ``modified_time`` ascending or descending."""
    order = (sort_order or DEFAULT_GOVDOCS_SORT_ORDER).strip().lower()
    if order not in (GOVDOCS_SORT_ASC, GOVDOCS_SORT_DESC):
        raise HTTPException(
            status_code=400,
            detail=f"sortOrder must be '{GOVDOCS_SORT_ASC}' or '{GOVDOCS_SORT_DESC}'",
        )
    reverse = order == GOVDOCS_SORT_DESC
    return sorted(items, key=lambda item: item["modified_time"], reverse=reverse)


def annotate_govdocs_user(
    items: list[dict[str, Any]],
    user: str,
) -> list[dict[str, Any]]:
    """Add ``user`` to each list entry."""
    return [{**item, "user": user} for item in items]


def paginate_govdocs_list(
    items: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Return a page slice plus pagination metadata."""
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    if page_size < 1 or page_size > MAX_GOVDOCS_PAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"pageSize must be between 1 and {MAX_GOVDOCS_PAGE_SIZE}",
        )

    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    end = start + page_size
    page_list = items[start:end]

    return {
        "list": page_list,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


def resolve_govdocs_file_path(path_str: str, workspace_dir: Path) -> Path:
    """Resolve *path_str* to a file path that must lie under ``govdocs/``."""
    target = resolve_workspace_write_path(path_str, workspace_dir)
    govdocs_root = _govdocs_dir(workspace_dir).resolve()
    try:
        target.relative_to(govdocs_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Path must be under the govdocs directory",
        ) from exc
    return target


def _file_entry(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "filename": path.name,
        "path": str(path.resolve()),
        "size": stat.st_size,
        "suffix": path.suffix.lower(),
        "created_time": datetime.fromtimestamp(
            stat.st_ctime,
            tz=timezone.utc,
        ).isoformat(),
        "modified_time": datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
    }


def _mime_for_suffix(suffix: str) -> str:
    return _SUFFIX_MIME.get(suffix.lower(), "application/octet-stream")


def _extract_docx_plain_text(path: Path) -> str | None:
    """Extract paragraph text from a ``.docx`` file (requires ``python-docx``)."""
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("python-docx not installed; contentText omitted for %s", path)
        return None

    try:
        document = Document(str(path))
        parts = [paragraph.text for paragraph in document.paragraphs]
        return "\n".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to extract docx text from %s: %s", path, exc)
        return None


def govdocs_file_detail(path: Path) -> dict[str, Any]:
    """Return metadata and file content for an existing ``govdocs`` file."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    detail = _file_entry(path)
    size = int(detail["size"])
    detail["contentType"] = _mime_for_suffix(path.suffix)

    if size > MAX_GOVDOCS_DETAIL_CONTENT_BYTES:
        detail["contentOmitted"] = True
        detail["contentOmittedReason"] = (
            f"File exceeds {MAX_GOVDOCS_DETAIL_CONTENT_BYTES // (1024 * 1024)} MB; "
            "content not included in response"
        )
        detail["contentBase64"] = None
        detail["contentText"] = None
        return detail

    raw = path.read_bytes()
    detail["contentBase64"] = (
        base64.b64encode(raw).decode("ascii") if raw else ""
    )
    detail["contentOmitted"] = False

    if path.suffix.lower() == ".docx":
        detail["contentText"] = _extract_docx_plain_text(path)
    else:
        detail["contentText"] = None

    return detail


_GOVDOCS_COPY_LABEL = " - 副本"
_MAX_GOVDOCS_COPY_SUFFIX_ATTEMPTS = 999


def resolve_unique_govdocs_dest(
    parent: Path,
    desired_filename: str,
    *,
    exclude: Path | None = None,
) -> Path:
    """Return a destination path for *desired_filename*, avoiding name clashes.

    If the desired name is taken, append `` - 副本``, then `` - 副本 (2)``, etc.
    """
    safe_name = Path(desired_filename.strip()).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="newFilename must not be empty")

    def _available(candidate: Path) -> bool:
        if not candidate.exists():
            return True
        if exclude is not None and candidate.resolve() == exclude.resolve():
            return True
        return False

    first = parent / safe_name
    if _available(first):
        return first

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix

    copy_first = parent / f"{stem}{_GOVDOCS_COPY_LABEL}{suffix}"
    if _available(copy_first):
        return copy_first

    for n in range(2, _MAX_GOVDOCS_COPY_SUFFIX_ATTEMPTS + 1):
        candidate = parent / f"{stem}{_GOVDOCS_COPY_LABEL} ({n}){suffix}"
        if _available(candidate):
            return candidate

    raise HTTPException(
        status_code=500,
        detail="Could not allocate a unique file name",
    )


def rename_govdocs_file(path: Path, new_filename: str) -> Path:
    """Rename *path* to *new_filename* (basename only) within ``govdocs/``."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    dest = resolve_unique_govdocs_dest(
        path.parent,
        new_filename,
        exclude=path.resolve(),
    )
    path.rename(dest)
    return dest.resolve()


def delete_govdocs_file(path: Path) -> None:
    """Delete a regular file under ``govdocs/``."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    path.unlink()


MAX_GOVDOCS_BATCH_DELETE = 100


def delete_govdocs_files_batch(
    workspace_dir: Path,
    path_strings: list[str],
) -> dict[str, Any]:
    """Delete multiple ``govdocs`` files; continue on individual failures."""
    if not path_strings:
        raise HTTPException(status_code=400, detail="paths must not be empty")
    if len(path_strings) > MAX_GOVDOCS_BATCH_DELETE:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_GOVDOCS_BATCH_DELETE} paths per request",
        )

    deleted: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    for raw in path_strings:
        path_label = (raw or "").strip()
        if not path_label:
            failed.append({"path": raw or "", "detail": "Path must not be empty"})
            continue
        try:
            target = resolve_govdocs_file_path(path_label, workspace_dir)
            delete_govdocs_file(target)
            deleted.append({"path": str(target.resolve())})
        except HTTPException as exc:
            detail = exc.detail
            if not isinstance(detail, str):
                detail = str(detail)
            failed.append({"path": path_label, "detail": detail})
        except OSError as exc:
            failed.append({"path": path_label, "detail": str(exc)})

    return {
        "deleted": deleted,
        "failed": failed,
        "deletedCount": len(deleted),
        "failedCount": len(failed),
    }
