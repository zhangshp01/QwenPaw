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

import json
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agentscope_runtime.engine.schemas.agent_schemas import Message

from ..agent_context import get_agent_for_request
from .console import _extract_session_and_payload
from .skills import _build_workspace_skill_specs
from ..runner.manager import ChatManager
from ..runner.session import SafeJSONSession
from ..runner.api import get_chat_manager, get_session, get_workspace
from ..runner.utils import agentscope_msg_to_message
from ..runner.models import ChatSpec, ChatUpdate
from ...providers.provider_manager import ProviderManager

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
    if body.skill:
        text = f"[skill:{body.skill}]\n{text}"
    if body.resume_from_waiting and (
        body.selected_option or body.prompt_menu_input
    ):
        extra = body.prompt_menu_input or body.selected_option or ""
        text = f"{text}\n[continuation:{extra}]"
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
        data.append(
            {
                "id": item.id,
                "title": item.name,
                "pinned": item.pinned,
                "updatedAt": item.updated_at.isoformat(),
                "lastRunId": item.id,
                "meta": meta,
            },
        )
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
    status = await workspace.task_tracker.get_status(chat.id)
    messages_out: list[dict[str, Any]] = []
    if state:
        memory_state = state.get("agent", {}).get("memory", {})
        memory = InMemoryMemory()
        memory.load_state_dict(memory_state, strict=False)
        memories = await memory.get_memory(prepend_summary=False)
        runtime_msgs = agentscope_msg_to_message(memories)
        for i, m in enumerate(runtime_msgs):
            messages_out.append(
                _runtime_message_to_govdoc(m, seq=i, run_id=chat.id),
            )
    return AgentLoopResponse(
        data={
            "id": chat.id,
            "title": chat.name,
            "pinned": chat.pinned,
            "runningContext": {},
            "pendingPromptMenu": None,
            "taskTree": [],
            "messageActions": {
                "canCopy": True,
                "canLike": True,
                "canDislike": True,
                "canRegenerate": True,
            },
            "stepOutcomes": [],
            "messages": messages_out,
            "artifacts": [],
            "compressionSnapshots": [],
            "status": status,
        },
    )


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

    tracker = workspace.task_tracker
    queue, is_new = await tracker.attach_or_start(
        chat.id,
        native_payload,
        console_channel.stream_one,
    )
    if is_new:
        await tracker.detach_subscriber(chat.id, queue)

    stream_url = (
        f"/api/agentloop/conversations/{chat.id}/events?run_id={chat.id}"
    )
    return AgentLoopResponse(
        data={
            "conversationId": chat.id,
            "conversationTitle": chat.name,
            "runId": chat.id,
            "taskId": chat.id,
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
):
    workspace = await get_agent_for_request(request)
    mgr = workspace.chat_manager
    uid = _resolve_agentloop_user_id(request)
    chat = await _get_owned_chat_or_404(mgr, conversation_id, uid)
    key = run_id or chat.id
    if key != chat.id:
        raise HTTPException(status_code=400, detail="run_id mismatch for this server")

    tracker = workspace.task_tracker
    queue = await tracker.attach(chat.id)
    if queue is None:
        raise HTTPException(status_code=404, detail="暂无运行记录")

    async def event_stream() -> AsyncGenerator[str, None]:
        stream_it = tracker.stream_from_queue(queue, chat.id)
        try:
            async for chunk in stream_it:
                yield _passthrough_console_sse_chunk(chunk)
        finally:
            await stream_it.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
    )


router.include_router(conversations_router)

# ---------------------------------------------------------------------------
# /api/agentloop/workspace (stubs — no govdoc SQLite workspace in QwenPaw)
# ---------------------------------------------------------------------------


@workspace_router.get("", response_model=AgentLoopResponse)
async def workspace_list(view: str = "recent") -> AgentLoopResponse:  # noqa: ARG001
    return _not_implemented("Workspace list")


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


@workspace_router.get("/download/{node_id}")
async def workspace_download(node_id: str) -> None:  # noqa: ARG001
    raise HTTPException(status_code=501, detail="Workspace download not implemented")


@workspace_router.post("/uploads", response_model=AgentLoopResponse)
async def workspace_upload(
    file: UploadFile | None = File(None),  # noqa: ARG001
) -> AgentLoopResponse:
    return _not_implemented("Workspace upload")


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
