"""Bounded operator additions to a running Manager turn; no new model turn or tool binding.

The bus keeps each addition unacknowledged until the original turn has handed its
reply to every participating chat. A failed write leaves the normal queue in charge.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from . import (
    attention, chat_attachments, chat_commands, chat_settings, gate_commands, gate_routing, intent_routing,
    operator_identity, session_routing, turn_metrics,
)
from .message import MessageEnvelope, MessageType, is_lark
from .ops import TurnOrigin
from .runtimes import Capability, MidTurnInput
from .tiers.turn_token import TurnPhase, TurnToken

if TYPE_CHECKING:
    from .tiers.manager import ManagerAgent, Prepared, ReplyOutcome
    from .tiers.manager_session import ManagerSession

logger = logging.getLogger(__name__)
MAX_MERGED = 3
MERGED_REPLY = "（已并入上一条的回复）"


def enabled(manager_id: str) -> bool:
    from .managers_api import DEFAULT_MANAGER_ID

    return manager_id == DEFAULT_MANAGER_ID and chat_settings.mid_turn_input_enabled()


class MergedKind(str, Enum):
    OPERATOR_MESSAGE = "operator_message"


@dataclass
class MergedInput:
    envelope: MessageEnvelope
    token: TurnToken
    chat: str
    input_text: str
    kind: MergedKind = MergedKind.OPERATOR_MESSAGE
    delivered: bool = False

    def metadata(self, *, delivery: turn_metrics.TurnDelivery, started_at: datetime) -> dict[str, Any]:
        result = {"message_id": self.envelope.message_id, "chat": self.chat,
                  "received_at": self.envelope.timestamp, "kind": self.kind.value}
        times = self.token.times
        if times is not None:
            from .tiers.manager import _message_origin

            origin = _message_origin(self.envelope)
            if is_lark(origin["channel"]) or origin["channel"] == "telegram":
                context = self.envelope.payload.get("channel_context") or {}
                key = origin["channel_id"], str(context.get("message_id") or "")
                known = turn_metrics.ack_board.contains(*key)
                times.ack_at = turn_metrics.ack_board.take(*key)
                times.ack = (turn_metrics.AckKind.REACTION if times.ack_at else
                             turn_metrics.AckKind.NONE if known else turn_metrics.AckKind.PENDING)
            # Its first reply waits for the user turn it was merged into.
            times.queued_behind = turn_metrics.QueuedBehind.USER
            result["turn_metrics"] = times.to_metadata(delivery, TurnOrigin.USER, times.received_at < started_at)
        return result


@dataclass
class RunningInput:
    token: TurnToken
    prepared: Prepared
    session: ManagerSession
    accepting: bool = True
    inputs: list[MergedInput] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MidTurnInputs:
    def __init__(self, agent: ManagerAgent) -> None:
        self.agent = agent
        self._turns: dict[str, RunningInput] = {}
        self._parents: dict[str, str] = {}

    def open(self, token: TurnToken, prepared: Prepared, text: str) -> str:
        accepts = (enabled(self.agent.agent_id) and prepared.turn is not None
                   and prepared.turn.origin is TurnOrigin.USER
                   and Capability.MID_TURN_INPUT in prepared.launch.runtime.capabilities
                   and isinstance(prepared.session.claude, MidTurnInput))
        previous = self._turns.get(token.message_id)
        if previous is not None:  # a failed resume retries inputs already accepted, even after switching off
            previous.prepared, previous.session, previous.accepting = prepared, prepared.session, accepts
            if previous.inputs and not prepared.delivery_followup:
                text += "\n\n" + "\n\n".join(item.input_text for item in previous.inputs)
        elif accepts:
            self._turns[token.message_id] = RunningInput(token, prepared, prepared.session)
        return text

    async def close(self, token: TurnToken) -> None:
        if (running := self._turns.get(token.message_id)) is not None:
            running.accepting = False  # before awaiting a write already in flight
            async with running.lock:
                pass

    def parent(self, message_id: str) -> str:
        return self._parents.get(message_id, message_id)

    def stop(self, token: TurnToken) -> None:
        """Withdraw every addition too: a restart must not resurrect a stopped request."""
        for message_id, parent in self._parents.items():
            if parent == token.message_id:
                if (addition := self.agent._turns.find(message_id)) is not None:
                    addition.request_stop(token.stop.via if token.stop else "chat")
                if self.agent._bus is not None:
                    self.agent._bus.settle(message_id)

    async def _operator(self, envelope: MessageEnvelope) -> bool:
        from .tiers.manager import _message_client, _message_origin

        origin = _message_origin(envelope)
        if origin["channel"] == "web":
            return _message_client(envelope)[0] is None
        if not (is_lark(origin["channel"]) or origin["channel"] == "telegram"):
            return False
        operator = await operator_identity.operator_sender_id(origin["channel_id"])
        return bool(operator and origin["sender"] == operator)

    @staticmethod
    def _chat(envelope: MessageEnvelope) -> tuple[str | None, str | None, str | None]:
        from .tiers.manager import _message_origin

        origin = _message_origin(envelope)
        return origin["channel"], origin["channel_id"], origin["chat"]

    def _pending(self, envelope: MessageEnvelope) -> bool:
        return self.agent.ops is not None and self.agent.ops.has_confirmation(self._chat(envelope))

    async def merge(self, session: ManagerSession, envelope: MessageEnvelope) -> bool:
        try:
            return await self._merge(session, envelope)
        except Exception:
            logger.exception("mid-turn admission failed for %s; keeping message queued", envelope.message_id)
            return False

    async def _merge(self, session: ManagerSession, envelope: MessageEnvelope) -> bool:
        if not enabled(self.agent.agent_id) or envelope.message_type is not MessageType.USER_MESSAGE:
            return False
        token = session.running_token
        running = self._turns.get(token.message_id) if token is not None else None
        if running is None or not running.accepting or self.agent._router.queued(session):
            return False
        merged_token = self.agent._turns.find(envelope.message_id)
        if merged_token is not None and merged_token.cancel.requested:
            return False
        text = envelope.payload.get("text")
        from .tiers.manager import is_new_session_command, is_stop_command, parse_profile_command

        # Use the same exact command boundaries as normal message handling.
        # References are queued even when the quoted gate/intent has since closed.
        if (not isinstance(text, str) or not text.strip()
                or is_new_session_command(text) or is_stop_command(text) or chat_commands.is_help(text)
                or parse_profile_command(text) is not None or gate_commands.is_command(text)
                or attention.is_command(text)
                or envelope.payload.get("attachments") or envelope.payload.get("quote_ref")
                or gate_routing.references(envelope) is not None or intent_routing.is_reply(text)):
            return False
        original = running.prepared.envelope
        if not session_routing.routes(self.agent.agent_id) and self._chat(envelope) != self._chat(original):
            return False
        try:
            async with asyncio.timeout(5):
                if not await self._operator(original) or not await self._operator(envelope):
                    return False
        except Exception:
            logger.warning("mid-turn identity unavailable; keeping message queued")
            return False
        async with running.lock:
            # Recheck after identity I/O: result/stop/confirmation may have arrived meanwhile.
            if (not running.accepting or token.phase is not TurnPhase.RUNNING or token.cancel.requested
                    or running.prepared.turn.origin is not TurnOrigin.USER
                    or len(running.inputs) >= MAX_MERGED or self._pending(original) or self._pending(envelope)
                    or (merged_token is not None and merged_token.cancel.requested)):
                return False
            from .tiers.manager import channel_line

            line = channel_line(envelope)
            message = (f"[Looper] Another message from the user arrived while you were working ({line}). "
                       f"Answer it in this same reply.\n{text}")
            merged_token = self.agent._turns.find(envelope.message_id) or self.agent._register_turn(envelope)
            # A stop from the addition's chat must reach the original even while stdin drains.
            self._parents[envelope.message_id] = token.message_id
            written = False
            try:
                written = await session.claude.write_mid_turn(message)
                if not written:
                    return False
            except (OSError, TimeoutError):
                logger.warning("mid-turn input write failed; keeping message queued")
                return False
            finally:
                if not written:
                    self._parents.pop(envelope.message_id, None)
                    if token.cancel.requested:
                        merged_token.request_stop(token.stop.via if token.stop else "chat")
            item = MergedInput(envelope, merged_token, self._chat(envelope)[2] or "", message)
            running.inputs.append(item)
            # After a successful write, never queue a duplicate because a chat/metrics write failed.
            try:
                async with asyncio.timeout(10):
                    await self.agent._persist_user(envelope, text)
                    await self.agent._send_status(envelope, "已并入当前回复", phase=True)
            except Exception:
                logger.warning("could not persist/show merged input %s", envelope.message_id, exc_info=True)
            if chat_settings.metrics_enabled():
                turn_metrics.background(self.agent._mark_metrics_replied(envelope))
            return True

    async def reply(self, original: MessageEnvelope, text: str, *, error: str | None,
                    outcome: ReplyOutcome) -> None:
        from .tiers.manager import ReplyOutcome

        running = self._turns.get(original.message_id)
        if running is None:
            return
        paths = [item["path"] for item in original.payload.get("attachments", ())
                 if isinstance(item, dict) and isinstance(item.get("path"), str)]
        relayed_text = chat_attachments.redact(text, self.agent._runtime_root, paths=paths)
        for item in running.inputs:
            if item.delivered:
                continue
            same = self._chat(original) == self._chat(item.envelope)
            # A stopped addition always gets the stopped reply; a web stream always needs its terminal event.
            if not same or self._chat(item.envelope)[0] == "web" or outcome is ReplyOutcome.STOPPED:
                await self.agent._send_reply(item.envelope,
                    MERGED_REPLY if same and outcome is ReplyOutcome.ANSWERED else relayed_text,
                    error=error, outcome=outcome)
            item.delivered = True
            if item.token.times is not None and text and error is None:
                item.token.times.first_reply_at = datetime.now(timezone.utc)

    async def finish(self, token: TurnToken) -> None:
        running = self._turns.pop(token.message_id, None)
        if running is None:
            return
        try:
            if running.inputs:
                async with asyncio.timeout(10):
                    await running.prepared.run.recorder.annotate({"merged": [item.metadata(
                        delivery=token.metrics_delivery, started_at=self.agent._metrics_started_at)
                        for item in running.inputs]})
        except Exception:
            logger.warning("could not record merged inputs for %s", token.message_id, exc_info=True)
        finally:
            for item in running.inputs:
                self._parents.pop(item.envelope.message_id, None)
                item.token.settle(token.outcome)
                self.agent._persisted.discard(item.envelope.message_id)
                if item.delivered and self.agent._bus is not None:
                    self.agent._bus.settle_handled(item.envelope)
