"""The chat's system settings (``system_settings``), edited in Settings → Chat.

``chat.stop_enabled``: whether a reply can be stopped (Esc and the Stop button in the dashboard,
``POST /api/chat/stop``, ``/stop`` on Telegram and Lark). Experimental, and off by default until the criteria of chat-stop-v2 section 10 are met.

``manager.completion_turns``: whether a task's end in a direct chat or the dashboard chat is told by the Manager
in a read-only turn of its own (``dm_only``) instead of the code notice (``off``, the default; manager completion
ledger RFC section 3.7).

``manager.research_order``: whether the default Manager's contract says where to look things up, and in what order,
before it asks the user (``research_order``; off by default).

``ledger.enabled``: whether the ledger records what waits for the user, what was decided and who does what
(off by default). The ledger reads it from the database (``db.ledger.enabled``); this module does not keep a
second copy. Turning it on names the enabled bots that have no operator id.

Stop, the completion events and the Manager's prompt check the first three from synchronous code, so they are
kept in memory: ``load`` reads them at startup and ``update`` refreshes them. Until loaded, they are off.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from ..db import ledger as db_ledger
from ..db import system_settings as db_settings

STOP_ENABLED_KEY = "chat.stop_enabled"
DEFAULT_STOP_ENABLED = False
COMPLETION_TURNS_KEY = "manager.completion_turns"
RESEARCH_ORDER_KEY = "manager.research_order"
DEFAULT_RESEARCH_ORDER = False
FIELDS = ("stop_enabled", "completion_turns", "research_order", "ledger_enabled")

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
    stored = await db_settings.get_settings([STOP_ENABLED_KEY, COMPLETION_TURNS_KEY, RESEARCH_ORDER_KEY])
    set_stop_enabled(stored.get(STOP_ENABLED_KEY) == "true")
    set_research_order(stored.get(RESEARCH_ORDER_KEY) == "true")
    try:
        turns = CompletionTurns(stored.get(COMPLETION_TURNS_KEY, DEFAULT_COMPLETION_TURNS.value))
    except ValueError:
        turns = DEFAULT_COMPLETION_TURNS
    set_completion_turns(turns)
    return _stop_enabled


async def settings_view() -> dict[str, Any]:
    """The settings in effect. ``ledger_enabled`` is the stored row (``db.ledger.enabled``), not a copy in memory."""
    return {"stop_enabled": _stop_enabled, "completion_turns": _completion_turns.value,
            "research_order": _research_order, "ledger_enabled": await db_ledger.enabled()}


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
    if "ledger_enabled" in body:
        value = body["ledger_enabled"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("ledger_enabled must be true, false or null")
        values["ledger_enabled"] = value
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
    if "ledger_enabled" in values:
        value = values["ledger_enabled"]
        stored[db_ledger.ENABLED_KEY] = None if value is None else ("true" if value else "false")
    await db_settings.put_settings(stored)
    await load()
    view = await settings_view()
    if values.get("ledger_enabled") is True:
        from .channels.operator_ids import missing

        view["missing_operator_ids"] = [
            {"channel_id": bot.channel_id, "channel_type": bot.channel_type, "name": bot.name}
            for bot in await missing()]
    return view
