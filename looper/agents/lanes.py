"""Typed Manager priorities and stopping a background turn without settling its wake."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from .message import MessageEnvelope, MessageType
from .ops import TurnOrigin
from .runtimes.cancel import StopCleanup

if TYPE_CHECKING:
    from .ops.service import ManagerOps
    from .tiers.manager import ManagerAgent
    from .tiers.manager_session import ManagerSession
    from .tiers.turn_token import TurnToken

logger = logging.getLogger(__name__)
_warning_day: date | None = None
_daily_preemptions = 0


class Lane(IntEnum):
    INTERACTIVE = 0
    USER_TASK = 1
    RECOVERY = 2
    BACKGROUND = 3


def manager_lane(origin: TurnOrigin) -> Lane | None:
    return {TurnOrigin.USER: Lane.INTERACTIVE, TurnOrigin.COMPLETION: Lane.BACKGROUND,
            TurnOrigin.IDLE_CHECK: Lane.BACKGROUND,
            TurnOrigin.OWN: None}[origin]


class Arrival(StrEnum):
    RUN = "run"
    MERGE = "merge"
    QUEUE = "queue"
    PREEMPT = "preempt"


def classify_arrival(running_origin: TurnOrigin | None, can_interrupt: bool, mid_turn_merge: bool) -> Arrival:
    if running_origin is None or manager_lane(running_origin) is None:
        return Arrival.RUN
    if manager_lane(running_origin) is Lane.INTERACTIVE:
        return Arrival.MERGE if mid_turn_merge else Arrival.QUEUE
    return Arrival.PREEMPT if can_interrupt else Arrival.QUEUE


def prepare_background(session: ManagerSession, token: TurnToken) -> None:
    """Request an after-init process checkpoint only for enabled background turns."""
    from . import lanes_settings

    if lanes_settings.enabled() and token.ops_turn is not None \
            and manager_lane(token.ops_turn.origin) is Lane.BACKGROUND:
        token.cancel.cleanup = StopCleanup.TRACK


def preempt(session: ManagerSession, *, ops: ManagerOps | None, reason: str = "preempt") -> bool:
    """Stop the target's live background turn. Its worker still owes fallback and wake acknowledgement."""
    token = session.running_token
    if (token is None or not token.live or token.cancel.requested or token.ops_turn is None
            or manager_lane(token.ops_turn.origin) is not Lane.BACKGROUND or not session.claude.can_interrupt):
        return False
    # The cancel callback may start the interrupt immediately: require cleanup before requesting it.
    token.cancel.cleanup = StopCleanup.REQUIRED
    if not token.request_stop(reason):
        return False
    if ops is not None:
        ops.stop_turn(token.ops_turn)
    _note_preemption()
    return True


async def route_arrival(agent: ManagerAgent, session: ManagerSession, envelope: MessageEnvelope) -> bool:
    """Classify an arriving user message; only an accepted merge consumes it here."""
    if envelope.message_type is not MessageType.USER_MESSAGE:
        return False
    token = session.running_token
    origin = token.ops_turn.origin if token is not None and token.live and token.ops_turn is not None else None
    merged = await agent._mid_turn.merge(session, envelope) if origin is TurnOrigin.USER else False
    arrival = classify_arrival(origin, session.claude.can_interrupt, merged)
    if arrival is Arrival.PREEMPT and preempt(session, ops=agent.ops):
        token.preempted_by = envelope.message_id
    await agent._router.void_idle_checks(session)
    return arrival is Arrival.MERGE


def completion_metadata(token: TurnToken) -> dict:
    if token.cancel.cleanup is StopCleanup.NONE:
        return {}
    return {"lane": {"lane": "background", "preempted_by": token.preempted_by}}


def completion_note(session: ManagerSession, token: TurnToken | None, count: int) -> None:
    """Tell the next user turn once, after the preempted completion's notices have been written."""
    if token is None or token.preempted_by is None or token.preemption_noted:
        return
    note = (f"[Looper] Before this message, your report on {count} ended task(s) was interrupted so you could "
            "answer. Looper sent their notices itself; do not report them again unless asked.")
    session.stopped_notes = [*session.stopped_notes, note][-3:]
    token.preemption_noted = True


def _note_preemption() -> None:
    global _warning_day, _daily_preemptions

    today = datetime.now(timezone.utc).date()
    if today != _warning_day:
        _warning_day, _daily_preemptions = today, 0
    _daily_preemptions += 1
    if _daily_preemptions == 21:
        logger.warning("Manager background preemptions exceeded 20 today; background work may be delayed")
