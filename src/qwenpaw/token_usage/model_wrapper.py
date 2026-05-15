# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from pydantic import BaseModel

from ..constant import LLM_CHAT_LOG, LLM_CHAT_LOG_MAX_CHARS
from .buffer import _UsageEvent
from .manager import get_token_usage_manager

_LOGGER = logging.getLogger("qwenpaw.llm.chat")


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    _usage_by_session: dict[str, dict[str, Any]] = {}

    def __init__(self, provider_id: str, model: ChatModelBase) -> None:
        super().__init__(
            model_name=getattr(model, "model_name", "unknown"),
            stream=getattr(model, "stream", True),
        )
        self._model = model
        self._provider_id = provider_id

    def _record_usage(self, usage: ChatUsage | None) -> None:
        """Enqueue a usage event synchronously — never blocks the caller."""
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        if pt <= 0 and ct <= 0:
            return

        event = _UsageEvent(
            provider_id=self._provider_id,
            model_name=self.model_name,
            prompt_tokens=pt,
            completion_tokens=ct,
            date_str=date.today().isoformat(),
            now_iso=datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds",
            ),
        )
        # Fire-and-forget: synchronous put_nowait, ~100 ns, no await needed.
        get_token_usage_manager().enqueue(event)

        usage_data = {
            "provider_id": self._provider_id,
            "model_name": self.model_name,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        }
        self._store_usage(usage_data)

    @classmethod
    def pop_usage_for_session(cls, session_id: str) -> dict[str, Any] | None:
        return cls._usage_by_session.pop(session_id, None)

    def _store_usage(self, usage: dict[str, Any] | None) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        if session_id and usage:
            TokenRecordingModelWrapper._usage_by_session[session_id] = usage

    def _llm_log_should_emit(self) -> tuple[bool, int]:
        """(emit, levelno): emit detailed chat payload logs."""
        if LLM_CHAT_LOG:
            return True, logging.INFO
        if _LOGGER.isEnabledFor(logging.DEBUG):
            return True, logging.DEBUG
        return False, logging.DEBUG

    @staticmethod
    def _cap_str(s: str, max_chars: int) -> str:
        if len(s) <= max_chars:
            return s
        return (
            f"{s[:max_chars]}... "
            f"[truncated total_chars={len(s)} "
            f"raise QWENPAW_LLM_CHAT_LOG_MAX_CHARS to see more]"
        )

    def _redact_large_strings(self, obj: Any, *, max_str: int, depth: int = 0) -> Any:
        """Shorten huge strings (e.g. embedded base64) for readable logs."""
        if depth > 24:
            return "<max-depth>"
        if isinstance(obj, str):
            stripped = obj.lstrip()
            if stripped.startswith("data:") or len(obj) > max_str:
                return f"<omitted-string len={len(obj)}>"
            return obj
        if isinstance(obj, dict):
            return {
                str(k): self._redact_large_strings(
                    v,
                    max_str=max_str,
                    depth=depth + 1,
                )
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [
                self._redact_large_strings(
                    x,
                    max_str=max_str,
                    depth=depth + 1,
                )
                for x in obj
            ]
        if isinstance(obj, tuple):
            return tuple(
                self._redact_large_strings(x, max_str=max_str, depth=depth + 1)
                for x in obj
            )
        return obj

    def _serialize_for_llm_log(self, obj: Any) -> str:
        cap = max(2000, LLM_CHAT_LOG_MAX_CHARS)
        try:
            redacted = self._redact_large_strings(obj, max_str=8000)
            text = json.dumps(
                redacted,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except (TypeError, ValueError):
            text = repr(obj)
        return self._cap_str(text, cap)

    def _emit_llm_request_log(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: Any,
        stream: bool,
        levelno: int,
    ) -> None:
        payload = (
            "LLM REQUEST "
            f"provider={self._provider_id} model={self.model_name} "
            f"stream={stream} tool_choice={tool_choice!r} "
            f"n_messages={len(messages)} "
            f"n_tools={(len(tools) if tools else 0)}\n"
            f"messages_json=\n{self._serialize_for_llm_log(messages)}\n"
            f"tools_json=\n{self._serialize_for_llm_log(tools if tools else [])}"
        )
        _LOGGER.log(levelno, payload)

    def _emit_llm_response_log(self, *, body: str, levelno: int) -> None:
        _LOGGER.log(levelno, "LLM RESPONSE\n%s", body)

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # Fix: Omit tool_choice="auto" for vLLM compatibility
        # vLLM without --enable-auto-tool-choice will reject requests when
        # tool_choice="auto" is present, even if tools are provided.
        # By omitting tool_choice when it's "auto", we bypass the check
        # while keeping tools available for correct tool calling behavior.
        if tool_choice == "auto":
            tool_choice = None

        want_log, lvl = self._llm_log_should_emit()
        streams = bool(getattr(self, "stream", True))
        eff_stream = streams and kwargs.get("stream") is not False

        if want_log:
            self._emit_llm_request_log(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                stream=eff_stream,
                levelno=lvl,
            )

        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result, log_level=lvl if want_log else None)

        if want_log:
            try:
                rdict = self._redact_large_strings(asdict(result), max_str=8000)
            except (TypeError, ValueError):
                rdict = {"repr": repr(result)[: LLM_CHAT_LOG_MAX_CHARS]}
            self._emit_llm_response_log(
                body=f"payload_json=\n{self._serialize_for_llm_log(rdict)}",
                levelno=lvl,
            )
        self._record_usage(getattr(result, "usage", None))
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        *,
        log_level: int | None,
    ) -> AsyncGenerator[ChatResponse, None]:
        last_usage: ChatUsage | None = None
        chunk_rows: list[dict[str, Any]] = []
        max_saved = 64
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                last_usage = chunk.usage
            if log_level is not None:
                try:
                    row = self._redact_large_strings(
                        asdict(chunk),
                        max_str=6000,
                    )
                except (TypeError, ValueError):
                    row = {"error": repr(chunk)[:1200]}
                chunk_rows.append(row)
                if len(chunk_rows) > max_saved:
                    chunk_rows.pop(0)
            yield chunk
        self._record_usage(last_usage)
        if log_level is not None:
            summary: dict[str, Any] = {
                "streaming": True,
                "chunks_kept": len(chunk_rows),
                "chunks_note": (
                    "at most 64 most recent chunks (base64/elided); "
                    "full stream only via non-stream path"
                ),
                "chunks": chunk_rows,
            }
            if last_usage is not None:
                try:
                    summary["final_usage"] = asdict(last_usage)
                except (TypeError, ValueError):
                    summary["final_usage"] = repr(last_usage)
            self._emit_llm_response_log(
                body=f"payload_json=\n{self._serialize_for_llm_log(summary)}",
                levelno=log_level,
            )

