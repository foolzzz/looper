"""Whether a proactive message goes out now, waits for the digest, or is dropped (``deliver``): a pure function of
typed inputs, so every rule has a table-driven test and every send point decides the same way.

A proactive message is one Looper sends without being asked in that moment: a gate reminder, a task notice, a
subscription's push, an intent, a completion turn, a reminder the user set, the morning brief. Before one goes
out, its send point calls ``deliver`` with what it is (``Delivery``), the operator's policy (``DeliveryPolicy``)
and the chat's state now (``DeliveryState``), and gets a ``Decision``: an outcome and the one rule that settled
it, so "why didn't you tell me" has an answer. The rules, first match wins:

1. a source that said it has nothing to tell (``silent``): dropped;
2. a gate that holds back a task, or a reminder the user set themselves: sent, never throttled, not counted;
3. outside the source's own active hours (a schedule's or subscription's throttle field): into the digest;
4. the scheduled morning brief: quiet hours apply, but it bypasses the daily cap and is not counted;
5. the user wrote in this chat in the last ``recent_reply`` (5 minutes): sent, not counted;
6. inside the quiet hours (22:00–08:00 in the user's timezone): into the digest;
7. a direct chat that already had ``dm_daily_cap`` (5) counted messages today: into the digest;
8. otherwise sent, and counted when the chat is direct.

The digest a deferred message goes into (``Decision.digest_date``) is named by the local date pushes resume on:
the end of the next quiet hours (the next local day when there are none). With the system setting
``delivery.enabled`` off (the default) a send point that switch controls does not call ``deliver``. The morning
brief has its own switch: it calls ``deliver`` when it runs, and ``decide`` records the decision only while
``delivery.enabled`` is on. On, a send point calls ``decide``, which reads the policy and the chat's count,
decides and records the decision in ``proactive_deliveries`` once per message.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from .turn_metrics import ProactiveSource

logger = logging.getLogger(__name__)

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(8, 0)
DEFAULT_DM_DAILY_CAP = 5
DEFAULT_RECENT_REPLY = timedelta(minutes=5)


# What a proactive message is, its send point: the source the proactive-message metrics record it by (one enum for
# ``proactive_deliveries.source``). ``morning_brief`` is the digest itself.
DeliveryKind = ProactiveSource


class Urgency(StrEnum):
    """Why a message may not wait. ``BLOCKING`` and ``USER_SET`` are never throttled."""

    NORMAL = "normal"
    DIGEST = "digest"  # a subscription's digest: subject to the ordinary quiet hours and daily cap
    BLOCKING = "blocking"  # a gate waits for the user and holds back its task
    USER_SET = "user_set"  # a reminder the user asked for themselves


EXEMPT = frozenset({Urgency.BLOCKING, Urgency.USER_SET})


class ChatKind(StrEnum):
    """Where it goes: the daily cap counts direct chats (and the dashboard chat) only."""

    DIRECT = "direct"
    GROUP = "group"


class Outcome(StrEnum):
    SEND = "send"
    DIGEST = "digest"
    DROP = "drop"


class Reason(StrEnum):
    """The rule that settled a decision (the module docstring's order)."""

    SILENT = "silent"
    URGENT = "urgent"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
    RECENT_REPLY = "recent_reply"
    QUIET_HOURS = "quiet_hours"
    DAILY_CAP = "daily_cap"
    WITHIN_LIMITS = "within_limits"


@dataclass(frozen=True)
class DailyWindow:
    """A span of local wall time each day, ``start`` included and ``end`` not; it crosses midnight when ``start`` is
    later than ``end`` (22:00–08:00)."""

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError(f"a daily window needs different start and end, not {self.start:%H:%M} twice")
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("a daily window is local wall time, without a timezone")

    def contains(self, moment: time) -> bool:
        moment = moment.replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    @classmethod
    def parse(cls, text: str) -> DailyWindow:
        """``HH:MM-HH:MM``; ``ValueError`` names what is wrong."""
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError(f"a daily window is HH:MM-HH:MM, not {text!r}")
        try:
            start, end = (time.fromisoformat(part.strip()) for part in parts)
        except ValueError:
            raise ValueError(f"a daily window is HH:MM-HH:MM, not {text!r}") from None
        return cls(start, end)

    def text(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class SourceThrottle:
    """The throttle fields of the schedule or subscription a message comes from that decide its delivery: the hours
    it is in effect, in its own timezone (None: all day)."""

    active_hours: DailyWindow | None
    timezone: ZoneInfo


@dataclass(frozen=True)
class DeliveryPolicy:
    """The operator's rules: the quiet hours (None: none) in ``timezone`` (``user.timezone``), how many counted
    proactive messages a direct chat gets a day, and how long after the user wrote a message passes anyway."""

    timezone: tzinfo  # an IANA zone, or a fixed offset when the server has no zone name
    quiet_hours: DailyWindow | None = DailyWindow(DEFAULT_QUIET_START, DEFAULT_QUIET_END)
    dm_daily_cap: int = DEFAULT_DM_DAILY_CAP
    recent_reply: timedelta = DEFAULT_RECENT_REPLY

    def __post_init__(self) -> None:
        if isinstance(self.dm_daily_cap, bool) or not isinstance(self.dm_daily_cap, int) or self.dm_daily_cap < 0:
            raise ValueError(f"dm_daily_cap must be a whole number from 0, not {self.dm_daily_cap!r}")
        if self.recent_reply < timedelta(0):
            raise ValueError("recent_reply cannot be negative")


@dataclass(frozen=True)
class Delivery:
    """The message about to go out: its kind, its urgency, the chat's kind, the throttle fields of its source (None
    for one that is not a schedule's or subscription's), and whether the source said it has nothing to tell."""

    kind: DeliveryKind
    urgency: Urgency
    chat: ChatKind
    source: SourceThrottle | None = None
    silent: bool = False


@dataclass(frozen=True)
class DeliveryState:
    """The chat now: the time (aware), the counted proactive messages it had on today's local date (the policy's
    timezone), and when the user last wrote in it."""

    now: datetime
    sent_today: int = 0
    last_user_message_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("now must carry a timezone")
        if self.last_user_message_at is not None and self.last_user_message_at.tzinfo is None:
            raise ValueError("last_user_message_at must carry a timezone")
        if self.sent_today < 0:
            raise ValueError("sent_today cannot be negative")


@dataclass(frozen=True)
class Decision:
    """What to do with the message and why; ``counted``: it counts toward the chat's daily cap; ``digest_date``: the
    digest it goes into (DIGEST only)."""

    outcome: Outcome
    reason: Reason
    counted: bool = False
    digest_date: date | None = None


def digest_date(policy: DeliveryPolicy, now: datetime) -> date:
    """The digest a message deferred at ``now`` goes into: the local date the next quiet hours end on (with none,
    the next local day)."""
    local = now.astimezone(policy.timezone)
    if policy.quiet_hours is None:
        return local.date() + timedelta(days=1)
    end = policy.quiet_hours.end
    return local.date() if local.time().replace(tzinfo=None) < end else local.date() + timedelta(days=1)


def deliver(delivery: Delivery, policy: DeliveryPolicy, state: DeliveryState) -> Decision:
    """Decide what happens to ``delivery`` now (the module docstring has the rules)."""
    if delivery.silent:
        return Decision(Outcome.DROP, Reason.SILENT)
    if delivery.urgency in EXEMPT and delivery.kind is not DeliveryKind.MORNING_BRIEF:
        return Decision(Outcome.SEND, Reason.URGENT)
    source = delivery.source
    if source is not None and source.active_hours is not None and \
            not source.active_hours.contains(state.now.astimezone(source.timezone).time()):
        return _later(Reason.OUTSIDE_ACTIVE_HOURS, policy, state)
    # A scheduled brief always respects quiet hours, including after a recent reply. As the digest
    # itself, it bypasses the cap and never consumes a counted proactive message.
    if delivery.kind is DeliveryKind.MORNING_BRIEF:
        if policy.quiet_hours is not None and policy.quiet_hours.contains(state.now.astimezone(policy.timezone).time()):
            return _later(Reason.QUIET_HOURS, policy, state)
        return Decision(Outcome.SEND, Reason.WITHIN_LIMITS)
    spoke = state.last_user_message_at
    # In UTC: two times of one ZoneInfo subtract as wall clocks, an hour off across a DST change. A later one (clocks
    # apart) is still recent.
    if spoke is not None and state.now.astimezone(timezone.utc) - spoke.astimezone(timezone.utc) <= policy.recent_reply:
        return Decision(Outcome.SEND, Reason.RECENT_REPLY)
    if policy.quiet_hours is not None and policy.quiet_hours.contains(state.now.astimezone(policy.timezone).time()):
        return _later(Reason.QUIET_HOURS, policy, state)
    direct = delivery.chat is ChatKind.DIRECT
    if direct and state.sent_today >= policy.dm_daily_cap:
        return _later(Reason.DAILY_CAP, policy, state)
    return Decision(Outcome.SEND, Reason.WITHIN_LIMITS, counted=direct)


def _later(reason: Reason, policy: DeliveryPolicy, state: DeliveryState) -> Decision:
    return Decision(Outcome.DIGEST, reason, digest_date=digest_date(policy, state.now))


# --- What a send point calls: the policy and the chat's count read, the decision made and recorded once ---


def load_policy() -> DeliveryPolicy:
    """The operator's policy: the built-in rules in the user's timezone (``user.timezone``, kept by the turn context
    settings; else the server's own zone)."""
    from . import turn_context

    return DeliveryPolicy(timezone=turn_context.zone())


async def decide(delivery: Delivery, *, dedupe_key: str, channel_id: str, chat: str,
                 last_user_message_at: datetime | None, source_ref: str | None = None, manager_id: str | None = None,
                 task_id: str | None = None, step_id: str | None = None, schedule_id: str | None = None,
                 now: datetime | None = None) -> Decision:
    """Decide on one proactive message and record the decision in its ``proactive_deliveries`` row
    (``dedupe_key``: the send point's key for the message, the one its channel receipt is recorded under). A message
    already decided on (tried again) keeps its first decision; a row the metrics recorded before the layer was on
    gets one. The decisions on one chat are taken one at a time, so its daily cap holds across send points."""
    from ..db import connection, proactive_deliveries as db

    policy = load_policy()
    async with connection.get_pool().acquire() as conn, conn.transaction():
        await db.lock_chat(conn, channel_id, chat)
        if (row := await db.get(conn, dedupe_key)) is not None and row["decision"] is not None:
            return _decision(row)
        now = now or datetime.now(timezone.utc)
        local_date = now.astimezone(policy.timezone).date()
        state = DeliveryState(now, await db.counted_on(conn, channel_id, chat, local_date), last_user_message_at)
        decision = deliver(delivery, policy, state)
        row = await db.insert_decision(conn, {
            "dedupe_key": dedupe_key, "source": delivery.kind.value, "source_ref": source_ref,
            "manager_id": manager_id, "urgency": delivery.urgency.value, "channel_id": channel_id, "chat": chat,
            "chat_kind": delivery.chat.value, "task_id": task_id, "step_id": step_id, "schedule_id": schedule_id,
            "decision": decision.outcome.value, "reason": decision.reason.value, "counted": decision.counted,
            "local_date": local_date, "digest_date": decision.digest_date, "decided_at": now})
    return _decision(row)


def _decision(row: Mapping[str, Any]) -> Decision:
    return Decision(Outcome(row["decision"]), Reason(row["reason"]), row["counted"], row["digest_date"])
