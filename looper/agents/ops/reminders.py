"""The default Manager's one-time reminder tools (reminders RFC): ``looper_reminder_create``, ``looper_reminder_list``
and ``looper_reminder_cancel``.

Only the default Manager has them, only while ``reminders.enabled`` is on (``ManagerOps.current_scope``), and only
the user's own turns run them (``Audience.OPERATOR``). A reminder goes to the user's direct chat: the one the turn
came from, or, from the dashboard, the first enabled bot of the default Manager that names its operator. Code
resolves the time, stores the reminder, reads it back and adds a line saying what it stored (``Turn.blocks``);
``agents.reminders`` sends it at its time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ...db import reminders as store
from .. import cron
from ..message import BOT_CHANNEL_TYPES, builtin_channel_id
from ..operator_identity import operator_dm_target
from ..reminders import TEXT_MAX, default_timezone, resolve_due, when
from . import Audience, Call, ErrorCode, OpError, RestrictedTool, Risk, Turn

CREATE, LIST, CANCEL = "looper_reminder_create", "looper_reminder_list", "looper_reminder_cancel"
TOOL_NAMES = (CREATE, LIST, CANCEL)
_SHOWN = "Looper adds a line after your reply with the reminder as stored; do not repeat it."


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _target(turn: Turn) -> tuple[str, str, str]:
    """Where a reminder set in ``turn`` goes: (bot, the user's id on it, how the user is told where)."""
    if turn.channel in BOT_CHANNEL_TYPES:
        if not turn.sender:
            raise OpError(ErrorCode.FORBIDDEN, "this message names no sender to remind")
        return turn.channel_id or builtin_channel_id(turn.channel), turn.sender, "这个私聊"
    if (found := await operator_dm_target()) is not None:
        return found.channel_id, found.chat, found.where
    raise OpError(ErrorCode.CONFLICT, "there is no direct chat to send a reminder to: the user names themselves as "
                                      "a bot's operator in Settings → Channels, or asks from their direct chat with "
                                      "the bot")


def _local(moment: datetime | None, zone_name: str) -> str | None:
    return None if moment is None else f"{moment.astimezone(cron.zone(zone_name)):%Y-%m-%d %H:%M}"


def _view(reminder: Mapping[str, Any]) -> dict[str, Any]:
    zone_name = reminder["timezone"]
    return {"id": reminder["id"], "text": reminder["text"], "due": _local(reminder["due_at"], zone_name),
            "timezone": zone_name, "status": reminder["status"], "detail": reminder["detail"],
            "asked_at": _local(reminder["created_at"], zone_name),
            "settled_at": _local(reminder["settled_at"], zone_name)}


async def _create(call: Call) -> dict[str, Any]:
    arguments, now = call.arguments, _now()
    zone_name = arguments.get("timezone") or default_timezone()
    try:
        due = resolve_due(at=arguments.get("at"), in_minutes=arguments.get("in_minutes"), timezone=zone_name,
                          now=now)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc), field="at") from None
    channel_id, chat, where = await _target(call.turn)
    stored = await store.create(text=arguments["text"].strip(), due_at=due.at, timezone=zone_name,
                                channel_id=channel_id, chat=chat, created_channel=call.turn.channel or "web",
                                created_message_id=call.turn.message_id)
    call.turn.blocks.append(f"已记下提醒 #{stored['id']}：{when(stored['due_at'], zone_name, now=now)}"
                            f"（{zone_name}），到时发到{where}：{stored['text']}"
                            + (f"\n注意：{due.note}。" if due.note else ""))
    answer: dict[str, Any] = {"reminder": _view(stored), "shown_to_user": _SHOWN}
    if due.note:  # a clock change moved or chose the time: the line after the reply says so too
        answer["clock_change"] = due.note
    return answer


async def _list(call: Call) -> dict[str, Any]:
    arguments, now = call.arguments, _now()
    status = arguments.get("status") or "pending"
    found, more = await store.list_reminders(status=None if status == "all" else status,
                                             limit=arguments.get("limit") or store.LIST_DEFAULT)
    zone_name = default_timezone()
    return {"now": f"{_local(now, zone_name)} {zone_name}", "reminders": [_view(row) for row in found],
            "more": more}


async def _cancel(call: Call) -> dict[str, Any]:
    reminder_id = call.arguments["id"]
    found, cancelled = await store.cancel(reminder_id, now=_now())
    if found is None:
        raise OpError(ErrorCode.NOT_FOUND, f"there is no reminder #{reminder_id}; looper_reminder_list lists them")
    if not cancelled:
        raise OpError(ErrorCode.CONFLICT, f"reminder #{reminder_id} is {found['status']}: only pending, uncertain, "
                                          "failed or missed reminders can be cancelled; nothing changed")
    call.turn.blocks.append(f"已取消提醒 #{reminder_id}（原定 {when(found['due_at'], found['timezone'])}）："
                            f"{found['text']}")
    return {"reminder": _view(found), "shown_to_user": _SHOWN}


TOOLS: tuple[RestrictedTool, ...] = (
    RestrictedTool(
        refusal=f"{CREATE} runs only in the user's own direct chat or on the dashboard",
        name=CREATE, risk=Risk.WRITE, audience=Audience.OPERATOR, run=_create,
        description="Set a one-time reminder the user asks for: at its time Looper itself sends text (the user's "
                    "own words) to the user's direct chat, without you. Give at (HH:MM: the next such time; or "
                    "YYYY-MM-DD HH:MM), in timezone (IANA; the server's zone by default), or in_minutes from now. "
                    "A repeating reminder is a schedule (looper_schedules), not this. Say it is noted only after "
                    "this call succeeds; Looper adds the stored reminder after your reply.",
        schema={"type": "object", "required": ["text"], "additionalProperties": False, "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": TEXT_MAX},
            "at": {"type": "string", "minLength": 1, "maxLength": 20},
            "in_minutes": {"type": "integer", "minimum": 1, "maximum": 366 * 24 * 60},
            "timezone": {"type": "string", "minLength": 1, "maxLength": 64}}},
    ),
    RestrictedTool(
        refusal=f"{LIST} runs only in the user's own direct chat or on the dashboard",
        name=LIST, risk=Risk.READ, audience=Audience.OPERATOR, run=_list,
        description="List the user's one-time reminders with the time now: the pending ones soonest first, or those "
                    "of status (all: every one, latest first). Each has its id, text, due time and zone, status "
                    "(delivered; missed: Looper was not running at its time; failed; uncertain: it may not have "
                    "been delivered; cancelled) and why.",
        schema={"type": "object", "additionalProperties": False, "properties": {
            "status": {"type": "string",
                       "enum": ["pending", "delivered", "missed", "failed", "uncertain", "cancelled", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": store.LIST_MAX}}},
    ),
    RestrictedTool(
        refusal=f"{CANCEL} runs only in the user's own direct chat or on the dashboard",
        name=CANCEL, risk=Risk.WRITE, audience=Audience.OPERATOR, run=_cancel,
        description="Cancel a pending one-time reminder or dismiss an uncertain, failed or missed one by its id "
                    "(looper_reminder_list gives it), when the user asks. To reschedule, cancel then create a new one. "
                    "Cancelling an uncertain reminder does not determine whether it was delivered.",
        schema={"type": "object", "required": ["id"], "additionalProperties": False, "properties": {
            "id": {"type": "integer", "minimum": 1}}},
    ),
)
