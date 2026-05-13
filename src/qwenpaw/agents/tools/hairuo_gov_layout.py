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
4. Optional layout recommendation LLM: ``GOV_LAYOUT_RECOMMEND_CHAT_URL``,
   ``GOV_LAYOUT_RECOMMEND_MODEL``, ``GOV_LAYOUT_RECOMMEND_API_KEY`` override
   ``tools.gov_document_layout_hairuo.recommend_*`` when set.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .file_io import _resolve_file_path

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 600
HAIRUOKB_API_URL_ENV = "HAIRUOKB_API_URL"
HAIRUOKB_API_KEY_ENV = "HAIRUOKB_API_KEY"
HAIRUOKB_COOKIE_ENV = "HAIRUOKB_COOKIE"
GOV_LAYOUT_DEFAULTS_ENV = "GOV_LAYOUT_DEFAULTS"
GOV_LAYOUT_RECOMMEND_CHAT_URL_ENV = "GOV_LAYOUT_RECOMMEND_CHAT_URL"
GOV_LAYOUT_RECOMMEND_MODEL_ENV = "GOV_LAYOUT_RECOMMEND_MODEL"
GOV_LAYOUT_RECOMMEND_API_KEY_ENV = "GOV_LAYOUT_RECOMMEND_API_KEY"
PATH_TEMPLATE = "/areport/report-agent/v2/layoutTemplate/get"

_LAYOUT_RECOMMEND_SYSTEM_TMPL = """【角色】
你是一位精通党政机关公文格式与行文规则的资深秘书，熟悉党委系统与行政系统的公文差异。

【任务】
根据以下【模板列表】以及用户输入的【公文场景】，从列表中筛选出**两个最合适**的公文模板。

【判断标准（严格按优先级执行）】

1. **行文方向匹配（核心）**
   - 内容是**向上级请示、报告、意见** → 必须选“上行文”
   - 内容是**向下级部署、通知、批复** 或 **平级商洽、函** → 选“平行、下行文”（若模板标题未明确区分，优先选非上行文）

2. **党委 vs 行政（关键区分）**
   - 公文涉及**党委、党组、党务、组织人事、政治建设** → 优先选“党委”模板
   - 公文涉及**政府行政业务、财政、项目、行政执法** → 优先选“行政”模板

3. **行业标签辅助**
   - `industryTag` 与用户场景匹配度（如“昆明”优于“通用”）

4. **使用热度（最低优先级）**
   - 以上条件持平的情况下，选择 `count` 值更高的模板

【模板列表】
{template_list_json}

【输出格式要求】

只输出以下JSON格式，不要输出任何其他文字：
[{{"template": "模板标题01", "id": "模板ID02"}}]

- 禁止输出任何分析、思考、说明或额外文字
- 禁止输出换行、空行或多余标点
- 如果无法确定，输出：无法匹配
"""


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
    base_url: str
) -> str:
    path = f"{base_url}{PATH_TEMPLATE}"
    return f"{path}"


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


def _layout_recommend_chat_url(defaults: dict[str, Any]) -> str:
    env_u = (os.environ.get(GOV_LAYOUT_RECOMMEND_CHAT_URL_ENV) or "").strip()
    if env_u:
        return env_u
    return (defaults.get("recommend_chat_url") or "").strip()


def _layout_recommend_model_id(defaults: dict[str, Any]) -> str:
    env_m = (os.environ.get(GOV_LAYOUT_RECOMMEND_MODEL_ENV) or "").strip()
    if env_m:
        return env_m
    m = (defaults.get("recommend_model") or "").strip()
    return m or "Qwen3-235B-A22B-FP8"


def _layout_recommend_api_key(defaults: dict[str, Any]) -> str | None:
    env_k = (os.environ.get(GOV_LAYOUT_RECOMMEND_API_KEY_ENV) or "").strip()
    if env_k:
        return env_k
    k = (defaults.get("recommend_api_key") or "").strip()
    return k if k else None


def _layout_recommend_verify_ssl(defaults: dict[str, Any]) -> bool:
    rv = defaults.get("recommend_verify_ssl")
    if rv is not None:
        return bool(rv)
    return bool(defaults.get("verify_ssl", True))


def _minimal_templates_for_llm(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "templateTitle", "industryTag", "templateType", "count")
    out: list[dict[str, Any]] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        out.append({k: x[k] for k in keys if k in x})
    return out


def _strip_markdown_code_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3].strip()
    return t


def _json_array_from_llm_text(text: str) -> list[Any] | None:
    t = _strip_markdown_code_fence(text).strip()
    if not t:
        return None
    if t == "无法匹配":
        return []
    if "无法匹配" in t and "[" not in t:
        return []
    try:
        parsed: Any = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\[.*]", t, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, list) else None


def _parse_recommendation_entries(content: str) -> list[tuple[str, str]]:
    """Parse model output into up to two ``(template_title_hint, id)`` pairs."""
    arr = _json_array_from_llm_text(content)
    if arr is None:
        return []
    pairs: list[tuple[str, str]] = []
    for el in arr:
        if not isinstance(el, dict):
            continue
        tid = el.get("id")
        if not isinstance(tid, str):
            tid = str(tid) if tid is not None else ""
        title = el.get("template")
        if not isinstance(title, str):
            tt2 = el.get("templateTitle")
            title = tt2 if isinstance(tt2, str) else ""
        tid = tid.strip()
        title = title.strip()
        if tid or title:
            pairs.append((title, tid))
        if len(pairs) >= 2:
            break
    return pairs


def _match_recommended_items(
    pairs: list[tuple[str, str]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for x in items:
        if not isinstance(x, dict):
            continue
        iid = x.get("id")
        if iid is not None:
            by_id[str(iid).strip()] = x
        tt = x.get("templateTitle")
        if isinstance(tt, str) and tt.strip():
            by_title[tt.strip()] = x
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for title_hint, tid in pairs:
        cand: dict[str, Any] | None = None
        if tid:
            cand = by_id.get(tid)
        if cand is None and title_hint:
            cand = by_title.get(title_hint)
        if cand is None:
            continue
        key = str(cand.get("id", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(cand))
        if len(out) >= 2:
            break
    return out


def _post_layout_recommend_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str | None,
    verify_ssl: bool,
) -> dict[str, Any] | None:
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request_obj = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    ssl_ctx: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            request_obj,
            timeout=HTTP_TIMEOUT,
            context=ssl_ctx,
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read()[:800]
        except Exception:
            detail = b""
        logger.warning(
            "layout recommend chat HTTP %s: %r",
            exc.code,
            detail,
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning("layout recommend chat network error: %s", exc)
        return None
    except Exception as exc:
        logger.warning("layout recommend chat request error: %s", exc)
        return None
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("layout recommend chat invalid JSON: %s", exc)
        return None
    return out if isinstance(out, dict) else None


def _recommend_layout_templates_via_llm(
    defaults: dict[str, Any],
    items: list[dict[str, Any]],
    user_scene: str,
) -> list[dict[str, Any]]:
    chat_url = _layout_recommend_chat_url(defaults)
    if not chat_url:
        return []
    if not items:
        return []
    mini = _minimal_templates_for_llm(items)
    if not mini:
        return []
    system_text = _LAYOUT_RECOMMEND_SYSTEM_TMPL.format(
        template_list_json=json.dumps(mini, ensure_ascii=False),
    )
    model_id = _layout_recommend_model_id(defaults)
    rec_key = _layout_recommend_api_key(defaults)
    verify = _layout_recommend_verify_ssl(defaults)
    payload = {
        "model": model_id,
        "temperature": 0,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_scene},
        ],
    }
    data = _post_layout_recommend_json(
        chat_url,
        payload,
        api_key=rec_key,
        verify_ssl=verify,
    )
    if not data:
        return []
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        logger.warning("layout recommend chat: missing choices")
        return []
    msg = choices[0] if isinstance(choices[0], dict) else None
    inner = msg.get("message") if isinstance(msg, dict) else None
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, str) or not content.strip():
        return []
    pairs = _parse_recommendation_entries(content)
    matched = _match_recommended_items(pairs, items)
    logger.info(
        "HaiRuo layoutTemplate recommend: user_scene=%r matched=%s",
        user_scene[:200] if user_scene else "",
        len(matched),
    )
    return matched


def _attach_recommended_if_configured(
    result: dict[str, Any],
    defaults: dict[str, Any],
    items: list[dict[str, Any]],
    user_scene: str,
) -> None:
    if not _layout_recommend_chat_url(defaults):
        return
    if not items:
        result["recommended"] = []
        return
    result["recommended"] = _recommend_layout_templates_via_llm(
        defaults,
        items,
        user_scene,
    )


def fetch_layout_template_result_list(
    template_title: str,
    *,
    page: int = 1,
    page_size: int = 10000,
    explicit_defaults_path: str = "",
) -> dict[str, Any]:
    """Call HaiRuo layoutTemplate and return ``data`` as ``{"total", "list"}``.

    Supports ``data`` as either a **list** of template objects (``.../layoutTemplate/get``)
    or a legacy **object** with ``{"list", "total"}``. When ``data`` is a top-level array,
    ``list`` and ``total`` reflect the **full** response (``page`` / ``page_size`` are ignored).

    When ``recommend_chat_url`` is set (config or ``GOV_LAYOUT_RECOMMEND_CHAT_URL``),
    the response also includes ``recommended``: up to two list entries chosen by an
    OpenAI-compatible chat model from ``template_title`` as the user 公文场景描述.
    """
    title = (template_title or "").strip()
    if not title:
        raise GovLayoutHttpError("template_title 不能为空。")

    defaults = load_layout_runtime_config(explicit_defaults_path=explicit_defaults_path)
    base = _resolve_base_url(defaults)
    api_key, cookie = _resolve_credentials(defaults)
    verify_ssl = bool(defaults.get("verify_ssl", True))

    url = _build_layout_url(base)
    host = urllib.parse.urlsplit(base).netloc or base
    logger.info(
        "HaiRuo layoutTemplate request:  host=%s verify_ssl=%s",
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

    items: list[dict[str, Any]]
    total: int
    for_recommend: list[dict[str, Any]]

    if isinstance(data, list):
        raw = [x for x in data if isinstance(x, dict)]
        for_recommend = [_pick_item(x) for x in raw]
        total = len(for_recommend)
        items = for_recommend
    elif isinstance(data, dict):
        lst = data.get("list")
        if isinstance(lst, list):
            for_recommend = [_pick_item(x) for x in lst if isinstance(x, dict)]
            items = for_recommend
            tv = data.get("total", len(items))
            try:
                total = int(tv)
            except (TypeError, ValueError):
                total = len(items)
        else:
            for_recommend = []
            items = []
            tv = data.get("total", 0)
            try:
                total = int(tv)
            except (TypeError, ValueError):
                total = 0
    else:
        logger.info(
            "HaiRuo layoutTemplate success: template_title=%r empty_or_non_object_data=%r",
            title,
            type(data).__name__,
        )
        result = {"total": 0, "list": []}
        _attach_recommended_if_configured(result, defaults, [], title)
        return result

    logger.info(
        "HaiRuo layoutTemplate success: template_title=%r total=%s list_items=%s",
        title,
        total,
        len(items),
    )
    result = {"total": total, "list": items}
    _attach_recommended_if_configured(result, defaults, for_recommend, title)
    return result
