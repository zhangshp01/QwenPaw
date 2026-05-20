# -*- coding: utf-8 -*-
"""Resolve and validate workspace file write targets."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..constant import SECRET_DIR

# Max binary payload for ``PUT /api/agentloop/workspace/files_binary`` (50 MiB).
MAX_WORKSPACE_BINARY_WRITE_BYTES = 50 * 1024 * 1024


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
