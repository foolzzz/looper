"""The chat's system settings (``system_settings``), edited in Settings → Chat.

``chat.stop_enabled``: whether a reply can be stopped (Esc and the Stop button in the dashboard,
``POST /api/chat/stop``, ``/stop`` on Telegram and Lark). Experimental, and off by default until the criteria of chat-stop-v2 section 10 are met.

``manager.completion_turns``: whether a task's end in a direct chat or the dashboard chat is told by the Manager
in a read-only turn of its own (``dm_only``) instead of the code notice (``off``, the default; manager completion
ledger RFC section 3.7).

``manager.research_order``: whether the default Manager's contract says where to look things up, and in what order,
before it asks the user (``research_order``; off by default).

Stop, the completion events and the Manager's prompt check them from synchronous code, so they are kept in memory: ``load`` reads them
at startup and ``update`` refreshes them. Until loaded, both are off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import system_settings as db_settings

STOP_ENABLED_KEY = "chat.stop_enabled"
DEFAULT_STOP_ENABLED = False
COMPLETION_TURNS_KEY = "manager.completion_turns"
RESEARCH_ORDER_KEY = "manager.research_order"
DEFAULT_RESEARCH_ORDER = False
TURN_CONTEXT_KEY = "manager.turn_context.enabled"
TIMEZONE_KEY = "user.timezone"
REPLY_LANGUAGE_KEY = "user.reply_language"
FIELDS = ("stop_enabled", "completion_turns", "research_order", "turn_context_enabled", "timezone", "reply_language")
_turn_context_enabled = False
_timezone: str | None = None
_reply_language = "zh-CN"


def turn_context_enabled() -> bool:
    return _turn_context_enabled


def timezone() -> str | None:
    return _timezone


def reply_language() -> str:
    return _reply_language


STOP_DISABLED_REPLY = "停止功能暂未开放"


class CompletionTurns(str, Enum):
    """Who tells a direct chat or the dashboard chat that its task ended: the code notice, or the Manager."""

    OFF = "off"
    DM_ONLY = "dm_only"  # direct chats and the dashboard chat; a group chat always gets the code notice


DEFAULT_COMPLETION_TURNS = CompletionTurns.OFF

_stop_enabled = DEFAULT_STOP_ENABLED
_completion_turns = DEFAULT_COMPLETION_TURNS
_research_order = DEFAULT_RESEARCH_ORDER


def stop_enabled() -> bool:
    return _stop_enabled


def set_stop_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _stop_enabled
    _stop_enabled = value


def completion_turns() -> CompletionTurns:
    return _completion_turns


def set_completion_turns(value: CompletionTurns) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _completion_turns
    _completion_turns = CompletionTurns(value)


def research_order() -> bool:
    return _research_order


def set_research_order(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update`` store it; tests use this directly)."""
    global _research_order
    _research_order = value


async def load() -> bool:
    """Read the stored settings at startup; a missing or unreadable value is the default (off). Answers whether
    stopping a reply is on."""
    global _turn_context_enabled, _timezone, _reply_language
    stored = await db_settings.get_settings([STOP_ENABLED_KEY, COMPLETION_TURNS_KEY, RESEARCH_ORDER_KEY,
                                             TURN_CONTEXT_KEY, TIMEZONE_KEY, REPLY_LANGUAGE_KEY])
    _turn_context_enabled = stored.get(TURN_CONTEXT_KEY) == "true"
    _timezone = stored.get(TIMEZONE_KEY)
    _reply_language = stored.get(REPLY_LANGUAGE_KEY, "zh-CN")
    if _reply_language not in ("zh-CN", "en"):
        _reply_language = "zh-CN"
    set_stop_enabled(stored.get(STOP_ENABLED_KEY) == "true")
    set_research_order(stored.get(RESEARCH_ORDER_KEY) == "true")
    try:
        turns = CompletionTurns(stored.get(COMPLETION_TURNS_KEY, DEFAULT_COMPLETION_TURNS.value))
    except ValueError:
        turns = DEFAULT_COMPLETION_TURNS
    set_completion_turns(turns)
    return _stop_enabled


def settings_view() -> dict[str, Any]:
    from .turn_context import server_zone

    return {"stop_enabled": _stop_enabled, "completion_turns": _completion_turns.value, "research_order": _research_order,
            "turn_context_enabled": _turn_context_enabled, "timezone": _timezone,
            "server_timezone": str(server_zone()), "reply_language": _reply_language}


def checked(body: Mapping[str, Any]) -> dict[str, Any]:
    """The settings in ``body`` (each a value, or null for the default); ``ValueError`` for a bad one."""
    if unknown := [key for key in body if key not in FIELDS]:
        raise ValueError(f"unknown setting {unknown[0]!r}; the chat has {', '.join(FIELDS)}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(FIELDS)} (null: the default)")
    values: dict[str, Any] = {}
    if "stop_enabled" in body:
        value = body["stop_enabled"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("stop_enabled must be true, false or null")
        values["stop_enabled"] = value
    if "completion_turns" in body:
        value = body["completion_turns"]
        if value is not None and value not in {turns.value for turns in CompletionTurns}:
            raise ValueError("completion_turns must be " + ", ".join(t.value for t in CompletionTurns) + " or null")
        values["completion_turns"] = value
    if "research_order" in body:
        value = body["research_order"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("research_order must be true, false or null")
        values["research_order"] = value
    if "turn_context_enabled" in body:
        value = body["turn_context_enabled"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("turn_context_enabled must be true, false or null")
        values["turn_context_enabled"] = value
    if "timezone" in body:
        value = body["timezone"]
        if value is not None:
            try:
                if not isinstance(value, str) or not value:
                    raise ValueError()
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("timezone must be a valid IANA timezone name or null") from None
        values["timezone"] = value
    if "reply_language" in body:
        value = body["reply_language"]
        if value is not None and value not in ("zh-CN", "en"):
            raise ValueError("reply_language must be zh-CN, en or null")
        values["reply_language"] = value
    return values


async def update(body: Mapping[str, Any]) -> dict[str, Any]:
    """Store the settings in ``body`` and apply them at once. A refused body changes nothing; a setting the body
    leaves out keeps its value."""
    values = checked(body)
    stored: dict[str, str | None] = {}
    if "stop_enabled" in values:
        value = values["stop_enabled"]
        stored[STOP_ENABLED_KEY] = None if value is None else ("true" if value else "false")
    if "completion_turns" in values:
        stored[COMPLETION_TURNS_KEY] = values["completion_turns"]
    if "research_order" in values:
        value = values["research_order"]
        stored[RESEARCH_ORDER_KEY] = None if value is None else ("true" if value else "false")
    if "turn_context_enabled" in values:
        value = values["turn_context_enabled"]
        stored[TURN_CONTEXT_KEY] = None if value is None else ("true" if value else "false")
    for field, key in (("timezone", TIMEZONE_KEY), ("reply_language", REPLY_LANGUAGE_KEY)):
        if field in values:
            stored[key] = values[field]
    # Resolve the warning before storing: a failed bot lookup must not silently enable the switch.
    missing = None
    if values.get("turn_context_enabled") is True:
        from .channels import operator_ids

        missing = await operator_ids.missing()
    await db_settings.put_settings(stored)
    await load()
    view = settings_view()
    if missing is not None:
        view["missing_operator_ids"] = [asdict(bot) for bot in missing]
    return view
