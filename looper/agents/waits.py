"""Waits: steps of a task's plan that wait for something outside the task.

A wait opens once the steps it depends on are done. It waits for its earliest
time and, when given, a typed check; the steps that depend on it wait until
the Planner closes it. The loop writes ready or timed_out, and the user may
request cancellation; the Planner writes pending, waiting, done, failed and
skipped. Ready needs no model turn; timed_out and cancel_requested get a
``[WAIT]`` turn, which may extend, proceed or fail. A DECISION or CHAT reply
may also cancel an unfinished wait.

This module owns those states and their step statuses, the check and spec
types, and the Planner's columns of ``task_waits``. The loop owns the check
results and notice. System limits live in ``system_settings``: ``load`` reads
them at startup for synchronous protocol validation, and ``update`` refreshes
them. Check output is kept to 2 KiB, checks take at most a minute, and command
checks need their project's explicit permission.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from ..db import system_settings as db_settings

logger = logging.getLogger(__name__)

WAITING = "waiting"
TOPIC_MAX_CHARS = 200
COMMAND_MAX_CHARS = 2_000
RESOLUTION_MAX_CHARS = 1_000
REASON_MAX_CHARS = 1_000
OUTPUT_MAX_BYTES = 2_048
CHECK_TIMEOUT_SEC = 60
DEFAULT_EVERY_SECONDS = 600
WAIT_TICK_SEC = 15
MAX_CONCURRENT_CHECKS = 4
# A [WAIT] turn that failed is tried again on its own once after each of these waits (seconds since it failed).
RETRY_WAIT_SEC = (60, 300)
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

MAX_WAIT_KEY = "waits.max_wait_seconds"
DEFAULT_MAX_WAIT_SECONDS = 7 * 86400
MAX_WAIT_RANGE = (60, 30 * 86400)
MIN_CHECK_KEY = "waits.min_check_seconds"
DEFAULT_MIN_CHECK_SECONDS = 300
MIN_CHECK_RANGE = (10, 86400)


@dataclass(frozen=True)
class WaitLimits:
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
    min_check_seconds: int = DEFAULT_MIN_CHECK_SECONDS


_limits = WaitLimits()
_KEYS = {"max_wait_seconds": MAX_WAIT_KEY, "min_check_seconds": MIN_CHECK_KEY}
_RANGES = {"max_wait_seconds": MAX_WAIT_RANGE, "min_check_seconds": MIN_CHECK_RANGE}
_sources: dict[str, str] = {}


def limits() -> WaitLimits:
    """The limits loaded at startup or last changed in Settings."""
    return _limits


def _seconds(raw: Any, name: str) -> int:
    low, high = _RANGES[name]
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ValueError(f"{name} must be a whole number of seconds from {low} to {high}")
    return raw


async def load() -> WaitLimits:
    """Read both limits into memory; an invalid stored setting falls back to the built-in with a warning."""
    global _limits, _sources
    stored = await db_settings.get_settings(list(_KEYS.values()))
    values, sources = {}, {}
    for name, key in _KEYS.items():
        values[name] = getattr(WaitLimits(), name)
        if stored.get(key) is not None:
            try:
                values[name] = _seconds(int(stored[key]), name)
                sources[name] = "setting"
            except (TypeError, ValueError):
                logger.warning("the stored %s is not a whole number of seconds from %d to %d; using the built-in",
                               key, *_RANGES[name])
    if values["min_check_seconds"] > values["max_wait_seconds"]:
        logger.warning("the stored %s exceeds %s; using the built-in limits", MIN_CHECK_KEY, MAX_WAIT_KEY)
        values = {name: getattr(WaitLimits(), name) for name in _KEYS}
        sources = {}
    _limits, _sources = WaitLimits(**values), sources
    return _limits


async def settings_view() -> dict[str, Any]:
    """Each limit's value, built-in, range and source (``setting`` or ``built-in``)."""
    return {name: {"value": getattr(_limits, name), "default": getattr(WaitLimits(), name),
                   "min": _RANGES[name][0], "max": _RANGES[name][1],
                   "source": _sources.get(name, "built-in")} for name in _KEYS}


async def update(body: Mapping[str, Any]) -> dict[str, Any]:
    """Store and apply one or both limits (null: the built-in); a refused body changes nothing."""
    if not isinstance(body, Mapping):
        raise ValueError("wait settings must be an object")
    if unknown := [key for key in body if key not in _KEYS]:
        raise ValueError(f"unknown setting {unknown[0]!r}; waits has {', '.join(_KEYS)}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(_KEYS)} (null: the built-in)")
    values = {name: None if raw is None else _seconds(raw, name) for name, raw in body.items()}
    after = {name: (getattr(WaitLimits(), name) if values[name] is None else values[name])
             if name in values else getattr(_limits, name) for name in _KEYS}
    if after["min_check_seconds"] > after["max_wait_seconds"]:
        raise ValueError("min_check_seconds must not exceed max_wait_seconds")
    await db_settings.put_settings({_KEYS[name]: None if value is None else str(value)
                                    for name, value in values.items()})
    await load()
    return await settings_view()


class WaitState(str, Enum):
    PENDING = "pending"
    WAITING = "waiting"
    READY = "ready"
    TIMED_OUT = "timed_out"
    CANCEL_REQUESTED = "cancel_requested"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def open(self) -> bool:
        return self in (WaitState.WAITING, WaitState.READY, WaitState.TIMED_OUT, WaitState.CANCEL_REQUESTED)

    @property
    def closed(self) -> bool:
        return self in (WaitState.DONE, WaitState.FAILED, WaitState.SKIPPED)

    @property
    def needs_planner(self) -> bool:
        return self in (WaitState.READY, WaitState.TIMED_OUT, WaitState.CANCEL_REQUESTED)

    @property
    def step_status(self) -> str:
        return STEP_STATUS[self]


STEP_STATUS: Mapping[WaitState, str] = {
    WaitState.PENDING: "pending",
    WaitState.WAITING: WAITING,
    WaitState.READY: WAITING,
    WaitState.TIMED_OUT: WAITING,
    WaitState.CANCEL_REQUESTED: WAITING,
    WaitState.DONE: "verified",
    WaitState.FAILED: "failed",
    WaitState.SKIPPED: "skipped",
}


class WaitOutcome(str, Enum):
    READY = "ready"
    RESUMED = "resumed"
    PROCEEDED = "proceeded"
    CANCELLED = "cancelled"


class CheckType(str, Enum):
    GITHUB_PR = "github_pr"
    COMMAND = "command"


class ReadyWhen(str, Enum):
    HEAD_CHANGED = "head_changed"
    CHECKS_FINISHED = "checks_finished"
    NEW_COMMENTS = "new_comments"


class CheckResult(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    ERROR = "error"


class WaitAction(str, Enum):
    """What a [WAIT] reply does with its wait."""

    EXTEND = "extend"
    PROCEED = "proceed"
    FAIL = "fail"


class WaitChangeAction(str, Enum):
    """What a DECISION or CHAT reply's ``waits`` does to a wait."""

    CANCEL = "cancel"


@dataclass(frozen=True)
class GithubPrCheck:
    repo: str
    number: int
    ready_when: ReadyWhen

    def row(self) -> dict[str, Any]:
        return {"type": CheckType.GITHUB_PR.value, "repo": self.repo, "number": self.number,
                "ready_when": self.ready_when.value}

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> GithubPrCheck:
        return cls(str(raw["repo"]), int(raw["number"]), ReadyWhen(raw["ready_when"]))

    def describe(self) -> str:
        return f"github_pr {self.repo}#{self.number} {self.ready_when.value}"


@dataclass(frozen=True)
class CommandCheck:
    command: str

    def row(self) -> dict[str, Any]:
        return {"type": CheckType.COMMAND.value, "command": self.command}

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> CommandCheck:
        return cls(str(raw["command"]))

    def describe(self) -> str:
        return f"command `{self.command[:80]}`"


Check = GithubPrCheck | CommandCheck


def check_from_row(raw: Mapping[str, Any] | None) -> Check | None:
    if raw is None:
        return None
    kind = CheckType(raw["type"])
    return GithubPrCheck.from_row(raw) if kind is CheckType.GITHUB_PR else CommandCheck.from_row(raw)


@dataclass(frozen=True)
class WaitSpec:
    topic: str
    max_wait_seconds: int
    not_before_seconds: int | None = None
    not_before: datetime | None = None
    check: Check | None = None
    every_seconds: int | None = None

    def row(self) -> dict[str, Any]:
        return {"max_wait_seconds": self.max_wait_seconds, "not_before_seconds": self.not_before_seconds,
                "not_before": self.not_before.isoformat() if self.not_before else None,
                "check": self.check.row() if self.check else None, "every_seconds": self.every_seconds}

    @classmethod
    def from_row(cls, topic: str, raw: Mapping[str, Any]) -> WaitSpec:
        return cls(topic, int(raw["max_wait_seconds"]), raw.get("not_before_seconds"),
                   datetime.fromisoformat(raw["not_before"]) if raw.get("not_before") else None,
                   check_from_row(raw.get("check")), raw.get("every_seconds"))


def effective_every(spec: WaitSpec, limits: WaitLimits) -> int | None:
    """A check's interval: its own, else the default within the wait's length and the system minimum."""
    if spec.check is None:
        return None
    return (spec.every_seconds if spec.every_seconds is not None else
            max(limits.min_check_seconds, min(DEFAULT_EVERY_SECONDS, spec.max_wait_seconds)))


def open_times(spec: WaitSpec, opened_at: datetime) -> tuple[datetime, datetime, datetime]:
    """The earliest time, deadline and first check time of a wait opened at ``opened_at``."""
    due_at = max(opened_at + timedelta(seconds=spec.not_before_seconds or 0), spec.not_before or opened_at)
    return due_at, opened_at + timedelta(seconds=spec.max_wait_seconds), opened_at


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


def describe(spec: WaitSpec, every: int | None) -> str:
    """The check, earliest time and maximum wait as a turn or activity describes them."""
    parts = []
    if spec.check is not None:
        parts.append(spec.check.describe() + (f" every {_duration(every)}" if every is not None else ""))
    if spec.not_before is not None:
        parts.append("until " + spec.not_before.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    elif spec.not_before_seconds:
        parts.append(f"after {_duration(spec.not_before_seconds)}")
    parts.append(f"at most {_duration(spec.max_wait_seconds)}")
    return "; ".join(parts)


@dataclass(frozen=True)
class WaitPolicy:
    limits: WaitLimits = WaitLimits()
    commands: bool = False

    command_refusal = ("this project does not let a wait run a command (wait_commands is off): "
                       "use a github_pr check, or a time only (not_before_seconds)")


@dataclass
class Wait:
    """A wait as its task's Planner keeps it (the Planner's columns of its ``task_waits`` row)."""

    spec: WaitSpec
    state: WaitState = WaitState.PENDING
    outcome: WaitOutcome | None = None
    opened_at: datetime | None = None
    due_at: datetime | None = None
    deadline_at: datetime | None = None
    next_check_at: datetime | None = None
    closed_at: datetime | None = None
    failed_at: datetime | None = None
    resolution: str = ""
    extensions: int = 0
    failures: int = 0

    @property
    def topic(self) -> str:
        return self.spec.topic

    def row(self, step_id: str) -> dict[str, Any]:
        """The wait as ``db.waits.write`` writes it, with the check interval resolved."""
        spec = self.spec.row()
        spec["every_seconds"] = effective_every(self.spec, limits())
        return {"step_id": step_id, "topic": self.topic, "spec": spec, "state": self.state.value,
                "outcome": self.outcome.value if self.outcome else None, "opened_at": self.opened_at,
                "due_at": self.due_at, "deadline_at": self.deadline_at, "next_check_at": self.next_check_at,
                "resolution": self.resolution, "extensions": self.extensions, "failures": self.failures,
                "failed_at": self.failed_at, "closed_at": self.closed_at}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Wait:
        return cls(spec=WaitSpec.from_row(str(row.get("topic") or ""), row["spec"]),
                   state=WaitState(row.get("state") or WaitState.PENDING.value),
                   outcome=WaitOutcome(row["outcome"]) if row.get("outcome") else None,
                   opened_at=row.get("opened_at"), due_at=row.get("due_at"), deadline_at=row.get("deadline_at"),
                   next_check_at=row.get("next_check_at"), closed_at=row.get("closed_at"),
                   failed_at=row.get("failed_at"), resolution=str(row.get("resolution") or ""),
                   extensions=int(row.get("extensions") or 0), failures=int(row.get("failures") or 0))

    def retry_at(self, waits: Sequence[float] = RETRY_WAIT_SEC) -> datetime | None:
        """When its failed [WAIT] turn is tried again on its own; None when no retry is left."""
        if not 0 < self.failures <= len(waits) or self.failed_at is None:
            return None
        return self.failed_at + timedelta(seconds=waits[self.failures - 1])


@dataclass(frozen=True)
class WaitView:
    """What checking a reply needs to know of one wait of the plan."""

    state: WaitState
