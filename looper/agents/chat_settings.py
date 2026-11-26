"""The chat's system settings (``system_settings``), edited in Settings → Chat.

``chat.stop_enabled``: whether a reply can be stopped (Esc and the Stop button in the dashboard,
``POST /api/chat/stop``, ``/stop`` on Telegram and Lark). Experimental, and off by default until the criteria of chat-stop-v2 section 10 are met.

``manager.completion_turns``: whether a task's end in a direct chat or the dashboard chat is told by the Manager
in a read-only turn of its own (``dm_only``) instead of the code notice (``off``, the default; manager completion
ledger RFC section 3.7).

``manager.research_order``: whether the default Manager's contract says where to look things up, and in what order,
before it asks the user (``research_order``; off by default).

``manager.metrics.enabled``: operator-only observational metrics, off by default; edited through
``manager_metrics_settings.get`` and ``manager_metrics_settings.update``, separately from client chat settings.

``channels.outbound.enabled``: the operator's shared write gateway and durable channel outbox
(``outbound_enabled``; off by default), configured only through ``outbound_gateway.get/update``.
Lark is wired first; Telegram follows separately.

``manager.delivery_guard.enabled``: operator-only delivery guard configuration, disabled by default.
The Manager behavior is wired separately; its v1 operations are ``delivery_guard.get`` and ``delivery_guard.update``.

``manager.mid_turn_input.enabled``: whether the default Manager merges eligible operator messages into a
running user turn; disabled by default, configured only through ``mid_turn_input.get/update``.

``ledger.enabled``: whether the ledger records what waits for the user, what was decided and who does what
(off by default). The ledger reads it from the database (``db.ledger.enabled``); this module does not keep a
second copy. Turning it on names the enabled bots that have no operator id.

``delivery.enabled``: whether proactive messages pass the delivery layer (``proactive_delivery``) before they go
out; off by default (proactive delivery RFC section 2). Operator-only, through ``proactive_delivery.get`` and
``proactive_delivery.update``, like the delivery guard.

``morning_brief.enabled``, ``morning_brief.time`` and ``morning_brief.cutoff``: the weekday morning brief
(off by default, 09:15 to 12:00 local).
Operator-only, through ``morning_brief.get`` and ``morning_brief.update``. Not part of the chat settings clients see.

``manager.session_routing.enabled`` and ``manager.sessions.max_resident``: whether the default Manager gives each
group and each other sender's direct chat a session of its own, and how many sessions keep a process resident
(``session_routing``; off, and 2). Operator-only. Turning routing on names the enabled bots that have no operator
id: their direct chats stay in the main session. Configured through ``session_routing.get/update``.

Turn context, timezone and reply language have separate operator-only
``turn_context_settings.get/update`` operations; they are not client chat settings.

Stop, completion turns, research order, metrics, delivery guard, outbound writes, turn context, the delivery
layer, session routing and the morning brief are kept in memory: ``load`` reads them at startup and updates
refresh them. Until loaded, the switches are off and the brief's time is 09:15.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import asdict
from datetime import time
from enum import Enum
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import session_routing
from ..db import ledger as db_ledger
from ..db import system_settings as db_settings

STOP_ENABLED_KEY = "chat.stop_enabled"
DEFAULT_STOP_ENABLED = False
COMPLETION_TURNS_KEY = "manager.completion_turns"
RESEARCH_ORDER_KEY = "manager.research_order"
DEFAULT_RESEARCH_ORDER = False
METRICS_ENABLED_KEY = "manager.metrics.enabled"
DEFAULT_METRICS_ENABLED = False
DELIVERY_GUARD_KEY = "manager.delivery_guard.enabled"
DEFAULT_DELIVERY_GUARD_ENABLED = False
MID_TURN_INPUT_KEY = "manager.mid_turn_input.enabled"
DEFAULT_MID_TURN_INPUT_ENABLED = False
OUTBOUND_ENABLED_KEY = "channels.outbound.enabled"
DEFAULT_OUTBOUND_ENABLED = False
OUTBOUND_DEFAULTS = dict(send_qps=5, write_qps=15, edit_interval_seconds=1, max_in_flight=4)
# Rate and edit defaults are conservative platform assumptions, to verify (RFC section 7).
OUTBOUND_FIELDS = ("outbound_enabled", *OUTBOUND_DEFAULTS)
DELIVERY_ENABLED_KEY = "delivery.enabled"
DEFAULT_DELIVERY_ENABLED = False
MORNING_BRIEF_ENABLED_KEY = "morning_brief.enabled"
MORNING_BRIEF_TIME_KEY = "morning_brief.time"
MORNING_BRIEF_CUTOFF_KEY = "morning_brief.cutoff"
DEFAULT_MORNING_BRIEF_ENABLED = False
DEFAULT_MORNING_BRIEF_TIME = "09:15"
DEFAULT_MORNING_BRIEF_CUTOFF = "12:00"
_MORNING_BRIEF_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
FIELDS = ("stop_enabled", "completion_turns", "research_order", "ledger_enabled")
OPERATOR_FIELDS = ("ledger_enabled",)  # only the operator sets the ledger
SESSION_ROUTING_FIELDS = ("session_routing_enabled", "max_resident")
logger = logging.getLogger(__name__)

TURN_CONTEXT_KEY = "manager.turn_context.enabled"
TIMEZONE_KEY = "user.timezone"
REPLY_LANGUAGE_KEY = "user.reply_language"
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
_metrics_enabled = DEFAULT_METRICS_ENABLED
_outbound_enabled = DEFAULT_OUTBOUND_ENABLED
_outbound_limits = dict(OUTBOUND_DEFAULTS)
_delivery_guard_enabled = DEFAULT_DELIVERY_GUARD_ENABLED
_mid_turn_input_enabled = DEFAULT_MID_TURN_INPUT_ENABLED
_delivery_enabled = DEFAULT_DELIVERY_ENABLED
_morning_brief_enabled = DEFAULT_MORNING_BRIEF_ENABLED
_morning_brief_time = DEFAULT_MORNING_BRIEF_TIME
_morning_brief_cutoff: str | None = None


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


def metrics_enabled() -> bool:
    return _metrics_enabled


def set_metrics_enabled(value: bool) -> None:
    global _metrics_enabled
    _metrics_enabled = value


def metrics_view() -> dict[str, bool]:
    return {"enabled": _metrics_enabled}


async def update_metrics(value: bool | None) -> dict[str, bool]:
    """Persist the operator-only metrics switch separately from client chat settings."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await db_settings.put_settings({METRICS_ENABLED_KEY: None if value is None else ("true" if value else "false")})
    set_metrics_enabled(DEFAULT_METRICS_ENABLED if value is None else value)
    return metrics_view()


def outbound_enabled() -> bool:
    return _outbound_enabled


def set_outbound_enabled(value: bool) -> None:
    """Apply the operator's outbound control in memory; persistent updates go through ``update_outbound``."""
    global _outbound_enabled
    _outbound_enabled = value


def outbound_limits() -> dict[str, float | int]:
    return dict(_outbound_limits)


def outbound_view() -> dict[str, Any]:
    return {"outbound_enabled": outbound_enabled(), **outbound_limits()}


def delivery_guard_enabled() -> bool:
    return _delivery_guard_enabled


def set_delivery_guard_enabled(value: bool) -> None:
    """Apply the delivery setting in memory; Manager wiring is a separate change."""
    global _delivery_guard_enabled
    _delivery_guard_enabled = value


def delivery_guard_view() -> dict[str, bool]:
    return {"enabled": _delivery_guard_enabled}


async def update_delivery_guard(value: bool | None) -> dict[str, bool]:
    """Persist an operator's choice; null restores the disabled default.

    Separate from ``update`` so existing chat settings exposed to clients stay unchanged.
    """
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await db_settings.put_settings({DELIVERY_GUARD_KEY: None if value is None else ("true" if value else "false")})
    set_delivery_guard_enabled(DEFAULT_DELIVERY_GUARD_ENABLED if value is None else value)
    return delivery_guard_view()


def mid_turn_input_enabled() -> bool:
    return _mid_turn_input_enabled


def set_mid_turn_input_enabled(value: bool) -> None:
    global _mid_turn_input_enabled
    _mid_turn_input_enabled = value


def mid_turn_input_view() -> dict[str, bool]:
    return {"enabled": _mid_turn_input_enabled}


async def update_mid_turn_input(value: bool | None) -> dict[str, bool]:
    """Persist the operator's merge switch without extending the client chat settings contract."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await db_settings.put_settings({MID_TURN_INPUT_KEY: None if value is None else ("true" if value else "false")})
    set_mid_turn_input_enabled(DEFAULT_MID_TURN_INPUT_ENABLED if value is None else value)
    return mid_turn_input_view()


def delivery_enabled() -> bool:
    return _delivery_enabled


def set_delivery_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update_delivery`` store it; tests use this directly)."""
    global _delivery_enabled
    _delivery_enabled = value


def delivery_view() -> dict[str, bool]:
    return {"enabled": _delivery_enabled}


async def update_delivery(value: bool | None) -> dict[str, bool]:
    """Persist the operator's choice; null restores the default, off."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    await db_settings.put_settings({DELIVERY_ENABLED_KEY: None if value is None else ("true" if value else "false")})
    set_delivery_enabled(DEFAULT_DELIVERY_ENABLED if value is None else value)
    return delivery_view()


def morning_brief_enabled() -> bool:
    return _morning_brief_enabled


def set_morning_brief_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``update_morning_brief`` store it; tests use this directly)."""
    global _morning_brief_enabled
    _morning_brief_enabled = value


def morning_brief_time() -> str:
    return _morning_brief_time


def set_morning_brief_time(value: str) -> None:
    """Apply a value in memory only (``load`` and ``update_morning_brief`` store it; tests use this directly)."""
    global _morning_brief_time
    _morning_brief_time = value


class MorningBriefBoundary(Enum):
    END_OF_DAY = "end_of_day"


def morning_brief_cutoff() -> time | MorningBriefBoundary:
    return time.fromisoformat(_morning_brief_cutoff) if _morning_brief_cutoff is not None else derived_morning_brief_cutoff(_morning_brief_time)


def derived_morning_brief_cutoff(when: str) -> time | MorningBriefBoundary:
    """Keep a same-day catch-up window for send times stored before cutoffs existed."""
    if when < DEFAULT_MORNING_BRIEF_CUTOFF:
        return time.fromisoformat(DEFAULT_MORNING_BRIEF_CUTOFF)
    hour, minute = map(int, when.split(":"))
    minutes = hour * 60 + minute + 180
    return MorningBriefBoundary.END_OF_DAY if minutes >= 24 * 60 else time(minutes // 60, minutes % 60)


def _morning_brief_cutoff_view(cutoff: time | MorningBriefBoundary) -> str | dict[str, str]:
    """24:00 is an exclusive day boundary, not a wall-clock time or a magic string."""
    return {"kind": cutoff.value} if isinstance(cutoff, MorningBriefBoundary) else cutoff.strftime("%H:%M")


def set_morning_brief_cutoff(value: str | None) -> None:
    """Apply a validated stored cutoff, or None to derive it from the send time."""
    global _morning_brief_cutoff
    _morning_brief_cutoff = value


def morning_brief_view() -> dict[str, Any]:
    return {"enabled": _morning_brief_enabled, "time": _morning_brief_time, "cutoff": _morning_brief_cutoff_view(morning_brief_cutoff()),
            "cutoff_source": "stored" if _morning_brief_cutoff is not None else "derived"}


def checked_morning_brief(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete prospective window before confirmation or any write."""
    unknown = [key for key in body if key not in ("enabled", "time", "cutoff")]
    if unknown:
        raise ValueError(f"unknown setting {unknown[0]!r}; the morning brief has enabled, time, cutoff")
    if not body:
        raise ValueError("give at least one of enabled, time, cutoff (null: the default)")
    enabled = body["enabled"] if "enabled" in body else None
    when = body["time"] if "time" in body else None
    if "enabled" in body and enabled is not None and not isinstance(enabled, bool):
        raise ValueError("enabled must be true, false or null")
    cutoff = body.get("cutoff")
    for name, value in (("time", when), ("cutoff", cutoff)):
        if name in body and value is not None and (not isinstance(value, str) or _MORNING_BRIEF_TIME.fullmatch(value) is None):
            raise ValueError(f"{name} must be HH:MM or null")
    effective_time = (DEFAULT_MORNING_BRIEF_TIME if when is None else when) if "time" in body else _morning_brief_time
    chosen_cutoff = cutoff if "cutoff" in body else _morning_brief_cutoff
    if chosen_cutoff is not None and chosen_cutoff <= effective_time:
        raise ValueError("cutoff must be later than time")
    effective_cutoff = time.fromisoformat(chosen_cutoff) if chosen_cutoff is not None else derived_morning_brief_cutoff(effective_time)
    return {"enabled": (DEFAULT_MORNING_BRIEF_ENABLED if enabled is None else enabled) if "enabled" in body else _morning_brief_enabled,
            "time": effective_time, "cutoff": _morning_brief_cutoff_view(effective_cutoff)}


async def update_morning_brief(body: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a validated choice; null cutoff restores derivation from the send time."""
    checked_morning_brief(body)
    stored: dict[str, str | None] = {}
    if "enabled" in body:
        enabled = body["enabled"]
        stored[MORNING_BRIEF_ENABLED_KEY] = None if enabled is None else ("true" if enabled else "false")
    if "time" in body:
        stored[MORNING_BRIEF_TIME_KEY] = body["time"]
    if "cutoff" in body:
        stored[MORNING_BRIEF_CUTOFF_KEY] = body["cutoff"]
    await db_settings.put_settings(stored)
    if "enabled" in body:
        set_morning_brief_enabled(DEFAULT_MORNING_BRIEF_ENABLED if enabled is None else enabled)
    if "time" in body:
        when = body["time"]
        set_morning_brief_time(DEFAULT_MORNING_BRIEF_TIME if when is None else when)
    if "cutoff" in body:
        set_morning_brief_cutoff(body["cutoff"])
    return morning_brief_view()


async def load() -> bool:
    """Read the stored settings at startup; a missing or unreadable value is the default (off). Answers whether
    stopping a reply is on."""
    from . import compaction_restore, receipts_settings, status_card

    global _turn_context_enabled, _timezone, _reply_language
    stored = await db_settings.get_settings([
        STOP_ENABLED_KEY, COMPLETION_TURNS_KEY, RESEARCH_ORDER_KEY, METRICS_ENABLED_KEY, DELIVERY_GUARD_KEY,
        MID_TURN_INPUT_KEY,
        TURN_CONTEXT_KEY, TIMEZONE_KEY, REPLY_LANGUAGE_KEY, OUTBOUND_ENABLED_KEY, DELIVERY_ENABLED_KEY,
        session_routing.ROUTING_KEY, session_routing.MAX_RESIDENT_KEY, status_card.ENABLED_KEY,
        receipts_settings.ENABLED_KEY, compaction_restore.KEY,
        *("channels.outbound." + key for key in OUTBOUND_DEFAULTS),
        MORNING_BRIEF_ENABLED_KEY, MORNING_BRIEF_TIME_KEY, MORNING_BRIEF_CUTOFF_KEY,
    ])
    session_routing.apply_stored(stored)
    _turn_context_enabled = stored.get(TURN_CONTEXT_KEY) == "true"
    _timezone = stored.get(TIMEZONE_KEY)
    _reply_language = stored.get(REPLY_LANGUAGE_KEY, "zh-CN")
    if _reply_language not in ("zh-CN", "en"):
        _reply_language = "zh-CN"
    set_stop_enabled(stored.get(STOP_ENABLED_KEY) == "true")
    set_metrics_enabled(stored.get(METRICS_ENABLED_KEY) == "true")
    set_research_order(stored.get(RESEARCH_ORDER_KEY) == "true")
    set_delivery_guard_enabled(stored.get(DELIVERY_GUARD_KEY) == "true")
    set_mid_turn_input_enabled(stored.get(MID_TURN_INPUT_KEY) == "true")
    set_delivery_enabled(stored.get(DELIVERY_ENABLED_KEY) == "true")
    set_morning_brief_enabled(stored.get(MORNING_BRIEF_ENABLED_KEY) == "true")
    raw_time = stored.get(MORNING_BRIEF_TIME_KEY)
    set_morning_brief_time(raw_time if isinstance(raw_time, str) and _MORNING_BRIEF_TIME.fullmatch(raw_time)
                           else DEFAULT_MORNING_BRIEF_TIME)
    raw_cutoff = stored.get(MORNING_BRIEF_CUTOFF_KEY)
    if raw_cutoff is not None and (not isinstance(raw_cutoff, str) or not _MORNING_BRIEF_TIME.fullmatch(raw_cutoff)
                                   or raw_cutoff <= morning_brief_time()):
        logger.warning("Ignoring invalid morning_brief.cutoff %r for send time %s; deriving cutoff",
                       raw_cutoff, morning_brief_time())
        raw_cutoff = None
    set_morning_brief_cutoff(raw_cutoff)
    set_outbound_enabled(stored.get(OUTBOUND_ENABLED_KEY) == "true")
    limits = {}
    for key, default in OUTBOUND_DEFAULTS.items():
        raw = stored.get("channels.outbound." + key)
        try:
            value = float(raw) if raw is not None else default
            if key == "max_in_flight" and value == int(value):
                value = int(value)
            checked_outbound({key: value})
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
    status_card.apply_stored(stored.get(status_card.ENABLED_KEY))
    receipts_settings.apply_stored(stored.get(receipts_settings.ENABLED_KEY))
    compaction_restore.apply_stored(stored.get(compaction_restore.KEY))
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


def session_routing_view() -> dict[str, Any]:
    return {"session_routing_enabled": session_routing.routing_enabled(),
            "max_resident": session_routing.max_resident()}


def checked_session_routing(body: Mapping[str, Any]) -> dict[str, Any]:
    if unknown := [key for key in body if key not in SESSION_ROUTING_FIELDS]:
        raise ValueError(f"unknown session routing setting {unknown[0]!r}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(SESSION_ROUTING_FIELDS)} (null: the default)")
    values: dict[str, Any] = {}
    if "session_routing_enabled" in body:
        value = body["session_routing_enabled"]
        if value is not None and not isinstance(value, bool):
            raise ValueError("session_routing_enabled must be true, false or null")
        values["session_routing_enabled"] = value
    if "max_resident" in body:
        value = body["max_resident"]
        values["max_resident"] = None if value is None else session_routing.checked_max_resident(value)
    return values


async def update_session_routing(body: Mapping[str, Any]) -> dict[str, Any]:
    values = checked_session_routing(body)
    stored: dict[str, str | None] = {}
    if "session_routing_enabled" in values:
        value = values["session_routing_enabled"]
        stored[session_routing.ROUTING_KEY] = None if value is None else ("true" if value else "false")
    if "max_resident" in values:
        value = values["max_resident"]
        stored[session_routing.MAX_RESIDENT_KEY] = None if value is None else str(value)
    await db_settings.put_settings(stored)
    await load()
    view = session_routing_view()
    if values.get("session_routing_enabled") is True:
        from .channels.operator_ids import missing

        view["missing_operator_ids"] = [
            {"channel_id": bot.channel_id, "channel_type": bot.channel_type, "name": bot.name}
            for bot in await missing()]
    return view


TURN_CONTEXT_FIELDS = ("turn_context_enabled", "timezone", "reply_language")


def turn_context_view() -> dict[str, Any]:
    """Operator-only settings, kept out of the existing client chat view."""
    from .turn_context import server_zone

    return {"turn_context_enabled": _turn_context_enabled, "timezone": _timezone,
            "server_timezone": str(server_zone()), "reply_language": _reply_language}


def checked_turn_context(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate all named fields before any setting is stored."""
    if unknown := [key for key in body if key not in TURN_CONTEXT_FIELDS]:
        raise ValueError(f"unknown turn context setting {unknown[0]!r}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(TURN_CONTEXT_FIELDS)} (null: the default)")
    values: dict[str, Any] = {}
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


async def update_turn_context(body: Mapping[str, Any]) -> dict[str, Any]:
    """Store only named fields; null restores the default. A refused body writes nothing."""
    values = checked_turn_context(body)
    stored: dict[str, str | None] = {}
    if "turn_context_enabled" in values:
        value = values["turn_context_enabled"]
        stored[TURN_CONTEXT_KEY] = None if value is None else ("true" if value else "false")
    for field, key in (("timezone", TIMEZONE_KEY), ("reply_language", REPLY_LANGUAGE_KEY)):
        if field in values:
            stored[key] = values[field]
    missing_bots = None
    if values.get("turn_context_enabled") is True:
        from .channels.operator_ids import missing

        # A failed lookup must not silently enable the switch.
        missing_bots = await missing()
    await db_settings.put_settings(stored)
    await load()
    view = turn_context_view()
    if missing_bots is not None:
        view["missing_operator_ids"] = [asdict(bot) for bot in missing_bots]
    return view


def checked_outbound(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate operator-only gateway controls without extending the chat settings contract."""
    if unknown := [key for key in body if key not in OUTBOUND_FIELDS]:
        raise ValueError(f"unknown outbound setting {unknown[0]!r}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(OUTBOUND_FIELDS)} (null: the default)")
    values: dict[str, Any] = {}
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
    return values


async def update_outbound(body: Mapping[str, Any]) -> dict[str, Any]:
    """Persist and refresh gateway controls; null restores each disabled or conservative default."""
    values = checked_outbound(body)
    stored: dict[str, str | None] = {}
    if "outbound_enabled" in values:
        value = values["outbound_enabled"]
        stored[OUTBOUND_ENABLED_KEY] = None if value is None else ("true" if value else "false")
    for key in OUTBOUND_DEFAULTS:
        if key in values:
            stored["channels.outbound." + key] = None if values[key] is None else str(values[key])
    await db_settings.put_settings(stored)
    await load()
    return outbound_view()
