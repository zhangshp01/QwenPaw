# -*- coding: utf-8 -*-
"""govdoc-agent (智慧公文) AgentLoop API compatibility layer.

Maps govdoc-agent HTTP routes under ``/api/agentloop`` with the same path
layout and ``{code, message, data}`` response envelope. Conversation storage
is backed by the existing chat manager + console channel streaming (not the
govdoc SQLite schema).

Reference implementation:
``govdoc-agent/app/routes/*.py`` (prefixes ``/api/agentloop``).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from agentscope.memory import InMemoryMemory
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agentscope_runtime.engine.schemas.agent_schemas import Message

from ..agent_context import get_agent_for_request
from ..workspace_paths import (
    DEFAULT_GOVDOCS_SORT_ORDER,
    GOVDOCS_SORT_ASC,
    GOVDOCS_SORT_DESC,
    MAX_WORKSPACE_BINARY_WRITE_BYTES,
    annotate_govdocs_user,
    build_govdocs_list_response,
    delete_govdocs_files_batch,
    filter_govdocs_by_filename,
    govdocs_file_detail,
    list_govdocs_files,
    rename_govdocs_file,
    build_workspace_files_zip,
    mime_type_for_suffix,
    resolve_govdocs_file_path,
    resolve_workspace_download_files,
    resolve_workspace_upload_directory,
    resolve_workspace_write_path,
    save_upload_to_workspace_directory,
    sort_govdocs_by_modified_time,
    write_bytes_to_path,
)
from .console import _extract_session_and_payload
from .skills import _build_workspace_skill_specs
from ..runner.manager import ChatManager
from ..runner.session import SafeJSONSession
from ..runner.api import get_chat_manager, get_session, get_workspace
from ..runner.utils import agentscope_msg_to_message
from ..runner.models import ChatSpec, ChatUpdate
from ...providers.provider_manager import ProviderManager
from .agentloop_conversation_detail import build_conversation_detail
from .agentloop_workflow_sse import (
    AgentLoopWorkflowSseTransformer,
    workflow_message_start_sse,
)
from ..workspace.workspace import Workspace

# ---------------------------------------------------------------------------
# Envelope & request bodies (aligned with govdoc-agent ``app/schemas.py``)
# ---------------------------------------------------------------------------


class AgentLoopResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Any = None


class ConversationCreateRequest(BaseModel):
    title: str | None = None


class ConversationRenameRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


class ConversationRunRequest(BaseModel):
    content: str
    model: str | None = None
    skill: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    resume_from_waiting: bool = False
    selected_option: str | None = None
    prompt_menu_input: str | None = None


class SettingsProfileUpdateRequest(BaseModel):
    default_model: str | None = None
    preferred_skills: list[str] = Field(default_factory=list)
    recommendation_summary: str | None = None


class MemoryUpdateRequest(BaseModel):
    identity: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    identify_markdown: str | None = None
    memory_markdown: str | None = None


class DictionaryEntryCreateRequest(BaseModel):
    dict_type: str  # "whiteList" | "blackList"
    word: str
    notes: str | None = None


class WorkspaceFolderCreateRequest(BaseModel):
    name: str
    parent_id: str | None = None


class WorkspaceDocumentCreateRequest(BaseModel):
    name: str
    parent_id: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    source: str | None = "manual"


class WorkspaceDocumentUpdateRequest(BaseModel):
    title: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceNodeRenameRequest(BaseModel):
    name: str


class WorkspaceNodeMoveRequest(BaseModel):
    parent_id: str | None = None


class WorkspaceGovdocRenameRequest(BaseModel):
    """Rename a file in ``govdocs/`` (basename only)."""

    path: str = Field(..., description="Existing file path (relative or absolute)")
    newFilename: str = Field(  # noqa: N815
        ...,
        min_length=1,
        description="New file name including extension, e.g. new-title.docx",
    )


class WorkspaceGovdocBatchDeleteRequest(BaseModel):
    """Batch-delete files under ``govdocs/``."""

    paths: list[str] = Field(
        ...,
        min_length=1,
        description="File paths (relative under govdocs/ or absolute under workspace)",
    )


class WorkspaceDownloadRequest(BaseModel):
    """Download one or more files from the agent workspace."""

    path: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "File path(s) under workspace (relative or absolute under workspace)"
        ),
    )


# Root mount: included from ``routers`` under ``/api`` → ``/api/agentloop/...``
router = APIRouter(prefix="/agentloop", tags=["agentloop"])
conversations_router = APIRouter(prefix="/conversations")
workspace_router = APIRouter(prefix="/workspace")
settings_router = APIRouter(prefix="/settings")


def _not_implemented(feature: str) -> AgentLoopResponse:
    return AgentLoopResponse(
        code=501,
        message=(
            f"{feature} is not backed by this server; "
            "use native QwenPaw workspace/settings APIs."
        ),
        data=None,
    )


_AGENTLOOP_RUN_IDS_META_KEY = "agentloopRunIds"
_AGENTLOOP_LAST_RUN_META_KEY = "lastRunId"
_AGENTLOOP_RUN_HISTORY_LIMIT = 50


def _chat_meta_dict(chat: ChatSpec) -> dict[str, Any]:
    meta = chat.meta
    return meta if isinstance(meta, dict) else {}


def _last_run_id_from_chat(chat: ChatSpec) -> str | None:
    raw = _chat_meta_dict(chat).get(_AGENTLOOP_LAST_RUN_META_KEY)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _run_belongs_to_conversation(
    chat: ChatSpec,
    run_id: str,
    *,
    registry_conversation_id: str | None,
) -> bool:
    if registry_conversation_id == chat.id:
        return True
    meta = _chat_meta_dict(chat)
    if meta.get(_AGENTLOOP_LAST_RUN_META_KEY) == run_id:
        return True
    history = meta.get(_AGENTLOOP_RUN_IDS_META_KEY)
    return isinstance(history, list) and run_id in history


async def _register_agentloop_run(
    workspace: Workspace,
    mgr: ChatManager,
    chat: ChatSpec,
    run_id: str,
) -> None:
    await workspace.agentloop_run_registry.register(run_id, chat.id)
    meta = dict(_chat_meta_dict(chat))
    meta[_AGENTLOOP_LAST_RUN_META_KEY] = run_id
    history = list(meta.get(_AGENTLOOP_RUN_IDS_META_KEY) or [])
    if run_id not in history:
        history.append(run_id)
    meta[_AGENTLOOP_RUN_IDS_META_KEY] = history[-_AGENTLOOP_RUN_HISTORY_LIMIT:]
    merged = chat.model_copy(
        update={
            "meta": meta,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await mgr.create_chat(merged)


def _resolve_agentloop_user_id(request: Request) -> str:
    from ..auth import verify_token

    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if token:
        username = verify_token(token)
        if username:
            return username
    return "agentloop"


def _message_to_plain_text(message: Message) -> str:
    chunks: list[str] = []
    for block in message.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _runtime_message_to_govdoc(
    message: Message,
    *,
    seq: int,
    run_id: str,
) -> dict[str, Any]:
    meta = getattr(message, "metadata", None) or {}
    return {
        "id": getattr(message, "id", None) or f"msg-{seq}",
        "runId": run_id,
        "role": message.role or "assistant",
        "skillName": None,
        "content": _message_to_plain_text(message),
        "contentHtml": None,
        "model": None,
        "annotations": [],
        "meta": meta if isinstance(meta, dict) else {},
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }


_PLAN_CMD_PREFIX = "/plan "


def _strip_leading_manual_plan_commands(text: str) -> str:
    """Remove one or more leading ``/plan`` / ``/plan <desc>`` prefixes (any case).

    Used when ``skill`` is set so a literal ``/plan`` in ``content`` cannot open
    the plan gate; the model should go straight to tools for the given skill.
    """
    s = text
    pat = re.compile(r"^\s*/plan(?:\s+|$)", re.IGNORECASE)
    while True:
        m = pat.match(s)
        if not m:
            return s
        s = s[m.end() :].lstrip()


def _ensure_leading_plan_command(text: str) -> str:
    """Ensure runner sees ``/plan <description>`` (``AgentRunner`` gate).

    Uses the same predicate as ``query.strip().lower().startswith("/plan ")``.
    Applied after skill / continuation lines are merged so ``[skill:…]`` does
    not hide a leading ``/plan``.
    """
    if text.strip().lower().startswith(_PLAN_CMD_PREFIX):
        return text
    return f"{_PLAN_CMD_PREFIX}{text}"


def _build_run_input_dict(
    body: ConversationRunRequest,
    *,
    chat: ChatSpec,
) -> dict[str, Any]:
    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": body.content}]
    for att in body.attachments or []:
        if not isinstance(att, dict):
            continue
        url = att.get("url") or att.get("file_url") or att.get("fileUrl")
        if isinstance(url, str) and url.strip():
            content_blocks.append(
                {
                    "type": "file",
                    "file_url": url.strip(),
                    "filename": att.get("filename") or att.get("name") or "",
                },
            )
    text = body.content
    skill_key = (body.skill or "").strip()
    if skill_key:
        text = _strip_leading_manual_plan_commands(text)
    if body.resume_from_waiting and (
        body.selected_option or body.prompt_menu_input
    ):
        extra = body.prompt_menu_input or body.selected_option or ""
        text = f"{text}\n[continuation:{extra}]"
    if skill_key:
        text = f"[skill:{skill_key}]\n{text}"
    else:
        text = _ensure_leading_plan_command(text)
    content_blocks[0] = {"type": "text", "text": text}

    req: dict[str, Any] = {
        "channel": "console",
        "user_id": chat.user_id,
        "session_id": chat.session_id,
        "input": [
            {
                "role": "user",
                "type": "message",
                "content": content_blocks,
            },
        ],
    }
    if body.model:
        req["model"] = body.model
    return req


def _passthrough_console_sse_chunk(raw: str) -> str:
    """Re-emit Console SSE: one ``data:`` line per AgentScope JSON object (no outer envelope)."""
    out_parts: list[str] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block == "data: [DONE]" or (
            block.startswith("data:") and "[DONE]" in block
        ):
            out_parts.append("data: [DONE]\n\n")
            continue
        if not block.startswith("data:"):
            continue
        payload_str = block[5:].strip()
        try:
            inner: Any = json.loads(payload_str)
        except json.JSONDecodeError:
            inner = {"raw": payload_str}
        out_parts.append(
            "data: " + json.dumps(inner, ensure_ascii=False) + "\n\n",
        )
    return "".join(out_parts)


def _slim_agentloop_sse_payload(obj: Any) -> Any:
    """Drop nulls and empty noise fields from streamed AgentLoop JSON."""

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if v is None:
                continue
            if k in {"metadata", "usage"} and v == {}:
                continue
            slim_v = _slim_agentloop_sse_payload(v)
            if slim_v is None:
                continue
            if slim_v == {} and k in {"metadata", "usage"}:
                continue
            out[k] = slim_v
        return out
    if isinstance(obj, list):
        return [_slim_agentloop_sse_payload(x) for x in obj]
    return obj


class _AgentLoopSseToolStreamDeduper:
    """Collapse duplicate in-progress tool ``arguments`` / ``output`` fragments
    (same ``msg_id`` + ``call_id``, identical payload until ``completed``).
    """

    def __init__(self) -> None:
        self._last_args: dict[tuple[str, str], str] = {}
        self._last_output: dict[tuple[str, str], str] = {}

    def should_skip(self, payload: dict[str, Any]) -> bool:
        if payload.get("object") != "content" or payload.get("type") != "data":
            return False
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        msg_id = payload.get("msg_id")
        call_id = data.get("call_id")
        if not isinstance(msg_id, str) or not isinstance(call_id, str):
            return False
        key = (msg_id, call_id)
        status = payload.get("status")

        if "arguments" in data:
            if status == "completed":
                self._last_args.pop(key, None)
                return False
            raw = data.get("arguments", "")
            cur = raw if isinstance(raw, str) else json.dumps(
                raw,
                ensure_ascii=False,
            )
            prev = self._last_args.get(key)
            if prev == cur:
                return True
            self._last_args[key] = cur
            return False

        if "output" in data:
            if status == "completed":
                self._last_output.pop(key, None)
                return False
            raw = data.get("output", "")
            cur = raw if isinstance(raw, str) else json.dumps(
                raw,
                ensure_ascii=False,
            )
            prev = self._last_output.get(key)
            if prev == cur:
                return True
            self._last_output[key] = cur
            return False

        return False


def _compact_should_drop_payload(d: dict[str, Any]) -> bool:
    """Events that duplicate information the UI already gets elsewhere."""

    if d.get("object") == "response" and d.get("status") == "in_progress":
        return True
    if (
        d.get("object") == "content"
        and d.get("status") == "completed"
        and d.get("type") == "data"
    ):
        data = d.get("data")
        if isinstance(data, dict) and (
            "arguments" in data or "output" in data
        ):
            return True
    return False


def _reshape_compact_plugin_message(d: dict[str, Any]) -> dict[str, Any]:
    """Replace nested ``content[]`` on tool messages with a flat ``tool`` dict."""

    if (
        d.get("object") != "message"
        or d.get("status") != "completed"
        or d.get("type") not in {"plugin_call", "plugin_call_output"}
    ):
        return d
    raw_content = d.get("content")
    if not isinstance(raw_content, list) or not raw_content:
        return d
    first = raw_content[0]
    if not isinstance(first, dict):
        return d
    blob = first.get("data")
    if not isinstance(blob, dict):
        return d
    out = {k: v for k, v in d.items() if k != "content"}
    tool = {
        k: blob[k]
        for k in ("call_id", "name", "arguments", "output")
        if k in blob
    }
    if tool:
        out["tool"] = tool
    return out


class _AgentLoopSseTerminalDeduper:
    """Suppress redundant ``content``/``message`` *completed* events when a
    ``response`` *completed* follows (same final assistant text in three envelopes).
    """

    def __init__(self) -> None:
        self._buf: list[dict[str, Any]] = []

    @staticmethod
    def _is_text_content_done(d: Any) -> bool:
        return (
            isinstance(d, dict)
            and d.get("object") == "content"
            and d.get("status") == "completed"
            and d.get("type") == "text"
        )

    @staticmethod
    def _is_assistant_message_done(d: Any) -> bool:
        return (
            isinstance(d, dict)
            and d.get("object") == "message"
            and d.get("status") == "completed"
            and d.get("type") == "message"
            and d.get("role") == "assistant"
        )

    @staticmethod
    def _is_response_done(d: Any) -> bool:
        return (
            isinstance(d, dict)
            and d.get("object") == "response"
            and d.get("status") == "completed"
        )

    def feed(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self._is_response_done(payload):
            self._buf.clear()
            return [payload]
        if self._is_text_content_done(payload):
            self._buf = [payload]
            return []
        if self._is_assistant_message_done(payload):
            if (
                len(self._buf) == 1
                and self._is_text_content_done(self._buf[0])
            ):
                self._buf.append(payload)
                return []
            flushed = list(self._buf)
            self._buf.clear()
            return flushed + [payload]
        flushed = list(self._buf)
        self._buf.clear()
        return flushed + [payload]

    def flush(self) -> list[dict[str, Any]]:
        out = list(self._buf)
        self._buf.clear()
        return out


def _emit_agentloop_sse_dict(em: dict[str, Any], *, compact: bool) -> str:
    if compact:
        em = _reshape_compact_plugin_message(em)
        slimmed = _slim_agentloop_sse_payload(em)
        if not isinstance(slimmed, dict):
            slimmed = em
        if _AgentLoopSseTerminalDeduper._is_response_done(slimmed):
            slimmed = {k: v for k, v in slimmed.items() if k != "output"}
        payload: Any = slimmed
    else:
        payload = em
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _passthrough_agentloop_sse_chunk(
    raw: str,
    deduper: _AgentLoopSseTerminalDeduper,
    tool_stream_deduper: _AgentLoopSseToolStreamDeduper,
    *,
    compact: bool = True,
) -> str:
    """Like :func:`_passthrough_console_sse_chunk` but:

    - Drop duplicate terminal ``content``/``message`` completed lines before
      ``response`` completed.
    - Drop consecutive duplicate in-progress tool ``arguments`` / ``output``
      fragments.
    - In ``compact`` mode: drop ``response`` ``in_progress``, drop redundant
      tool ``content`` ``completed`` lines (keep ``message`` completed with a
      flat ``tool`` object), strip nulls, omit final ``response`` ``output``.
    """
    out_parts: list[str] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block == "data: [DONE]" or (
            block.startswith("data:") and "[DONE]" in block
        ):
            for em in deduper.flush():
                out_parts.append(_emit_agentloop_sse_dict(em, compact=compact))
            out_parts.append("data: [DONE]\n\n")
            continue
        if not block.startswith("data:"):
            continue
        payload_str = block[5:].strip()
        try:
            inner: Any = json.loads(payload_str)
        except json.JSONDecodeError:
            inner = {"raw": payload_str}
        if isinstance(inner, dict):
            if compact and _compact_should_drop_payload(inner):
                continue
            if tool_stream_deduper.should_skip(inner):
                continue
            for em in deduper.feed(inner):
                out_parts.append(_emit_agentloop_sse_dict(em, compact=compact))
        else:
            for em in deduper.flush():
                out_parts.append(_emit_agentloop_sse_dict(em, compact=compact))
            out_parts.append(
                "data: "
                + json.dumps(
                    _slim_agentloop_sse_payload(inner)
                    if compact
                    else inner,
                    ensure_ascii=False,
                )
                + "\n\n",
            )
    return "".join(out_parts)


# ---------------------------------------------------------------------------
# /api/agentloop/health, /me, /models, /skills
# ---------------------------------------------------------------------------


@router.get("/health", response_model=AgentLoopResponse)
async def agentloop_health() -> AgentLoopResponse:
    return AgentLoopResponse(data={"status": "ok"})


@router.get("/me", response_model=AgentLoopResponse)
async def agentloop_me(request: Request) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    workspace = await get_agent_for_request(request)
    manager = ProviderManager.get_instance()
    providers = await manager.list_provider_info()
    auth_models: list[dict[str, Any]] = []
    for p in providers:
        for m in list(p.models) + list(p.extra_models):
            auth_models.append(
                {
                    "providerId": p.id,
                    "modelId": m.id,
                    "id": f"{p.id}:{m.id}",
                    "name": m.name or m.id,
                },
            )
    skills = _build_workspace_skill_specs(workspace.workspace_dir)
    preferred = [s.name for s in skills if getattr(s, "enabled", False)]
    return AgentLoopResponse(
        data={
            "user_id": uid,
            "name": uid,
            "preferredSkills": preferred,
            "recommendationSummary": None,
            "auth_models": auth_models,
            "identifyMarkdown": "",
            "memoryMarkdown": "",
            "sessionSummaryMarkdown": "",
        },
    )


@router.get("/models", response_model=AgentLoopResponse)
async def agentloop_models(request: Request) -> AgentLoopResponse:
    _ = await get_agent_for_request(request)
    manager = ProviderManager.get_instance()
    providers = await manager.list_provider_info()
    auth_models: list[dict[str, Any]] = []
    for p in providers:
        for m in list(p.models) + list(p.extra_models):
            auth_models.append(
                {
                    "providerId": p.id,
                    "modelId": m.id,
                    "id": f"{p.id}:{m.id}",
                    "name": m.name or m.id,
                },
            )
    return AgentLoopResponse(data=auth_models)


@router.get("/skills", response_model=AgentLoopResponse)
async def agentloop_skills(request: Request) -> AgentLoopResponse:
    """Skill descriptors (govdoc ``SkillDescriptor``: key/title/summary/examples)."""
    workspace = await get_agent_for_request(request)
    specs = _build_workspace_skill_specs(workspace.workspace_dir)
    descriptors = [
        {
            "key": s.name,
            "title": s.name.replace("-", " ").title(),
            "summary": (s.description or "").strip(),
            "examples": [],
        }
        for s in specs
    ]
    return AgentLoopResponse(data=descriptors)


# ---------------------------------------------------------------------------
# /api/agentloop/conversations
# ---------------------------------------------------------------------------


@conversations_router.get("", response_model=AgentLoopResponse)
async def list_conversations(
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    chats = await mgr.list_chats(user_id=uid, channel="console")
    chats.sort(key=lambda c: (c.pinned, c.updated_at.timestamp()), reverse=True)
    data = []
    for item in chats:
        delta = datetime.now(timezone.utc) - item.updated_at
        meta = (
            "刚刚更新"
            if delta.total_seconds() < 120
            else item.updated_at.strftime("%m月%d日 %H:%M")
        )
        entry: dict[str, Any] = {
            "id": item.id,
            "title": item.name,
            "pinned": item.pinned,
            "updatedAt": item.updated_at.isoformat(),
            "meta": meta,
        }
        last_run_id = _last_run_id_from_chat(item)
        if last_run_id is not None:
            entry["lastRunId"] = last_run_id
        data.append(entry)
    return AgentLoopResponse(data=data)


@conversations_router.post("", response_model=AgentLoopResponse)
async def create_conversation(
    request: Request,
    payload: ConversationCreateRequest,
    mgr: ChatManager = Depends(get_chat_manager),
) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    chat_id = str(uuid.uuid4())
    session_id = f"agentloop:{chat_id}"
    spec = ChatSpec(
        id=chat_id,
        name=payload.title or "新对话",
        session_id=session_id,
        user_id=uid,
        channel="console",
    )
    created = await mgr.create_chat(spec)
    return AgentLoopResponse(data={"id": created.id, "title": created.name})


async def _get_owned_chat_or_404(
    mgr: ChatManager,
    conversation_id: str,
    user_id: str,
) -> ChatSpec:
    chat = await mgr.get_chat(conversation_id)
    if chat is None or chat.user_id != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return chat


def _resolve_conversation_model(workspace: Any, chat: ChatSpec) -> str | None:
    """Best-effort model label for conversation detail (optional)."""
    meta = chat.meta if isinstance(chat.meta, dict) else {}
    for key in ("lastModel", "model", "defaultModel"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    cfg = getattr(workspace, "config", None)
    active = getattr(cfg, "active_model", None) if cfg is not None else None
    if active is not None:
        model_id = getattr(active, "model", None)
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip()
    return None


@conversations_router.get("/{conversation_id}", response_model=AgentLoopResponse)
async def get_conversation(
    request: Request,
    conversation_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    chat = await _get_owned_chat_or_404(mgr, conversation_id, uid)
    state = await session.get_session_state_dict(chat.session_id, chat.user_id)
    messages_out: list[Message] = []
    if state:
        memory_state = state.get("agent", {}).get("memory", {})
        memory = InMemoryMemory()
        memory.load_state_dict(memory_state, strict=False)
        memories = await memory.get_memory(prepend_summary=False)
        messages_out = agentscope_msg_to_message(memories)
    model = _resolve_conversation_model(workspace, chat)
    detail = build_conversation_detail(chat, messages_out, model=model)
    return AgentLoopResponse(data=detail)


@conversations_router.put("/{conversation_id}", response_model=AgentLoopResponse)
async def update_conversation(
    request: Request,
    conversation_id: str,
    payload: ConversationRenameRequest,
    mgr: ChatManager = Depends(get_chat_manager),
) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    chat = await _get_owned_chat_or_404(mgr, conversation_id, uid)
    patch_kwargs: dict[str, Any] = {}
    if payload.title is not None:
        patch_kwargs["name"] = payload.title
    if payload.pinned is not None:
        patch_kwargs["pinned"] = payload.pinned
    if not patch_kwargs:
        return AgentLoopResponse(
            data={
                "id": chat.id,
                "title": chat.name,
                "pinned": chat.pinned,
            },
        )
    updated = await mgr.patch_chat(
        conversation_id,
        ChatUpdate(**patch_kwargs),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return AgentLoopResponse(
        data={
            "id": updated.id,
            "title": updated.name,
            "pinned": updated.pinned,
        },
    )


@conversations_router.delete("/{conversation_id}", response_model=AgentLoopResponse)
async def delete_conversation(
    request: Request,
    conversation_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
) -> AgentLoopResponse:
    uid = _resolve_agentloop_user_id(request)
    await _get_owned_chat_or_404(mgr, conversation_id, uid)
    deleted = await mgr.delete_chats(chat_ids=[conversation_id])
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return AgentLoopResponse(data=True)


@conversations_router.post("/{conversation_id}/run", response_model=AgentLoopResponse)
async def run_conversation(
    request: Request,
    conversation_id: str,
    body: ConversationRunRequest,
) -> AgentLoopResponse:
    workspace = await get_agent_for_request(request)
    mgr = workspace.chat_manager
    uid = _resolve_agentloop_user_id(request)
    chat = await _get_owned_chat_or_404(mgr, conversation_id, uid)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(status_code=503, detail="Channel Console not found")

    input_dict = _build_run_input_dict(body, chat=chat)
    try:
        native_payload = _extract_session_and_payload(input_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = str(uuid.uuid4())
    tracker = workspace.task_tracker
    queue, is_new = await tracker.attach_or_start(
        run_id,
        native_payload,
        console_channel.stream_one,
    )
    if is_new:
        await tracker.detach_subscriber(run_id, queue)

    await _register_agentloop_run(workspace, mgr, chat, run_id)

    stream_url = (
        f"/api/agentloop/conversations/{chat.id}/events?run_id={run_id}"
    )
    return AgentLoopResponse(
        data={
            "conversationId": chat.id,
            "conversationTitle": chat.name,
            "runId": run_id,
            "taskId": run_id,
            "streamUrl": stream_url,
            "assistantMessage": None,
            "artifacts": [],
            "pendingPromptMenu": None,
        },
    )


@conversations_router.get("/{conversation_id}/events")
async def stream_conversation_events(
    request: Request,
    conversation_id: str,
    run_id: str | None = Query(default=None),
    event_format: str = Query(
        default="workflow",
        description='SSE payload shape. Use "workflow" for message_start / '
        "content_block_* / message_stop (Agent 工作流协议). Use \"legacy\" for "
        "passthrough AgentScope-style JSON (same as historical AgentLoop).",
    ),
    compact: bool = Query(
        default=True,
        description="Only for event_format=legacy: drop response in_progress, merge "
        "duplicate tool completed content lines, flatten plugin messages to a "
        "top-level tool field, dedupe tool stream chunks, strip nulls, omit "
        "final response output. Use compact=false for the raw verbose stream.",
    ),
):
    workspace = await get_agent_for_request(request)
    mgr = workspace.chat_manager
    uid = _resolve_agentloop_user_id(request)
    conversation_id = conversation_id.strip()
    rid = run_id.strip() if isinstance(run_id, str) and run_id.strip() else None
    chat = await _get_owned_chat_or_404(mgr, conversation_id, uid)
    if rid is None:
        rid = _last_run_id_from_chat(chat)
    if rid is None:
        raise HTTPException(status_code=400, detail="run_id is required")

    registry = workspace.agentloop_run_registry
    owner = await registry.resolve_conversation(rid)
    if not _run_belongs_to_conversation(
        chat,
        rid,
        registry_conversation_id=owner,
    ):
        raise HTTPException(
            status_code=400,
            detail="run_id does not belong to this conversation",
        )

    tracker = workspace.task_tracker
    queue = await tracker.attach(rid)
    if queue is None:
        raise HTTPException(status_code=404, detail="暂无运行记录")

    use_legacy = event_format.strip().lower() == "legacy"

    async def event_stream() -> AsyncGenerator[str, None]:
        stream_it = tracker.stream_from_queue(queue, rid)
        if use_legacy:
            deduper = _AgentLoopSseTerminalDeduper()
            tool_stream_deduper = _AgentLoopSseToolStreamDeduper()
            try:
                async for chunk in stream_it:
                    yield _passthrough_agentloop_sse_chunk(
                        chunk,
                        deduper,
                        tool_stream_deduper,
                        compact=compact,
                    )
            finally:
                tail = deduper.flush()
                if tail:
                    yield "".join(
                        _emit_agentloop_sse_dict(em, compact=compact)
                        for em in tail
                    )
                await stream_it.aclose()
            return

        deduper = _AgentLoopSseTerminalDeduper()
        tool_stream_deduper = _AgentLoopSseToolStreamDeduper()
        wf = AgentLoopWorkflowSseTransformer()
        yield workflow_message_start_sse()
        try:
            async for chunk in stream_it:
                passthrough = _passthrough_agentloop_sse_chunk(
                    chunk,
                    deduper,
                    tool_stream_deduper,
                    compact=False,
                )
                out = wf.consume_passthrough_batch(passthrough)
                if out:
                    yield out
            tail = deduper.flush()
            if tail:
                passthrough = "".join(
                    _emit_agentloop_sse_dict(em, compact=False) for em in tail
                )
                out = wf.consume_passthrough_batch(passthrough)
                if out:
                    yield out
        finally:
            yield wf.finish()
            await stream_it.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
    )


router.include_router(conversations_router)

# ---------------------------------------------------------------------------
# /api/agentloop/workspace
# ---------------------------------------------------------------------------


@workspace_router.put("/files_binary", response_model=AgentLoopResponse)
async def workspace_write_files_binary(
    request: Request,
    path: str = Query(
        ...,
        min_length=1,
        description=(
            "Target .docx path (relative to workspace or absolute under workspace)"
        ),
    ),
) -> AgentLoopResponse:
    """Write request body bytes to *path* in the active agent workspace (``.docx``)."""
    workspace = await get_agent_for_request(request)
    target = resolve_workspace_write_path(path, workspace.workspace_dir)

    data = await request.body()
    if len(data) > MAX_WORKSPACE_BINARY_WRITE_BYTES:
        limit_mb = MAX_WORKSPACE_BINARY_WRITE_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"Payload too large (max {limit_mb} MB)",
        )

    try:
        await asyncio.to_thread(write_bytes_to_path, target, data)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write file: {exc}",
        ) from exc

    return AgentLoopResponse(
        data={
            "written": True,
            "path": str(target),
            "size": len(data),
        },
    )


@workspace_router.get("/get_list", response_model=AgentLoopResponse)
async def workspace_govdocs_get_list(
    request: Request,
    sortOrder: str = Query(  # noqa: N803  # API uses camelCase
        DEFAULT_GOVDOCS_SORT_ORDER,
        description=f"Sort by modified_time: {GOVDOCS_SORT_ASC} or {GOVDOCS_SORT_DESC}",
    ),
    filename: str | None = Query(
        None,
        description=(
            "Fuzzy filter on filename or relative path under govdocs "
            "(case-insensitive substring)"
        ),
    ),
) -> AgentLoopResponse:
    """List files and folders under ``govdocs/`` (recursive), full list."""
    workspace = await get_agent_for_request(request)
    all_files = await asyncio.to_thread(list_govdocs_files, workspace.workspace_dir)
    all_files = filter_govdocs_by_filename(all_files, filename)
    all_files = sort_govdocs_by_modified_time(all_files, sortOrder)
    all_files = annotate_govdocs_user(all_files, workspace.agent_id)
    data = build_govdocs_list_response(all_files)
    return AgentLoopResponse(data=data)


@workspace_router.get("/file_detail", response_model=AgentLoopResponse)
async def workspace_govdocs_file_detail(
    request: Request,
    path: str = Query(..., min_length=1, description="File path under govdocs/"),
) -> AgentLoopResponse:
    """Return metadata for one file under ``govdocs/``."""
    workspace = await get_agent_for_request(request)
    target = resolve_govdocs_file_path(path, workspace.workspace_dir)
    detail = await asyncio.to_thread(govdocs_file_detail, target)
    return AgentLoopResponse(data=detail)


@workspace_router.put("/file_rename", response_model=AgentLoopResponse)
async def workspace_govdocs_file_rename(
    request: Request,
    body: WorkspaceGovdocRenameRequest,
) -> AgentLoopResponse:
    """Rename a file in ``govdocs/`` (``newFilename`` is basename only)."""
    workspace = await get_agent_for_request(request)
    target = resolve_govdocs_file_path(body.path, workspace.workspace_dir)

    def _do_rename() -> dict[str, Any]:
        new_path = rename_govdocs_file(target, body.newFilename)
        return govdocs_file_detail(new_path)

    data = await asyncio.to_thread(_do_rename)
    return AgentLoopResponse(data=data)


@workspace_router.delete("/file_delete", response_model=AgentLoopResponse)
async def workspace_govdocs_file_delete(
    request: Request,
    body: WorkspaceGovdocBatchDeleteRequest,
) -> AgentLoopResponse:
    """Batch-delete files under ``govdocs/`` (partial success allowed)."""
    workspace = await get_agent_for_request(request)
    data = await asyncio.to_thread(
        delete_govdocs_files_batch,
        workspace.workspace_dir,
        body.paths,
    )
    return AgentLoopResponse(data=data)


@workspace_router.get("/tree", response_model=AgentLoopResponse)
async def workspace_tree() -> AgentLoopResponse:
    return _not_implemented("Workspace tree")


@workspace_router.post("/folders", response_model=AgentLoopResponse)
async def workspace_create_folder(
    payload: WorkspaceFolderCreateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace folder create")


@workspace_router.post("/documents", response_model=AgentLoopResponse)
async def workspace_create_document(
    payload: WorkspaceDocumentCreateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace document create")


@workspace_router.post(
    "/nodes/{node_id}/send-to-conversation",
    response_model=AgentLoopResponse,
)
async def workspace_send_to_conversation(node_id: str) -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("send-to-conversation")


@workspace_router.post(
    "/download",
    response_model=None,
    summary="Download one or more workspace files",
    responses={
        200: {
            "content": {
                "application/octet-stream": {},
                "application/zip": {},
            },
        },
    },
)
@workspace_router.post(
    "/download/",
    include_in_schema=False,
    response_model=None,
)
async def workspace_download_files(
    request: Request,
    body: WorkspaceDownloadRequest,
):
    """Download workspace file(s). Single file streams directly; multiple become a zip."""
    workspace = await get_agent_for_request(request)
    files = await asyncio.to_thread(
        resolve_workspace_download_files,
        body.path,
        workspace.workspace_dir,
    )

    if len(files) == 1:
        target = files[0]
        return FileResponse(
            target,
            filename=target.name,
            media_type=mime_type_for_suffix(target.suffix),
        )

    buf = await asyncio.to_thread(
        build_workspace_files_zip,
        workspace.workspace_dir,
        files,
    )
    zip_name = f"workspace_download_{workspace.agent_id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
        },
    )


@workspace_router.post("/uploads", response_model=AgentLoopResponse)
async def workspace_upload(
    request: Request,
    directory: str = Form(
        ...,
        description=(
            "Target directory under agent workspace "
            "(e.g. govdocs or an absolute path under the workspace)"
        ),
    ),
    file: UploadFile = File(..., description="File to upload"),
) -> AgentLoopResponse:
    """Upload a file into *directory* under the active agent workspace."""
    workspace = await get_agent_for_request(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="file must have a filename")

    data = await file.read()
    if len(data) > MAX_WORKSPACE_BINARY_WRITE_BYTES:
        limit_mb = MAX_WORKSPACE_BINARY_WRITE_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"File too large (max {limit_mb} MB)",
        )

    upload_filename = file.filename

    def _do_upload() -> dict[str, Any]:
        target_dir = resolve_workspace_upload_directory(
            directory,
            workspace.workspace_dir,
        )
        entry = save_upload_to_workspace_directory(
            target_dir,
            upload_filename,
            data,
        )
        return annotate_govdocs_user([entry], workspace.agent_id)[0]

    result = await asyncio.to_thread(_do_upload)
    return AgentLoopResponse(data=result)


@workspace_router.get("/documents/{node_id}", response_model=AgentLoopResponse)
async def workspace_get_document(node_id: str) -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("Workspace document get")


@workspace_router.put("/documents/{node_id}", response_model=AgentLoopResponse)
async def workspace_save_document(
    node_id: str,  # noqa: ARG001
    payload: WorkspaceDocumentUpdateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace document save")


@workspace_router.put("/nodes/{node_id}", response_model=AgentLoopResponse)
async def workspace_rename_node(
    node_id: str,  # noqa: ARG001
    payload: WorkspaceNodeRenameRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace node rename")


@workspace_router.put("/nodes/{node_id}/move", response_model=AgentLoopResponse)
async def workspace_move_node(
    node_id: str,  # noqa: ARG001
    payload: WorkspaceNodeMoveRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace node move")


@workspace_router.delete("/nodes/{node_id}", response_model=AgentLoopResponse)
async def workspace_delete_node(node_id: str) -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("Workspace node delete")


router.include_router(workspace_router)

# ---------------------------------------------------------------------------
# /api/agentloop/settings (stubs)
# ---------------------------------------------------------------------------


@settings_router.get("/profile", response_model=AgentLoopResponse)
async def settings_get_profile(request: Request) -> AgentLoopResponse:
    workspace = await get_agent_for_request(request)
    manager = ProviderManager.get_instance()
    providers = await manager.list_provider_info()
    auth_models: list[dict[str, Any]] = []
    for p in providers:
        for m in list(p.models) + list(p.extra_models):
            auth_models.append(
                {
                    "providerId": p.id,
                    "modelId": m.id,
                    "id": f"{p.id}:{m.id}",
                    "name": m.name or m.id,
                },
            )
    skills = _build_workspace_skill_specs(workspace.workspace_dir)
    preferred = [s.name for s in skills if getattr(s, "enabled", False)]
    return AgentLoopResponse(
        data={
            "defaultModel": None,
            "preferredSkills": preferred,
            "recommendationSummary": None,
            "authModels": auth_models,
            "identifyMarkdown": "",
            "memoryMarkdown": "",
            "sessionSummaryMarkdown": "",
        },
    )


@settings_router.put("/profile", response_model=AgentLoopResponse)
async def settings_put_profile(
    payload: SettingsProfileUpdateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Settings profile update")


@settings_router.get("/memory", response_model=AgentLoopResponse)
async def settings_get_memory() -> AgentLoopResponse:
    return AgentLoopResponse(
        data={
            "identity": {},
            "memory": {},
            "identifyMarkdown": "",
            "memoryMarkdown": "",
            "sessionSummaryMarkdown": "",
        },
    )


@settings_router.put("/memory", response_model=AgentLoopResponse)
async def settings_put_memory(
    payload: MemoryUpdateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Settings memory update")


@settings_router.delete("/memory", response_model=AgentLoopResponse)
async def settings_delete_memory() -> AgentLoopResponse:
    return _not_implemented("Settings memory clear")


@settings_router.get("/dictionaries", response_model=AgentLoopResponse)
async def settings_get_dictionaries() -> AgentLoopResponse:
    return AgentLoopResponse(data={"whiteList": [], "blackList": []})


@settings_router.put("/dictionaries", response_model=AgentLoopResponse)
async def settings_put_dictionary(
    payload: DictionaryEntryCreateRequest,  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Dictionary create")


@settings_router.delete(
    "/dictionaries/{entry_id}",
    response_model=AgentLoopResponse,
)
async def settings_delete_dictionary(entry_id: str) -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("Dictionary delete")


@settings_router.get("/templates", response_model=AgentLoopResponse)
async def settings_get_templates() -> AgentLoopResponse:
    return AgentLoopResponse(data=[])


@settings_router.post("/templates", response_model=AgentLoopResponse)
async def settings_post_template(
    title: str | None = Form(None),  # noqa: ARG001
    document_type: str | None = Form(None),  # noqa: ARG001
    file: UploadFile | None = File(None),  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Template create")


@settings_router.delete(
    "/templates/{template_id}",
    response_model=AgentLoopResponse,
)
async def settings_delete_template(template_id: str) -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("Template delete")


router.include_router(settings_router)
