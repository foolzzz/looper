"""User gates: steps of a task's plan where the user and the task's Planner discuss what only the user can decide.

A gate is a step of kind gate (``planner_protocol.GateSpec``): a topic and an
agenda of questions. It opens once the steps it depends on are done, and the
steps that depend on it wait until it closes; the rest of the plan runs on.
Each message the user writes in it reaches the Planner in a ``[GATE]`` turn
of its own session, whose reply updates the agenda and either goes on with
the discussion or closes the gate with a summary, which is kept as a
supplement of the task (``via`` ``gate``) every later Worker and Reviewer
reads. A DECISION or CHAT reply may update a gate or cancel it (``gates``),
when what the task found answers it or it no longer matters.

This module owns the types: a gate's states and how each shows as its
step's status (``STEP_STATUS``), its agenda, whether a task may open gates
(``GatePolicy``: a task a schedule started runs unattended and may not, and
a project turns them on with ``user_gates``), the summary a gate closes with
and the conclusion text kept from it. The rows are ``db.user_gates``'s; the
Planner keeps one ``Gate`` per gate step; ``gate_service`` takes the user's
messages from the dashboard and sends the reminders.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# A step's (and its task's) status while a gate waits for the user.
WAITING_USER = "waiting_user"
# What a gate's step spec and its updates keep.
TOPIC_MAX_CHARS = 200
QUESTION_MAX_CHARS = 500
OPTIONS_MAX = 6
OPTION_MAX_CHARS = 100
ANSWER_MAX_CHARS = 1_000
FINDINGS_MAX_CHARS = 2_000
NOTE_MAX_CHARS = 500
RESOLUTION_MAX_CHARS = 1_000
SUMMARY_ITEM_MAX_CHARS = 500
# What a user's message in a gate keeps, as a supplement does.
MESSAGE_MAX_CHARS = 8_000
# The conclusion a gate that passed leaves as a supplement.
CONCLUSION_MAX_CHARS = 3_000
# What a new session's resume brief gets of each waiting gate's discussion so far.
THREAD_MAX_CHARS = 8_000
# A [GATE] turn that failed is tried again on its own once after each of these waits (seconds since it failed).
RETRY_WAIT_SEC = (60, 300)


class GateState(str, Enum):
    PENDING = "pending"  # its dependencies are not done
    OPEN = "open"  # it waits for the user, who has not written yet
    DISCUSSING = "discussing"  # the user wrote in it
    PASSED = "passed"  # closed with a summary
    CANCELLED = "cancelled"  # what the task found answers it, or it no longer matters
    SKIPPED = "skipped"  # replanned away before it opened

    @property
    def waiting(self) -> bool:
        """Whether the gate waits for the user: open, or under discussion."""
        return self in (GateState.OPEN, GateState.DISCUSSING)

    @property
    def closed(self) -> bool:
        return self in (GateState.PASSED, GateState.CANCELLED, GateState.SKIPPED)

    @property
    def step_status(self) -> str:
        """The status its step has (``task_steps.status``): a gate that passed or was cancelled is a done step, so
        the steps that depend on it may start."""
        return STEP_STATUS[self]


STEP_STATUS: Mapping[GateState, str] = {
    GateState.PENDING: "pending",
    GateState.OPEN: WAITING_USER,
    GateState.DISCUSSING: WAITING_USER,
    GateState.PASSED: "verified",
    GateState.CANCELLED: "verified",
    GateState.SKIPPED: "skipped",
}


class GatePolicy(str, Enum):
    """Whether a task's Planner may open new gates."""

    ALLOWED = "allowed"
    OFF = "off"  # its project has user_gates off
    UNATTENDED = "unattended"  # a schedule started it: no one waits to answer

    @property
    def refusal(self) -> str:
        """Why a gate in a reply is refused, and what to do instead (empty when allowed)."""
        if self is GatePolicy.ALLOWED:
            return ""
        why = ("this task runs unattended (a schedule started it), so it cannot wait for the user"
               if self is GatePolicy.UNATTENDED else "this project does not let you open gates (user_gates is off)")
        return (f"{why}: decide yourself, write your assumption into the instructions of the steps that rely on it, "
                "and list your assumptions in the final summary's open_issues")


def gate_policy(*, gates_on: bool, scheduled: bool) -> GatePolicy:
    """The policy of a task whose project has user gates ``gates_on``, and that a schedule started when
    ``scheduled``."""
    if scheduled:
        return GatePolicy.UNATTENDED
    return GatePolicy.ALLOWED if gates_on else GatePolicy.OFF


class AgendaStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DROPPED = "dropped"


@dataclass(frozen=True)
class AgendaItem:
    """One question of a gate's agenda: ``id`` is code's (``q1``, ``q2``, …)."""

    id: str
    question: str
    options: tuple[str, ...] = ()
    status: AgendaStatus = AgendaStatus.OPEN
    answer: str = ""

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "question": self.question, "options": list(self.options),
                "status": self.status.value, "answer": self.answer}

    @classmethod
    def from_view(cls, value: Mapping[str, Any]) -> AgendaItem:
        return cls(str(value.get("id") or ""), str(value.get("question") or ""),
                   tuple(str(o) for o in value.get("options") or []),
                   AgendaStatus(value.get("status") or AgendaStatus.OPEN.value), str(value.get("answer") or ""))

    def line(self) -> str:
        """The item as a turn's message lists it."""
        text = f"- {self.id} [{self.status.value}] {self.question}"
        if self.options:
            text += f" (options: {' / '.join(self.options)})"
        if self.status is AgendaStatus.RESOLVED and self.answer:
            text += f" → {self.answer}"
        return text


def numbered(items: Iterable[AgendaItem], current: Sequence[AgendaItem] = ()) -> tuple[AgendaItem, ...]:
    """``items`` with an id for each new one (none yet), after the highest ``q<n>`` of ``current`` and ``items``."""
    items = list(items)
    taken = [item.id for item in (*current, *items)]
    highest = max((int(i[1:]) for i in taken if i[:1] == "q" and i[1:].isdigit()), default=0)
    out = []
    for item in items:
        if not item.id:
            highest += 1
            item = AgendaItem(f"q{highest}", item.question, item.options, item.status, item.answer)
        out.append(item)
    return tuple(out)


def resolved(agenda: Iterable[AgendaItem]) -> tuple[int, int]:
    """How many of ``agenda``'s items are resolved or dropped, and how many there are."""
    items = list(agenda)
    return sum(1 for item in items if item.status is not AgendaStatus.OPEN), len(items)


class GateVerdict(str, Enum):
    """What a [GATE] reply does with its gate."""

    CONTINUE = "continue"
    CLOSE = "close"


class GateAction(str, Enum):
    """What a DECISION or CHAT reply's ``gates`` does to a gate."""

    UPDATE = "update"
    CANCEL = "cancel"


SUMMARY_FIELDS = ("decisions", "assumptions", "open")
# How a conclusion heads each field.
_SUMMARY_HEADINGS = {"decisions": "Decisions", "assumptions": "Assumptions (not confirmed by the user)",
                     "open": "Still open"}


@dataclass(frozen=True)
class GateSummary:
    """What a gate closed with: what was settled, what the Planner assumes without the user's word, and what is
    still open."""

    decisions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    open: tuple[str, ...] = ()

    def view(self) -> dict[str, list[str]]:
        return {name: list(getattr(self, name)) for name in SUMMARY_FIELDS}

    @classmethod
    def from_view(cls, value: Mapping[str, Any] | None) -> GateSummary | None:
        if not isinstance(value, Mapping):
            return None
        return cls(*(tuple(str(item) for item in value.get(name) or []) for name in SUMMARY_FIELDS))


def conclusion_text(label: str, topic: str, summary: GateSummary) -> str:
    """The supplement a gate that passed leaves: what was settled with the user, for every later Worker and
    Reviewer, at most ``CONCLUSION_MAX_CHARS`` characters."""
    lines = [f"Settled with the user at gate {label} ({topic}):"]
    for name in SUMMARY_FIELDS:
        items = getattr(summary, name)
        if items:
            lines += [f"{_SUMMARY_HEADINGS[name]}:", *(f"- {item}" for item in items)]
    if len(lines) == 1:
        lines.append("Nothing to note: go on as planned.")
    text = "\n".join(lines)
    return text if len(text) <= CONCLUSION_MAX_CHARS else text[: CONCLUSION_MAX_CHARS - 1].rstrip() + "…"


class MessageRole(str, Enum):
    USER = "user"
    PLANNER = "planner"
    LOOPER = "looper"  # code's own: the gate opened, changed, was cancelled, closed, a reminder, an error, a late message


class MessageKind(str, Enum):
    MESSAGE = "message"  # the user's
    END = "end"  # the user ended the discussion: the next [GATE] turn closes the gate
    REPLY = "reply"  # the Planner's
    OPENED = "opened"
    UPDATED = "updated"
    REMINDER = "reminder"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    RESUMED = "resumed"  # the task was restarted while the gate waits
    ERROR = "error"  # the Planner could not take the user's messages
    LATE = "late"  # the user's message reached the gate as it closed: it became a supplement of the task


def retry_at(failures: int, failed_at: datetime | None, waits: Sequence[float] = RETRY_WAIT_SEC) -> datetime | None:
    """When a gate whose last ``failures`` [GATE] turns failed, the latest at ``failed_at``, is tried again on its own:
    after ``waits[failures - 1]``; None when none failed or no retry is left."""
    if not 0 < failures <= len(waits) or failed_at is None:
        return None
    return failed_at + timedelta(seconds=waits[failures - 1])


@dataclass
class Gate:
    """A gate as its task's Planner keeps it (the Planner's columns of its ``task_user_gates`` row)."""

    topic: str
    agenda: tuple[AgendaItem, ...]
    findings: str = ""
    state: GateState = GateState.PENDING
    summary: GateSummary | None = None
    resolution: str = ""  # why it was cancelled
    ended_by_user: bool = False  # it passed because the user ended the discussion
    delivered_through: int = 0  # the last user message (task_user_gate_messages.id) a turn of the Planner had
    turns: int = 0  # [GATE] turns whose reply was taken
    # The [GATE] turns in a row that failed, the last user message the latest of them had, and when it failed.
    failures: int = 0
    failed_through: int = 0
    failed_at: datetime | None = None
    plan_change: dict[str, list[str]] | None = None  # the steps the turn that closed it added, dropped and stopped
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    # Not the Planner's: the API sets it with the user's first message.
    first_reply_at: datetime | None = field(default=None, compare=False)

    def retry_at(self, waits: Sequence[float] = RETRY_WAIT_SEC) -> datetime | None:
        """When its failed [GATE] turn is tried again on its own (after ``waits``, one for each retry); None when no
        turn failed or no retry is left."""
        return retry_at(self.failures, self.failed_at, waits)

    def row(self, step_id: str) -> dict[str, Any]:
        """The gate as ``db.user_gates.write`` writes it."""
        return {"step_id": step_id, "topic": self.topic, "agenda": [item.view() for item in self.agenda],
                "findings": self.findings, "state": self.state.value,
                "summary": self.summary.view() if self.summary else None, "resolution": self.resolution,
                "ended_by_user": self.ended_by_user, "delivered_through": self.delivered_through,
                "turns": self.turns, "failures": self.failures, "failed_through": self.failed_through,
                "failed_at": self.failed_at, "plan_change": self.plan_change, "opened_at": self.opened_at,
                "closed_at": self.closed_at}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Gate:
        return cls(
            topic=str(row.get("topic") or ""),
            agenda=tuple(AgendaItem.from_view(item) for item in row.get("agenda") or []),
            findings=str(row.get("findings") or ""),
            state=GateState(row.get("state") or GateState.PENDING.value),
            summary=GateSummary.from_view(row.get("summary")),
            resolution=str(row.get("resolution") or ""),
            ended_by_user=bool(row.get("ended_by_user")),
            delivered_through=int(row.get("delivered_through") or 0),
            turns=int(row.get("turns") or 0),
            failures=int(row.get("failures") or 0),
            failed_through=int(row.get("failed_through") or 0),
            failed_at=row.get("failed_at"),
            plan_change=row.get("plan_change"),
            opened_at=row.get("opened_at"),
            closed_at=row.get("closed_at"),
            first_reply_at=row.get("first_reply_at"),
        )


@dataclass(frozen=True)
class GateView:
    """What checking a reply needs to know of one gate of the plan."""

    state: GateState
    agenda: tuple[AgendaItem, ...] = ()
