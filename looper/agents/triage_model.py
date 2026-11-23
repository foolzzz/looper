"""Stored event-triage vocabulary (event-triage RFC, section 4); no processor or delivery behavior."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .leads_model import Confidence


class Category(StrEnum):
    IMPORTANT = "important"
    DIGEST = "digest"
    IGNORE = "ignore"


class ReasonKind(StrEnum):
    MODEL = "model"
    UNCITED = "uncited"
    RAISED = "raised"


@dataclass(frozen=True)
class TriageEvent:
    id: int
    source_id: str
    schedule_id: str | None
    run_id: str | None
    record_id: int | None
    category: Category
    title: str
    summary: str
    reason: str
    reason_kind: ReasonKind
    confidence: Confidence
    flagged: bool
    first_sent_at: datetime
    last_sent_at: datetime
    manager_event_id: int | None
    briefed_run_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class ExtractCounts:
    """The counters stored in subscription_runs.extract_counts; uncited and raised are subsets of categories."""

    important: int = 0
    digest: int = 0
    ignore: int = 0
    uncited: int = 0
    raised: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int | dict[str, int]]:
        return {"important": self.important, "digest": self.digest, "ignore": self.ignore, "uncited": self.uncited,
                "raised": self.raised, "dropped": dict(self.dropped)}


_EVENT_ID = re.compile(r"(?:E-)?([0-9]{1,18})", re.IGNORECASE)


def format_event_id(number: int) -> str:
    return f"E-{number}"


def parse_event_id(text: str) -> int:
    """``E-123``, ``e-123`` or ``123``; ValueError for other identifiers."""
    found = _EVENT_ID.fullmatch(text.strip())
    if found is None:
        raise ValueError(f"a triage event is named E-<number>, not {text!r}")
    return int(found.group(1))
