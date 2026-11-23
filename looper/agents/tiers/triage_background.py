"""The Manager's triage turn on the shared background entry (event triage RFC, sections 5 and 6.1).

``dispatch`` claims a chat's pending triage events (never in a task-end batch) and queues them on the main session's
background lane. ``tell`` runs the turn through ``background_turn.run`` with ``TurnOrigin.TRIAGE`` and settles the
batch through ``agents.triage_turn``: a failed, timed-out, stopped or unreadable turn sends nothing and leaves its
events for the brief. Nothing runs unless the delivery layer is on and triage events were claimed.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...db import llm_records, manager_events as store
from .. import chat_settings, completion_events, lanes_settings, session_routing, triage_settings, triage_turn, turn_metrics
from ..channels.base import PostOutcome, PostReceipt
from ..message import MessageEnvelope, MessageType, channel_address, parse_channel_id
from ..ops import TurnOrigin
from ..turn_metrics import TurnDelivery
from . import background_turn, manager_budget
from .turn_token import TurnStopped

if TYPE_CHECKING:
    from .manager import ManagerAgent

logger = logging.getLogger(__name__)
_post: Callable[..., Awaitable[PostReceipt]] | None = None


class _NothingToTell(Exception):
    """None of the batch's events is left to tell."""


_INTERRUPTED = ("[Looper] Before this message, your triage report was interrupted so you could answer. Nothing was "
                "sent from it; its events will be in the morning brief.")


def configure(post: Callable[..., Awaitable[PostReceipt]]) -> None:
    global _post
    _post = post


async def dispatch(agent: ManagerAgent, envelope: MessageEnvelope, target: completion_events.Target) -> None:
    """Claim the target's pending triage events and queue them as their own batch (the task-end claim is separate)."""
    if agent._turned_off or not chat_settings.delivery_enabled():
        return
    try:
        routed = session_routing.routes(agent.agent_id)
        operator = await agent._router.completion_operator(target.channel_id) if routed else None
        batch = uuid.uuid4()
        rows = await store.claim(*target.key, batch, kind="triage")
        if rows and not await triage_settings.enabled():
            await store.fall_back(reason="off", batch=batch)
            return
    except Exception:
        logger.exception("%s: could not take the triage events of %s; they stay pending", agent._log, target.key)
        return
    if rows:
        if routed or lanes_settings.enabled():
            agent._router.dispatch_completion(envelope, rows, batch, operator)
        else:
            await agent._tell_completion(rows, batch)


def _context(agent: ManagerAgent, row: dict[str, Any], batch: uuid.UUID) -> MessageEnvelope:
    """The chat the turn runs in: the operator's own, so it lands on the main session."""
    return background_turn.completion_context(row, agent.agent_address, batch)


def _sender(agent: ManagerAgent, row: dict[str, Any]) -> triage_turn.SendFn:
    if row["target_kind"] == "web":
        async def send(text: str, key: str) -> PostReceipt:
            assert agent._bus is not None
            outcome, error = await completion_events._publish(agent._bus, MessageEnvelope.create(
                source=agent.agent_address, target=channel_address("web"), message_type=MessageType.AGENT_REPLY,
                payload={"text": text, "chat_session_id": row["chat"]}, correlation_id=key,
                metadata={"origin_channel_id": "web"}))
            return PostReceipt(outcome is PostOutcome.SENT, error=error, outcome=outcome)
        return send
    if _post is None or parse_channel_id(row["channel_id"]) is None:
        async def unavailable(text: str, key: str) -> PostReceipt:
            return PostReceipt(False, error="no channel to send through", outcome=PostOutcome.REJECTED)
        return unavailable
    return triage_turn.dm_sender(_post, row)


def _record_id(state: dict[str, Any]) -> int | None:
    prepared = state.get("prepared")
    recorder = prepared.run.recorder if prepared is not None else None
    return state.get("record_id") or (recorder.record_id if recorder is not None else None)


async def tell(agent: ManagerAgent, rows: list[dict[str, Any]], batch: uuid.UUID) -> None:
    """Run the triage turn of the claimed events and settle them. Never raises past its own logging."""
    from .manager import NoReply, _CompletionNotTaken

    started = time.monotonic()
    first, state = rows[0], {"rows": rows}
    send = _sender(agent, first)
    end, reply = triage_turn.TurnEnd.FAILED, None
    delivery = TurnDelivery.FAILED
    try:
        if not await triage_turn.admit(batch):
            return

        async def message(token: TurnToken, context: MessageEnvelope) -> str:
            # Loaded under the turn's token: a user message that arrives meanwhile stops the turn before any model call.
            items = await token.step("triage_events", triage_turn.load(rows))
            if not items:  # nothing left to tell: settled once, outside the stoppable read
                await store.skip_batch(rows[0]["batch"], reason=store.Reason.MANAGER_SILENT.value)
                raise _NothingToTell()
            return triage_turn.render(items)

        async def fallback(reason: str) -> None:  # the deadline passed: the late turn is ended after this
            await triage_turn.finish(rows, end=triage_turn.TurnEnd.TIMEOUT, send=send)

        result = await background_turn.run(
            agent, origin=TurnOrigin.TRIAGE, deadline_seconds=completion_events.TURN_SECONDS, fallback=fallback,
            state=state, message=message, target=_context(agent, first, batch),
            metadata={"triage": {"batch": str(batch), "event_ids": [row["id"] for row in rows]}})
        if result.timed_out:
            return
        try:
            reply = result.value().strip()
        except TurnStopped:
            end, delivery = triage_turn.TurnEnd.STOPPED, TurnDelivery.STOPPED
            token = state.get("token")
            if token is not None and not token.preemption_noted:
                session = agent._session_now()
                session.stopped_notes = [*session.stopped_notes, _INTERRUPTED][-3:]
                token.preemption_noted = True
        except _NothingToTell:
            end = None
        except _CompletionNotTaken:
            await store.fall_back(reason="manager_off", batch=batch, record_id=_record_id(state))
            end = None
        except NoReply:
            end, reply = triage_turn.TurnEnd.REPLIED, ""
        except Exception:
            logger.error("%s: the triage turn %s failed", agent._log, batch, exc_info=True)
        else:
            turn = state.get("turn")
            if turn is not None and turn.refused:
                pass  # the tools did not work, whatever the model says
            else:
                end, delivery = triage_turn.TurnEnd.REPLIED, TurnDelivery.REPLY
        if end is not None:
            settled = await triage_turn.finish(rows, end=end, reply=reply, send=send, record_id=_record_id(state))
            if end is triage_turn.TurnEnd.REPLIED and settled.state != "delivered":
                delivery = TurnDelivery.EMPTY if settled.reason == "empty_reply" else delivery
            logger.info("triage turn ended batch=%s state=%s reason=%s seconds=%.1f", batch, settled.state,
                        settled.reason, time.monotonic() - started)
    except Exception:
        logger.exception("%s: the triage turn %s could not be settled; restart recovers it", agent._log, batch)
    finally:
        token, record_id = state.get("token"), _record_id(state)
        if token is not None and token.times is not None and record_id is not None:
            writing = turn_metrics.background(llm_records.add_metadata(record_id, {
                "turn_metrics": token.times.to_metadata(delivery, TurnOrigin.TRIAGE, False)}))
            if writing is not None and manager_budget.holds_lock(agent._session_now(), token):
                await writing
