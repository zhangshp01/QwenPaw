# -*- coding: utf-8 -*-
"""Workflow SSE helpers for streaming ``gov_document_writer`` document text.

``doc_reviewer`` does **not** stream document deltas (single completion only).
"""

from __future__ import annotations

import json
import re
from typing import Any

_DOCUMENT_STREAM_TOOL_NAMES = frozenset(
    {
        "gov_document_writer",
        "gov-document-writer",
    },
)

_ARGUMENT_BODY_KEYS: tuple[str, ...] = (
    "content",
    "document",
    "text",
    "body",
    "markdown",
    "data",
)

_PARTIAL_JSON_STRING_RE = re.compile(
    r'"(?P<key>content|document|text|body|markdown)"\s*:\s*"(?P<val>(?:\\.|[^"\\])*)',
    re.DOTALL,
)


def document_stream_tool_names() -> frozenset[str]:
    return _DOCUMENT_STREAM_TOOL_NAMES


def _decode_json_string_fragment(raw: str) -> str:
    if not raw:
        return ""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def extract_document_from_partial_json(raw: str) -> str:
    """Best-effort body string from incomplete tool JSON or partial arguments."""
    text = (raw or "").strip()
    if not text:
        return ""

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        brace = text.find("{")
        if brace >= 0:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text[brace:])
            except json.JSONDecodeError:
                parsed = None

    if isinstance(parsed, dict):
        doc = parsed.get("document")
        if isinstance(doc, str) and doc:
            return doc
        for key in _ARGUMENT_BODY_KEYS:
            if key == "document":
                continue
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        nr = parsed.get("normalizedResult")
        if isinstance(nr, dict):
            inner = nr.get("document")
            if isinstance(inner, str) and inner:
                return inner

    for match in _PARTIAL_JSON_STRING_RE.finditer(text):
        val = _decode_json_string_fragment(match.group("val"))
        if val:
            return val
    return ""


def document_from_tool_root(root: dict[str, Any] | None) -> str:
    if not isinstance(root, dict):
        return ""
    nr = root.get("normalizedResult")
    if isinstance(nr, dict):
        doc = nr.get("document")
        if isinstance(doc, str) and doc:
            return doc
    doc = root.get("document")
    if isinstance(doc, str) and doc:
        return doc
    return ""


def document_from_tool_arguments(arguments: Any) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, dict):
        for key in _ARGUMENT_BODY_KEYS:
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    text = arguments if isinstance(arguments, str) else json.dumps(
        arguments,
        ensure_ascii=False,
    )
    text = text.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        for key in _ARGUMENT_BODY_KEYS:
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return extract_document_from_partial_json(text)
