"""Operator-only work-mode setting, separate from agent-accessible chat settings."""

import json
import re
from typing import Any

from ..db import system_settings as db_settings

KEY = "tasks.conversations.enabled"
MANAGER_ANSWERS_KEY = "tasks.conversations.manager_answers"
MANAGER_MCP_KEY = "tasks.conversations.manager_mcp"
MAX_RESIDENT_KEY = "tasks.managers.max_resident"
MCP_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_MANAGER_MCP = 20
DEFAULT_MAX_RESIDENT = 6
MANAGER_OFF_TEXT = ("the task Manager does not answer: tasks.conversations.manager_answers is off. "
                    "Turn it on with task_conversations.update (manager_answers: true).")
_enabled = False
_manager_answers = False
_max_resident = DEFAULT_MAX_RESIDENT
_manager_mcp: tuple[str, ...] = ()
_UNSET = object()


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """Apply a value in memory; tests may set it without persisting a setting."""
    global _enabled
    _enabled = value


def manager_answers() -> bool:
    """Whether the task Manager answers task messages. Independent of ``enabled``; only the string ``true`` is on."""
    return _manager_answers


def manager_active() -> bool:
    """Work mode and the task Manager's own switch, both on: the one test for starting or serving a task Manager."""
    return _enabled and _manager_answers


def set_manager_answers(value: bool) -> None:
    global _manager_answers
    _manager_answers = value


async def load() -> None:
    global _max_resident, _manager_mcp
    stored = await db_settings.get_settings([KEY, MANAGER_ANSWERS_KEY, MAX_RESIDENT_KEY, MANAGER_MCP_KEY])
    set_enabled(stored.get(KEY) == "true")
    set_manager_answers(stored.get(MANAGER_ANSWERS_KEY) == "true")
    _max_resident = int(stored.get(MAX_RESIDENT_KEY, DEFAULT_MAX_RESIDENT))
    _checked_max_resident(_max_resident)
    names = json.loads(stored[MANAGER_MCP_KEY]) if stored.get(MANAGER_MCP_KEY) else []
    _checked_manager_mcp(names)
    _manager_mcp = tuple(names)


def max_resident() -> int:
    return _max_resident


def manager_mcp() -> tuple[str, ...]:
    """Names of the operator's MCP servers the task Manager's direct session may load, beside Looper's own."""
    return _manager_mcp


def view() -> dict[str, Any]:
    return {"enabled": enabled(), "manager_answers": manager_answers(), "max_resident": max_resident(),
            "manager_mcp": list(manager_mcp())}


def _stored(value: bool | None, name: str = "enabled") -> str | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be true, false or null")
    return None if value is None else ("true" if value else "false")


def _checked_max_resident(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError("max_resident must be an integer from 1 to 20 or null")
    return str(value)


def _checked_manager_mcp(value: object) -> str | None:
    """The stored JSON of a valid list of MCP server names; ``None`` (and ``[]``) store nothing."""
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) > MAX_MANAGER_MCP
            or not all(isinstance(name, str) and MCP_NAME.fullmatch(name) for name in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"manager_mcp must be null or a list of up to {MAX_MANAGER_MCP} unique MCP server names "
                         "(letters, digits, '_', '-', '.'; at most 64 characters)")
    return json.dumps(value) if value else None


async def update(value: bool | None | object = _UNSET, *, max_resident: int | None | object = _UNSET,
                 manager_answers: bool | None | object = _UNSET,
                 manager_mcp: list[str] | None | object = _UNSET) -> dict[str, Any]:
    """Persist first: a failed write leaves the effective setting unchanged."""
    global _max_resident, _manager_mcp
    fields = {}
    if value is not _UNSET:
        fields[KEY] = _stored(value)
    if manager_answers is not _UNSET:
        fields[MANAGER_ANSWERS_KEY] = _stored(manager_answers, "manager_answers")
    if max_resident is not _UNSET:
        fields[MAX_RESIDENT_KEY] = _checked_max_resident(max_resident)
    if manager_mcp is not _UNSET:
        fields[MANAGER_MCP_KEY] = _checked_manager_mcp(manager_mcp)
    await db_settings.put_settings(fields)
    if value is not _UNSET:
        set_enabled(False if value is None else value)
    if manager_answers is not _UNSET:
        set_manager_answers(False if manager_answers is None else manager_answers)
    if manager_mcp is not _UNSET:
        _manager_mcp = tuple(manager_mcp or ())
    if max_resident is not _UNSET:
        _max_resident = DEFAULT_MAX_RESIDENT if max_resident is None else max_resident
    return view()
