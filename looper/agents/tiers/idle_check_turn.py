"""Run an idle wake on main and commit its result before its worker acknowledges it."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ...db import idle_checks as store, llm_records
from .. import idle_checks, turn_metrics
from ..message import MessageEnvelope, MessageType, channel_address, parse_channel_id
from ..ops import TurnOrigin
from ..turn_metrics import TurnDelivery
from . import background_turn, manager_budget
from .turn_token import TurnStopped

if TYPE_CHECKING:
    from .manager import ManagerAgent
    from .turn_token import TurnToken

logger = logging.getLogger(__name__)
IDLE_TURN_SECONDS = 90
_post: idle_checks.PostFn | None = None
_INTERRUPTED = ("[Looper] Before this message, your idle check was interrupted so you could answer. "
                "Nothing was sent from it.")


def configure(post: idle_checks.PostFn) -> None:
    global _post
    _post = post


def _context(agent: ManagerAgent, check: dict[str, Any]) -> MessageEnvelope:
    parsed = parse_channel_id(check["channel_id"])
    if parsed is None:
        raise ValueError("idle check has an invalid channel id")
    return MessageEnvelope.create(source=channel_address(parsed.channel_type, check["channel_id"]),
        target=agent.agent_address, message_type=MessageType.IDLE_CHECK_DUE,
        payload={"text": "", "channel_context": {"chat_id": check["chat"], "chat_type": "p2p", "sender_id": check["chat"]}},
        correlation_id=f"idle-{check['id']}", metadata={"origin_channel_id": check["channel_id"]})


def stop_tokens(agent: ManagerAgent, envelope: MessageEnvelope, found: list[TurnToken]) -> list[TurnToken]:
    """Lark's send target is an open_id; /stop arrives with the actual DM's chat_id."""
    from .manager import _message_origin

    origin = _message_origin(envelope)
    token = agent._sessions["main"].running_token
    turn = token.ops_turn if token is not None else None
    if (token is not None and token.live and token not in found and turn is not None
            and turn.origin is TurnOrigin.IDLE_CHECK and not origin["group"]
            and origin["channel_id"] == turn.channel_id and origin["sender"] == turn.sender):
        return [token]
    return []


def _record_id(state: dict[str, Any]) -> int | None:
    prepared = state.get("prepared")
    recorder = prepared.run.recorder if prepared is not None else None
    return state.get("record_id") or (recorder.record_id if recorder is not None else None)


@manager_budget.after_reply
async def handle(agent: ManagerAgent, envelope: MessageEnvelope) -> None:
    from .manager import NoReply, _CompletionNotTaken

    check_id = envelope.payload.get("check_id")
    if type(check_id) is not int or agent.agent_id != idle_checks.MANAGER_ID:
        return
    check = await store.get(check_id)
    if check is None or check["manager_id"] != agent.agent_id or check["state"] != "running":
        return
    if agent._turned_off:
        await idle_checks.settle(check_id, "failed", reason="manager_off")
        return
    state: dict[str, Any] = {}
    delivery = TurnDelivery.REPLY

    async def fallback(reason: str) -> None:
        await idle_checks.settle(check_id, "failed", reason=reason, record_id=_record_id(state))

    try:
        result = await background_turn.run(agent, origin=TurnOrigin.IDLE_CHECK,
            message=idle_checks.render_message(check["items"], now=datetime.now(timezone.utc)),
            deadline_seconds=IDLE_TURN_SECONDS, fallback=fallback, metadata={"idle_check": {"check_id": check_id}},
            target=_context(agent, check), state=state)
        if result.timed_out:
            delivery = TurnDelivery.FAILED
            return
        try:
            reply = result.value()
        except TurnStopped:
            delivery = TurnDelivery.STOPPED
            await idle_checks.settle(check_id, "preempted", record_id=_record_id(state))
            token = state.get("token")
            if token is not None and not token.preemption_noted:
                session = agent._session_now()
                session.stopped_notes = [*session.stopped_notes, _INTERRUPTED][-3:]
                token.preemption_noted = True
            return
        except _CompletionNotTaken:
            delivery = TurnDelivery.FAILED
            await fallback("manager_off")
            return
        except NoReply:
            reply = ""  # the reply parser owns empty/INVALID semantics
        except Exception:
            logger.exception("idle check %s: the Manager turn failed", check_id)
            delivery = TurnDelivery.FAILED
            await fallback("turn_failed")
            return
        turn = state.get("turn")
        if turn is not None and turn.refused:
            delivery = TurnDelivery.FAILED
            await fallback("turn_failed")
            return
        kind = idle_checks.parse_reply(reply)
        if kind is idle_checks.IdleReply.MESSAGE and _post is None:
            delivery = TurnDelivery.FAILED
            await fallback("delivery_failed")
            return
        outcome = await idle_checks.deliver_result(check, reply, post=_post, record_id=_record_id(state))
        if outcome == "failed":
            delivery = TurnDelivery.FAILED
    finally:
        token, record_id = state.get("token"), _record_id(state)
        if token is not None and token.times is not None and record_id is not None:
            writing = turn_metrics.background(llm_records.add_metadata(record_id, {
                "turn_metrics": token.times.to_metadata(delivery, TurnOrigin.IDLE_CHECK, False)}))
            if writing is not None and manager_budget.holds_lock(agent._session_now(), token):
                await writing
