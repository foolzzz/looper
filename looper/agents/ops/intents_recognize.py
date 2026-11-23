"""An operator-confirmed, one-off recognition in one group's collected messages."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...db import connection
from .. import leads_model
from ..message import BOT_CHANNEL_TYPES, builtin_channel_id
from ..subscriptions import strip_mentions
from . import Audience, Call, ErrorCode, OpError, RestrictedTool, Risk

logger = logging.getLogger(__name__)
TOOL_NAME = "looper_intents_recognize"
CONTRACT = """\
## Recognizing requests in a group (looper_intents_recognize)
When the user asks what requests or problems a group raised over a period, call it with action preview (the chat,
and since/until; the last 7 days by default, at most 30), tell the user the message count and the estimated cost,
and call start only after they agree; Looper asks them to confirm too. Results are intents (I-...), listed in one
message, never pushed one by one. Use status to read a recognition's results. Never use it in a group chat.
"""


async def enabled() -> bool:
    """The tool and its contract are absent unless both switches can be read as on."""
    try:
        return await leads_model.intents_enabled() and await leads_model.on_request_enabled()
    except Exception:
        logger.debug("could not read intent recognition switches; recognition counts as off", exc_info=True)
        return False


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise OpError(ErrorCode.INVALID, "since and until must be ISO timestamps with a timezone") from None
    if result.tzinfo is None:
        raise OpError(ErrorCode.INVALID, "since and until must include a timezone")
    return result


async def _chat(value: str | None) -> dict[str, Any]:
    if not value or not value.strip():
        raise OpError(ErrorCode.INVALID, "chat is required for preview and start", field="chat")
    db = connection.get_pool()
    rows = await db.fetch("SELECT source_id,chat_id,name FROM source_chats WHERE kind IN ('group','topic') "
                          "AND chat_id=$1 ORDER BY source_id LIMIT 6", value.strip())
    if not rows:
        rows = await db.fetch("SELECT source_id,chat_id,name FROM source_chats WHERE kind IN ('group','topic') "
                              "AND strpos(lower(COALESCE(name,'')),lower($1))>0 ORDER BY name,chat_id LIMIT 6",
                              value.strip())
    if not rows:
        raise OpError(ErrorCode.INVALID, "no group or topic chat matches chat", field="chat")
    if len(rows) > 1:
        names = "、".join(strip_mentions(row["name"] or row["chat_id"]) for row in rows[:5])
        raise OpError(ErrorCode.INVALID, f"chat matches several groups; choose a unique chat id: {names}", field="chat")
    return dict(rows[0])


async def _preview(call: Call, *, chat: dict[str, Any] | None = None) -> dict[str, Any]:
    from .. import intent_recognition
    from ..intents import IntentRefused

    chat = chat or await _chat(call.arguments.get("chat"))
    try:
        return await intent_recognition.preview(source_id=chat["source_id"], chat_id=chat["chat_id"],
                                                since=_time(call.arguments.get("since")),
                                                until=_time(call.arguments.get("until")))
    except IntentRefused as exc:
        raise OpError(ErrorCode(exc.code), str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


async def _confirm(call: Call) -> str | None:
    if call.arguments["action"] != "start":
        return None
    chat = await _chat(call.arguments.get("chat"))
    found = await _preview(call, chat=chat)
    # Persist concrete bounds: a later confirmation cannot silently extend the range.
    call.confirmation.update(source_id=chat["source_id"], chat_id=chat["chat_id"],
                             since=found["range_from"], until=found["range_to"])
    truncated = (f"仅最新 2,000 条，实际起点 {found['effective_from']}" if found["truncated"] else "不截断")
    low, high = found["estimate_usd"]
    return strip_mentions(f"识别《{found['chat']['name']}》{found['range_from']}–{found['range_to']} 的 "
                          f"{found['events']} 条人发消息（{truncated}），约 ${low}–${high}，上限 $1。"
                          "结果作为意图入库，在这里列出，不逐条推送。")


async def run(call: Call) -> dict[str, Any]:
    from .. import intent_recognition
    from ..intents import IntentRefused

    from .service import _operator_turn

    # Confirmation executes in the next turn; recheck its identity before reading or starting.
    if call.scoped or not await _operator_turn(call.turn):
        raise OpError(ErrorCode.FORBIDDEN, TOOL.refusal)
    action = call.arguments["action"]
    if action == "preview":
        return await _preview(call)
    if action == "status":
        raw = call.arguments.get("recognition")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise OpError(ErrorCode.INVALID, "recognition must be R-12 or a positive number", field="recognition")
        try:
            return await intent_recognition.status(raw)
        except IntentRefused as exc:
            raise OpError(ErrorCode(exc.code), str(exc)) from None
        except ValueError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    if not call.confirmation:
        raise OpError(ErrorCode.FORBIDDEN, "start requires the operator's confirmation")
    reply = None
    if call.turn.channel in BOT_CHANNEL_TYPES:
        reply = (call.turn.channel_id or builtin_channel_id(call.turn.channel), call.turn.chat)
    try:
        result = await intent_recognition.service().begin(
            source_id=call.confirmation["source_id"], chat_id=call.confirmation["chat_id"],
            since=_time(call.confirmation["since"]), until=_time(call.confirmation["until"]),
            requested_via="manager", reply=reply)
    except IntentRefused as exc:
        raise OpError(ErrorCode(exc.code), str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    return {**result, "notice": "完成后会发到这里" if reply else
            "完成后在意图页的识别记录里；问我『识别结果』，我用 status 读给你"}


TOOL = RestrictedTool(
    name=TOOL_NAME, audience=Audience.OPERATOR, risk=Risk.WRITE, run=run, escalate=_confirm,
    refusal="只能在你和我的私聊或控制台里用，不能在群里或替别人用。",
    read_actions=frozenset({"preview", "status"}),
    description="Recognize development requests in one group's collected messages once: preview the count and "
                "estimated cost first, then start only after the user agrees (Looper asks for confirmation too). "
                "Default last 7 days, at most 30 days, 2,000 messages and $1. Results are intents, listed together, "
                "never pushed individually. Only the operator's own direct chat or console. Use status with R-id.",
    schema={"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "action": {"type": "string", "enum": ["preview", "start", "status"]},
        "chat": {"type": "string", "minLength": 1, "maxLength": 200},
        "since": {"type": "string", "minLength": 1}, "until": {"type": "string", "minLength": 1},
        "recognition": {"anyOf": [{"type": "string", "minLength": 1},
                                    {"type": "integer", "minimum": 1}]}}},
)
