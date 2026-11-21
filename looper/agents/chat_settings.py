"""The chat's system settings (``system_settings``), edited in Settings → Chat.

``chat.stop_enabled``: whether a reply can be stopped (Esc and the Stop button in the dashboard,
``POST /api/chat/stop``, ``/stop`` on Telegram and Lark). Experimental, and off by default until the criteria of chat-stop-v2 section 10 are met.

``manager.completion_turns``: whether a task's end in a direct chat or the dashboard chat is told by the Manager
in a read-only turn of its own (``dm_only``) instead of the code notice (``off``, the default; manager completion
ledger RFC section 3.7).

``manager.research_order``: whether the default Manager's contract says where to look things up, and in what order,
before it asks the user (``research_order``; off by default).

``channels.outbound.enabled``: the operator's shared write gateway and durable channel outbox
(``outbound_enabled``; off by default). Lark is wired first; Telegram follows separately.

``ledger.enabled``: whether the ledger records what waits for the user, what was decided and who does what
(off by default). The ledger reads it from the database (``db.ledger.enabled``); this module does not keep a
second copy. Turning it on names the enabled bots that have no operator id.

Stop, completion events, outbound writes and the Manager's prompt check their settings from synchronous code,
so they are kept in memory: ``load`` reads them at startup and ``update`` refreshes them. Until loaded, they are off.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import math
from typing import Any

from ..db import ledger as db_ledger
from ..db import system_settings as db_settings

STOP_ENABLED_KEY = "chat.stop_enabled"
DEFAULT_STOP_ENABLED = False
COMPLETION_TURNS_KEY = "manager.completion_turns"
RESEARCH_ORDER_KEY = "manager.research_order"
DEFAULT_RESEARCH_ORDER = False
OUTBOUND_ENABLED_KEY = "channels.outbound.enabled"
DEFAULT_OUTBOUND_ENABLED = False
OUTBOUND_DEFAULTS = dict(send_qps=5, write_qps=15, edit_interval_seconds=1, max_in_flight=4)
# Rate and edit defaults are conservative platform assumptions, to verify (RFC section 7).
OUTBOUND_FIELDS = ("outbound_enabled", *OUTBOUND_DEFAULTS)
FIELDS = ("stop_enabled", "completion_turns", "research_order", "ledger_enabled", *OUTBOUND_FIELDS)

STOP_DISABLED_REPLY = "停止功能暂未开放"


class CompletionTurns(str, Enum):
    """Who tells a direct chat or the dashboard chat that its task ended: the code notice, or the Manager."""

    OFF = "off"
    DM_ONLY = "dm_only"  # direct chats and the dashboard chat; a group chat always gets the code notice


DEFAULT_COMPLETION_TURNS = CompletionTurns.OFF

_stop_enabled = DEFAULT_STOP_ENABLED
_completion_turns = DEFAULT_COMPLETION_TURNS
_research_order = DEFAULT_RESEARCH_ORDER
_outbound_enabled = DEFAULT_OUTBOUND_ENABLED
_outbound_limits = dict(OUTBOUND_DEFAULTS)


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


def outbound_enabled() -> bool:
    return _outbound_enabled


def set_outbound_enabled(value: bool) -> None:
    """Apply the operator's outbound control in memory; persistent updates go through ``update``."""
    global _outbound_enabled
    _outbound_enabled = value


def outbound_limits() -> dict[str, float | int]:
    return dict(_outbound_limits)


def outbound_view() -> dict[str, Any]:
    return {"outbound_enabled": outbound_enabled(), **outbound_limits()}


async def load() -> bool:
    """Read the stored settings at startup; a missing or unreadable value is the default (off). Answers whether
    stopping a reply is on."""
    stored = await db_settings.get_settings(
        [STOP_ENABLED_KEY, COMPLETION_TURNS_KEY, RESEARCH_ORDER_KEY, OUTBOUND_ENABLED_KEY,
         *("channels.outbound." + key for key in OUTBOUND_DEFAULTS)])
    set_stop_enabled(stored.get(STOP_ENABLED_KEY) == "true")
    set_research_order(stored.get(RESEARCH_ORDER_KEY) == "true")
    set_outbound_enabled(stored.get(OUTBOUND_ENABLED_KEY) == "true")
    limits = {}
    for key, default in OUTBOUND_DEFAULTS.items():
        raw = stored.get("channels.outbound." + key)
        try:
            value = float(raw) if raw is not None else default
            if key == "max_in_flight" and value == int(value):
                value = int(value)
            checked({key: value})
        except (ValueError, TypeError, OverflowError):
            value = default
        limits[key] = value
    global _outbound_limits
    _outbound_limits = limits
    try:
        turns = CompletionTurns(stored.get(COMPLETION_TURNS_KEY, DEFAULT_COMPLETION_TURNS.value))
    except ValueError:
        turns = DEFAULT_COMPLETION_TURNS
    set_completion_turns(turns)
    return _stop_enabled


async def settings_view() -> dict[str, Any]:
    """The settings in effect. ``ledger_enabled`` is the stored row (``db.ledger.enabled``), not a copy in memory."""
    return {"stop_enabled": _stop_enabled, "completion_turns": _completion_turns.value,
            "research_order": _research_order, "outbound_enabled": _outbound_enabled,
            "ledger_enabled": await db_ledger.enabled()}


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
    if "outbound_enabled" in body:
        value = body["outbound_enabled"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("outbound_enabled must be true, false or null")
        values["outbound_enabled"] = value
    for key in OUTBOUND_DEFAULTS:
        if key not in body:
            continue
        value = body[key]
        if value is not None:
            if key == "max_in_flight":
                if type(value) is not int or not 1 <= value <= 32:
                    raise ValueError("max_in_flight must be an integer from 1 to 32 or null")
            elif type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive number or null")
        values[key] = value
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
    if "outbound_enabled" in values:
        value = values["outbound_enabled"]
        stored[OUTBOUND_ENABLED_KEY] = None if value is None else ("true" if value else "false")
    for key in OUTBOUND_DEFAULTS:
        if key in values:
            stored["channels.outbound." + key] = None if values[key] is None else str(values[key])
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
