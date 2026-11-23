"""Shared read-only background turns: preparation, deadlines and bounded late cleanup."""
from __future__ import annotations

import asyncio
import logging
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ...db import connection as db_connection, llm_records as db_llm_records, manager_events as db_manager_events
from .. import chat_settings, completion_events, lanes, lanes_settings, turn_metrics
from .. import delivery_guard_manager as delivery_guard
from ..channels.base import PostOutcome
from ..message import AgentAddress, MessageEnvelope, MessageType, channel_address, parse_channel_id
from ..ops import Turn, TurnOrigin
from ..supervisor import cancel_and_wait
from ..turn_metrics import TurnDelivery, TurnTimes
from . import manager_budget
from .turn_token import TurnPhase, TurnStopped, TurnToken

if TYPE_CHECKING:
    from .manager import ManagerAgent

logger = logging.getLogger(__name__)
Fallback = Callable[[str], Awaitable[None]]
Message = str | Callable[[TurnToken, MessageEnvelope], Awaitable[str]]
LateEnd = Callable[[asyncio.Task[str]], Awaitable[None]]


@dataclass(frozen=True)
class Result:
    timed_out: bool
    task: asyncio.Task[str]

    def value(self) -> str:
        return self.task.result()


async def run(agent: ManagerAgent, *, origin: TurnOrigin, deadline_seconds: float, fallback: Fallback,
              state: dict[str, Any], message: Message = "", metadata: dict[str, Any] | None = None,
              target: MessageEnvelope | None = None, work: Awaitable[str] | None = None,
              late: LateEnd | None = None) -> Result:
    """One background entry. Fallback is committed before bounding the late turn's cleanup.

    ``work``/``late`` preserve the Manager's completion hooks; new origins use the common implementation.
    The caller owns reply parsing, delivery and any failure policy after a completed task.
    """
    if work is None:
        if target is None:
            raise ValueError("a background turn needs a target chat")
        work = turn(agent, origin_kind=origin, message=message, metadata=metadata or {}, target=target, state=state)
    task = asyncio.create_task(work)
    try:
        done, _ = await asyncio.wait({task}, timeout=deadline_seconds)
    except asyncio.CancelledError:
        await cancel_and_wait(task)
        raise
    if not done:
        await fallback("turn_timeout")
        if late is not None:
            await late(task)
        else:
            await end_late(agent, task, target.correlation_id, state, origin=origin,
                           stop_reason=f"{origin.value}_timeout")
    return Result(not done, task)


async def turn(self: ManagerAgent, *, origin_kind: TurnOrigin, message: Message, metadata: dict[str, Any],
               target: MessageEnvelope, state: dict[str, Any]) -> str:
    from .manager import Ask, Prepared, _CompletionNotTaken, _chat_record, _message_origin

    context = target
    origin = _message_origin(context)
    try:
        state["chat_record"] = _chat_record(context, context.correlation_id)
    except ValueError:
        state["chat_record"] = {}
    token = self._turns.receive(context, channel=origin["channel"] or "", chat=origin["chat"] or "",
                                channel_id=origin["channel_id"])
    if chat_settings.metrics_enabled():
        token.times = TurnTimes(received_at=datetime.now(timezone.utc))
    token.envelope = None  # no status line: the chat sees only the reply
    turn = Turn(channel=origin["channel"], chat=origin["chat"], channel_id=origin["channel_id"],
                sender=origin["sender"], envelope=context, origin=origin_kind,
                require_match=self._claude.shares_bridge)
    token.ops_turn = turn
    lanes.prepare_background(self._session_now(), token)
    delivery_guard.initialize(turn)
    state["token"], state["turn"] = token, turn
    prepared: Prepared | None = None
    try:
        if lanes_settings.enabled():
            self._running_token = token  # arrivals can preempt even while task facts are loading
        token.advance(TurnPhase.PREPARING)
        text = await message(token, context) if callable(message) else message
        await token.step("turn_lock", self._turn_lock.acquire())
        self._lock_owner = token
        self._running_token = token
        if self.ops is not None:
            self.ops.begin_turn(turn, binding=token.binding, session_key=self._session_now().key)
        prepared = await self._prepare_turn(token, context, ask=Ask(text, "", turn))
        state["prepared"] = prepared
        if prepared.action is not None:  # turned off since: it does not act as the Manager
            raise _CompletionNotTaken()
        if origin_kind is TurnOrigin.COMPLETION:
            return await manager_budget.completion_reply(self, token, prepared)
        return await self._run_prepared(token, prepared)
    finally:
        if self._running_token is token:  # (a turn let go of past its deadline: another may run by now)
            self._running_token = None
        turn_metrics.note_lock_end(self._session_now(), token, origin_kind)
        if self._lock_owner is token and not manager_budget.holds_lock(self._session_now(), token):
            self._lock_owner = None
            self._turn_lock.release()
        if self.ops is not None:
            self.ops.end_turn(turn)
        token.settle(token.outcome)
        recorder = prepared.run.recorder if prepared is not None else None
        if recorder is not None and recorder.record_id is not None:
            state["record_id"] = recorder.record_id
            try:
                await recorder.annotate({**metadata, **lanes.completion_metadata(token)})
            except Exception:
                logger.warning("%s: could not mark run %s as a %s turn", self._log,
                               recorder.record_id, origin_kind.value, exc_info=True)


async def end_late(self: ManagerAgent, run: asyncio.Task[str], label: object, state: dict[str, Any], *,
                   origin: TurnOrigin, stop_reason: str) -> None:
    """End a completion turn past its deadline (its notices are out) within ``END_SECONDS`` twice, so the
    Manager's next message is not held behind it: first stopped as a stop ends a turn (a runtime run per turn
    ends its process tree), then cancelled. A cancelled turn may have left its runtime mid-turn, so that runtime
    is stopped and the next message starts a fresh one. A turn that ends even then is left to end on its own:
    the Manager lets go of it (its lock, its binding) and answers on a fresh runtime; it only writes its record."""
    token, turn = state.get("token"), state.get("turn")
    if token is not None and token.request_stop(stop_reason) and turn is not None \
            and self.ops is not None:
        self.ops.stop_turn(turn)
    cancelled = False
    for _ in range(2):
        try:
            done, _ = await asyncio.wait({run}, timeout=completion_events.END_SECONDS)
        except asyncio.CancelledError:
            run.cancel()
            raise
        if done:
            break
        cancelled = True
        run.cancel()
    if run.done():
        if not run.cancelled() and run.exception() is not None:
            logger.info("%s: the completion turn %s ended after its deadline (%s)", self._log, label,
                        type(run.exception()).__name__)
        if cancelled:
            try:
                await asyncio.wait_for(self._claude.stop(kill=True), completion_events.END_SECONDS)
            except Exception:
                logger.warning("%s: the runtime of the cancelled completion turn %s did not stop", self._log,
                               label, exc_info=True)
        return
    logger.error("%s: the completion turn %s did not end once cancelled; it is left to end on its own",
                 self._log, label)
    if token is not None:
        token.abandoned = True
    self._late_turns.add(run)
    run.add_done_callback(self._late_turns.discard)
    if self._lock_owner is token:
        turn_metrics.note_lock_end(self._session_now(), token, origin)
        self._lock_owner = None
        self._turn_lock.release()
    if self._running_token is token:
        self._running_token = None
    self._publish_binding(None)
    late, self._claude = self._claude, self._claude_runtime.resident_session(self._resident_label())
    self._process = None
    self._track_runtime_cleanup(late)


async def _tell_completion(self, rows: list[dict[str, Any]], batch: uuid.UUID) -> None:
    """Run the completion turn of the claimed events (``batch``) within ``TURN_SECONDS``: its reply, with the
    tasks' links code adds, goes to their chat (``delivered``); anything else sends their notices
    (``fallback``). At the deadline the notices go at once, and a runtime that can be interrupted is stopped
    the way ``/stop`` stops it; one that cannot runs to its end, and its reply only stays on its record."""
    from .manager import NoReply, OverCap, _CompletionNotTaken

    if rows[0].get("kind") == "triage":  # its own settling: no code notice, no task links
        from . import triage_background

        return await triage_background.tell(self, rows, batch)
    assert self._bus is not None
    started = time.monotonic()
    logger.info("completion turn started batch=%s manager=%s events=%d", batch, self.agent_id, len(rows))
    state: dict[str, Any] = {"rows": rows}
    async def fallback(reason: str) -> None:
        await self._fall_back_completion(batch, reason, state)

    result = await run(self, origin=TurnOrigin.COMPLETION, deadline_seconds=completion_events.TURN_SECONDS,
                       fallback=fallback, state=state,
                       work=self._completion_turn(rows, batch, state),
                       late=lambda task: self._end_late_completion(task, batch, state))
    reason: str | None = None
    delivery = TurnDelivery.REPLY
    rejected = False  # the channel refused the reply: its notices may go
    text = ""
    if result.timed_out:
        reason = "turn_timeout"
        delivery = TurnDelivery.FAILED
    else:
        try:
            text = result.value().strip()
        except TurnStopped:
            delivery = TurnDelivery.STOPPED
            reason = "turn_stopped"
        except _CompletionNotTaken:
            reason = "manager_off"
        except NoReply:
            delivery = TurnDelivery.EMPTY
            reason = "empty_reply"
        except OverCap:
            delivery = TurnDelivery.OVER_CAP
            reason = "turn_failed"
        except Exception as exc:
            logger.error("%s: the completion turn %s failed: %s", self._log, batch, type(exc).__name__)
            reason = "turn_failed"
        else:
            turn = state.get("turn")
            if turn is not None and turn.refused:
                reason = "turn_failed"
            elif not text:
                reason = "empty_reply"
        if reason is None:
            body = "\n\n".join(part for part in (
                text, completion_events.link_block(rows, base=self._config.dashboard_url)) if part)
            try:
                outcome, error = await completion_events.send_reply(self._bus, self.agent_address, rows, body,
                                                                    metadata=state.get("chat_record"))
            except completion_events.TargetChanged as exc:
                logger.warning("%s: the completion turn %s's reply is not sent: %s", self._log, batch, exc)
                reason = "target_changed"
            except Exception:  # before its events were ``sending``: nothing went
                logger.exception("%s: the completion turn %s's reply could not be sent", self._log, batch)
                reason = "send_failed"
            else:
                if outcome is PostOutcome.REJECTED:  # the channel did not send it: its notices may go
                    logger.warning("%s: the completion turn %s's reply was refused: %s", self._log, batch, error)
                    reason, rejected = "send_failed", True
                else:  # sent, or it may have been (an unknown post is final: nothing more is sent)
                    if outcome is not PostOutcome.SENT:
                        logger.warning("%s: the completion turn %s's reply may not have been sent: %s",
                                       self._log, batch, error)
                    # The record only is tried again; one never written stays ``sending`` until the service
                    # settles it ``uncertain``: the reply is not sent again.
                    await completion_events.record_reply(rows, outcome, error, record_id=state.get("record_id"))
        if reason is not None:
            await self._fall_back_completion(batch, reason, state, rejected=rejected)
    cost = None
    metrics_token = state.get("token")
    if metrics_token is not None and metrics_token.times is not None and state.get("record_id") is not None:
        if reason and delivery is TurnDelivery.REPLY:
            delivery = TurnDelivery.EMPTY if reason == "empty_reply" else TurnDelivery.FAILED
        writing = turn_metrics.background(db_llm_records.add_metadata(state["record_id"], {"turn_metrics":
            metrics_token.times.to_metadata(delivery, TurnOrigin.COMPLETION, False)}))
        if writing is not None and manager_budget.holds_lock(self._session_now(), metrics_token):
            await writing
    if state.get("record_id") is not None:
        try:
            cost = await db_connection.get_pool().fetchval("SELECT cost_usd FROM llm_records WHERE id = $1",
                                                           state["record_id"])
        except Exception:
            pass
    logger.info("completion turn ended batch=%s outcome=%s reason=%s seconds=%.1f cost_usd=%s", batch,
                "fallback" if reason else "delivered", reason, time.monotonic() - started, cost)
    if reason in db_manager_events.FAILING_REASONS:
        try:
            if await db_manager_events.failing():
                logger.warning("completion turns failing: the last %d batches all fell back to their notices",
                               db_manager_events.FAILING_BATCHES)
        except Exception:
            pass


def completion_context(row: dict[str, Any], manager: AgentAddress, batch: uuid.UUID) -> MessageEnvelope:
    """The envelope a completion turn runs on, as if it came from the chat its event names: what the turn's
    ``Channel:`` line, its chat's live turns (``/stop``) and the replies Claude writes on its own afterwards take
    their chat from. Its type is ``COMPLETION_DUE``: nothing treats it as the user's message."""
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    fallback = json.loads(row["fallback"]) if isinstance(row["fallback"], str) else row["fallback"]
    first = fallback[0] if fallback else {}
    if row["target_kind"] == "web":
        source = channel_address("web")
        context: dict[str, Any] = {"session_id": row["chat"]}
        metadata = {"origin_channel_id": first.get("origin_channel_id") or source.agent_id}
    else:
        parsed = parse_channel_id(row["channel_id"])
        source = channel_address(parsed.channel_type if parsed is not None else "lark", row["channel_id"])
        notice = first.get("notice") or {}
        # A Lark notice names the person; the chat it was asked in is the task's reply route.
        context = {"chat_id": payload.get("chat_id") or row["chat"], "chat_type": "p2p"}
        if notice.get("kind") == "user":
            context["sender_id"] = notice.get("chat")
        elif row.get("kind") == "triage":  # the operator's own direct chat: it runs on the main session
            context["sender_id"] = row["chat"]
        metadata = {"origin_channel_id": row["channel_id"]}
    return MessageEnvelope.create(source=source, target=manager, message_type=MessageType.COMPLETION_DUE,
                                  payload={"text": "", "channel_context": context},
                                  correlation_id=f"completion-{batch}", metadata=metadata)


async def completion_turn(self, rows: list[dict[str, Any]], batch: uuid.UUID, state: dict[str, Any]) -> str:
    from .manager import channel_line

    async def message(token: TurnToken, context: MessageEnvelope) -> str:
        more = await token.step("task_facts", completion_events.task_facts([row["task_id"] for row in rows]))
        return completion_events.completion_message(rows, channel_line=channel_line(context), more=more)

    return await turn(self, origin_kind=TurnOrigin.COMPLETION, message=message,
        metadata={"completion": {"batch": str(batch), "event_ids": [row["id"] for row in rows],
                                 "task_ids": [row["task_id"] for row in rows]}},
        target=completion_context(rows[0], self.agent_address, batch), state=state)

