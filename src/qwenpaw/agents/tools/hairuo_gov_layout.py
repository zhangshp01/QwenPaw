# -*- coding: utf-8 -*-
"""HaiRuo layout template list (layoutTemplate API) for :func:`gov_document_layout`.

Configuration is **re-read on every call** (no in-module cache of merged values):

1. Optional JSON files (see :func:`_defaults_file_candidates`): ``GOV_LAYOUT_DEFAULTS``,
   tool parameter ``layout_defaults_path``, or ``gov_document_layout_defaults.json``
   next to ``config.json``. **No** ``gov-document-layout/`` workspace folder is read.
2. **Merged with** ``config.json`` → ``tools.gov_document_layout_hairuo`` (set
   fields override file values). ``load_config`` uses mtime cache.
3. ``HAIRUOKB_API_URL``, ``HAIRUOKB_COOKIE``, ``HAIRUOKB_API_KEY`` still override
   ``base_url`` / credentials from the merged dict.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .file_io import _resolve_file_path

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
HAIRUOKB_API_URL_ENV = "HAIRUOKB_API_URL"
HAIRUOKB_API_KEY_ENV = "HAIRUOKB_API_KEY"
HAIRUOKB_COOKIE_ENV = "HAIRUOKB_COOKIE"
GOV_LAYOUT_DEFAULTS_ENV = "GOV_LAYOUT_DEFAULTS"
PATH_TEMPLATE = "/areport/report-agent/v2/layoutTemplate"


class GovLayoutHttpError(RuntimeError):
    """Raised when layoutTemplate request fails or API returns non-zero code."""


def _defaults_file_candidates(
    explicit_defaults_path: str,
) -> list[Path]:
    """JSON files that may supply HaiRuo defaults (never under ``gov-document-layout/``)."""
    paths: list[Path] = []
    env_path = (os.environ.get(GOV_LAYOUT_DEFAULTS_ENV) or "").strip()
    if env_path:
        paths.append(Path(env_path).expanduser())
    hint = (explicit_defaults_path or "").strip()
    if hint:
        try:
            paths.append(Path(_resolve_file_path(hint)))
        except Exception:
            paths.append(Path(hint).expanduser())
    try:
        from qwenpaw.config.utils import get_config_path

        paths.append(get_config_path().parent / "gov_document_layout_defaults.json")
    except Exception:
        pass
    return paths


def _load_first_layout_defaults_file(
    *,
    explicit_defaults_path: str,
) -> dict[str, Any]:
    """Return the first readable defaults JSON as a dict, or ``{}``."""
    for candidate in _defaults_file_candidates(explicit_defaults_path):
        if not candidate.is_file():
            continue
        try:
            with open(candidate, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return dict(data)
    return {}


def _project_hairuo_overlay() -> dict[str, Any]:
    """Non-empty / set fields from ``config.json`` ``tools.gov_document_layout_hairuo``."""
    try:
        from qwenpaw.config.utils import load_config

        cfg = load_config()
        raw = cfg.tools.gov_document_layout_hairuo.model_dump(exclude_none=True)
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str) and not value.strip():
            continue
        out[key] = value
    return out


def load_layout_runtime_config(
    *,
    explicit_defaults_path: str = "",
) -> dict[str, Any]:
    """Merge optional JSON defaults with ``config.json`` ``tools.gov_document_layout_hairuo``."""
    file_part = _load_first_layout_defaults_file(
        explicit_defaults_path=explicit_defaults_path,
    )
    overlay = _project_hairuo_overlay()
    return {**file_part, **overlay}


def _resolve_base_url(defaults: dict[str, Any]) -> str:
    file_url = (defaults.get("base_url") or "").strip()
    base_url = (
        (os.environ.get(HAIRUOKB_API_URL_ENV) or "").strip() or file_url
    )
    if not base_url:
        raise GovLayoutHttpError(
            f"未配置 HaiRuo 网关：请设置环境变量 {HAIRUOKB_API_URL_ENV}，"
            "或在 config.json 的 tools.gov_document_layout_hairuo.base_url 中填写，"
            "或使用 GOV_LAYOUT_DEFAULTS / 与 config.json 同目录的 "
            "gov_document_layout_defaults.json / 工具参数 layout_defaults_path 提供 base_url。",
        )
    parsed = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise GovLayoutHttpError(
            "base_url 无效，请使用带 scheme 的绝对地址，例如 https://host:18085。",
        )
    return base_url.rstrip("/")


def _resolve_credentials(defaults: dict[str, Any]) -> tuple[str | None, str | None]:
    file_key = (defaults.get("api_key") or "").strip()
    file_cookie = (defaults.get("cookie") or "").strip()
    key = (os.environ.get(HAIRUOKB_API_KEY_ENV) or "").strip() or file_key
    cookie = (os.environ.get(HAIRUOKB_COOKIE_ENV) or "").strip() or file_cookie
    api_key = key if key else None
    cookie_out = cookie if cookie else None
    if not api_key and not cookie_out:
        raise GovLayoutHttpError(
            f"未配置认证：请设置 {HAIRUOKB_COOKIE_ENV} 和/或 {HAIRUOKB_API_KEY_ENV}，"
            "或在 config.json 的 tools.gov_document_layout_hairuo 中填写 cookie / api_key，"
            "或通过 GOV_LAYOUT_DEFAULTS / gov_document_layout_defaults.json / layout_defaults_path 提供。",
        )
    return api_key, cookie_out


def _build_layout_url(
    base_url: str,
    *,
    page: int,
    page_size: int,
    template_title: str,
) -> str:
    path = f"{base_url}{PATH_TEMPLATE}/{int(page)}/{int(page_size)}"
    query = urllib.parse.urlencode({"templateTitle": template_title})
    return f"{path}?{query}"


def _decode_json_response(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise GovLayoutHttpError("服务器返回了非 JSON 内容。") from exc
    if not isinstance(payload, dict):
        raise GovLayoutHttpError("服务器返回的 JSON 不是对象。")
    return payload


def _decode_json_body(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        text = body.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _request_json(
    url: str,
    *,
    api_key: str | None,
    cookie: str | None,
    verify_ssl: bool,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if cookie:
        headers["Cookie"] = cookie
    request_obj = urllib.request.Request(url, headers=headers, method="GET")
    ssl_ctx: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            request_obj,
            timeout=HTTP_TIMEOUT,
            context=ssl_ctx,
        ) as response:
            return _decode_json_response(response.read())
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        payload = _decode_json_body(body_bytes)
        message: str | None = None
        if isinstance(payload, dict):
            m = payload.get("message")
            if isinstance(m, str) and m.strip():
                message = m.strip()
        if message:
            raise GovLayoutHttpError(message) from None
        raise GovLayoutHttpError(f"HTTP {exc.code}，请求 layoutTemplate 失败。") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise GovLayoutHttpError(f"网络请求失败：{reason}") from exc


def fetch_layout_template_result_list(
    template_title: str,
    *,
    page: int = 1,
    page_size: int = 10,
    explicit_defaults_path: str = "",
) -> dict[str, Any]:
    """Call HaiRuo layoutTemplate and return ``{"total", "list"}`` from ``data``."""
    title = (template_title or "").strip()
    if not title:
        raise GovLayoutHttpError("template_title 不能为空。")

    defaults = load_layout_runtime_config(explicit_defaults_path=explicit_defaults_path)
    base = _resolve_base_url(defaults)
    api_key, cookie = _resolve_credentials(defaults)
    verify_ssl = bool(defaults.get("verify_ssl", True))

    pg = max(1, int(page))
    pz = max(1, min(100, int(page_size)))
    url = _build_layout_url(base, page=pg, page_size=pz, template_title=title)
    host = urllib.parse.urlsplit(base).netloc or base
    logger.info(
        "HaiRuo layoutTemplate request: template_title=%r page=%s page_size=%s host=%s verify_ssl=%s",
        title,
        pg,
        pz,
        host,
        verify_ssl,
    )
    payload = _request_json(url, api_key=api_key, cookie=cookie, verify_ssl=verify_ssl)
    code = payload.get("code")
    if code != 0:
        message = payload.get("message") or f"接口返回 code={code}。"
        logger.warning(
            "HaiRuo layoutTemplate response: non-zero code=%s message=%s template_title=%r",
            code,
            message,
            title,
        )
        raise GovLayoutHttpError(str(message))
    data = payload.get("data")
    _KEEP_KEYS = ("id", "templateTitle", "industryTag", "share", "templateType", "layoutContent", "count") 

    def _pick_item(x: dict) -> dict:
        return {k: x[k] for k in _KEEP_KEYS if k in x}

    if isinstance(data, dict):
        lst = data.get("list")
        items = [_pick_item(x) for x in lst if isinstance(x, dict)] if isinstance(lst, list) else []
        tv = data.get("total", len(items))
        try:
            total = int(tv)
        except (TypeError, ValueError):
            total = len(items)
        logger.info(
            "HaiRuo layoutTemplate success: template_title=%r total=%s list_items=%s",
            title,
            total,
            len(items),
        )
        return {"total": total, "list": items}
    logger.info(
        "HaiRuo layoutTemplate success: template_title=%r empty_or_non_object_data=%r",
        title,
        type(data).__name__,
    )
    return {"total": 0, "list": []}
