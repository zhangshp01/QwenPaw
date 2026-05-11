# -*- coding: utf-8 -*-
"""Runtime context for tools (workspace dir, limits, agent tool-step index).

Also tracks the current asyncio :class:`~asyncio.Task` tool-execution order
(1-based within one ``QwenPawAgent.reply``), keyed by task so parallel tool
calls do not overwrite each other.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path
from weakref import WeakKeyDictionary

# Context variable to store the current agent's workspace directory
current_workspace_dir: ContextVar[Path | None] = ContextVar(
    "current_workspace_dir",
    default=None,
)


def get_current_workspace_dir() -> Path | None:
    """Get the current agent's workspace directory from context.

    Returns:
        Path to the current agent's workspace directory, or None if not set.
    """
    return current_workspace_dir.get()


def set_current_workspace_dir(workspace_dir: Path | None) -> None:
    """Set the current agent's workspace directory in context.

    Args:
        workspace_dir: Path to the agent's workspace directory.
    """
    current_workspace_dir.set(workspace_dir)


# Context variable to store the recent_max_bytes limit
current_recent_max_bytes: ContextVar[int | None] = ContextVar(
    "current_recent_max_bytes",
    default=None,
)


def get_current_recent_max_bytes() -> int | None:
    """Get the current agent's recent_max_bytes limit from context.

    Returns:
        Byte limit for recent tool output truncation, or None if not set.
    """
    return current_recent_max_bytes.get()


def set_current_recent_max_bytes(max_bytes: int | None) -> None:
    """Set the current agent's recent_max_bytes limit in context.

    Args:
        max_bytes: Byte limit for recent tool output truncation.
    """
    current_recent_max_bytes.set(max_bytes)


# Context variable to store the configured shell command timeout
current_shell_command_timeout: ContextVar[float | None] = ContextVar(
    "current_shell_command_timeout",
    default=None,
)


def get_current_shell_command_timeout() -> float | None:
    """Get the configured default timeout for execute_shell_command.

    Returns:
        Timeout in seconds, or None if not configured.
    """
    return current_shell_command_timeout.get()


def set_current_shell_command_timeout(timeout: float | None) -> None:
    """Set the configured default timeout for execute_shell_command.

    Args:
        timeout: Timeout in seconds.
    """
    current_shell_command_timeout.set(timeout)


# ---------------------------------------------------------------------------
# Tool execution step (1-based index within one agent ``reply`` cycle)
# ---------------------------------------------------------------------------

_agent_tool_step_by_task: WeakKeyDictionary[asyncio.Task, int] = (
    WeakKeyDictionary()
)


def set_agent_tool_step_for_running_task(step: int) -> None:
    """Associate *step* with ``asyncio.current_task()`` for the running tool."""
    task = asyncio.current_task()
    if task is not None:
        _agent_tool_step_by_task[task] = int(step)


def clear_agent_tool_step_for_running_task() -> None:
    """Remove the step mapping for ``asyncio.current_task()``."""
    task = asyncio.current_task()
    if task is not None:
        _agent_tool_step_by_task.pop(task, None)


def get_agent_tool_step_for_running_task() -> int | None:
    """Return the 1-based tool step for this task, or ``None`` if unset."""
    task = asyncio.current_task()
    if task is None:
        return None
    return _agent_tool_step_by_task.get(task)
