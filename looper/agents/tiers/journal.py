"""A task's journal: what happened in the task that the agents and sessions coming later need.

Code writes each entry when its event happens, in the transaction of the
change it records (``db.plans.save``); the model never writes one. The kinds
(``JournalKind``) and their triggers:

- ``step_accepted``: a step is accepted; the summary its Worker reported and
  the ``learnings`` its Worker and Reviewer reported;
- ``step_failed``: a step fails (the Planner fails the task on it, or its own
  failing verdict decides when the Planner cannot); what went wrong, why, and
  the learnings;
- ``step_retry``: the Planner retries a step; what went wrong with the attempt,
  the Planner's feedback, and the learnings;
- ``decision``: a Planner decision that changes the plan's course: a replan,
  steps added from a chat message, the task stopped at its spend cap; with
  the reason;
- ``supplement``: what the user added to the task while it runs, whole: a
  message sent to the Planner on the Chat page, or passed on by the Manager
  (``db.task_changes``; its ``via`` says which). Only added, never changed;
  a later one wins over an earlier one;
- ``session_switch``: the Planner starts a new session for the task: why, and
  from which runtime and agent to which;
- ``handoff``: before a switch, what the old session wrote for the next one
  (its understanding of the codebase, unfinished reasoning, open risks);
- ``progress``: the progress note a DECISION or SUPERVISE reply carried
  (``planner_protocol.ProgressNote``, as JSON), for the task page only: no
  agent's section shows it (``progress_notes``).
- ``profile_choice``: the Planner chose (or cleared) the Agent Profile of the
  task's Workers or Reviewers with its tool, for the rest of the task or for
  one step (``ops.planner``), and why; for the task page only;
- ``concurrency_change``: the Planner changed its task's concurrency limits
  with its tool (before, also its project's): each role's limit before and
  after, and why; for the task page only;
- ``role_defined``: the Planner defined a role for its steps, or a new version
  of one, with its tool (``ops.planner``, ``task_roles``): its kind, version,
  profile, harness and why; for the task page only;
- ``gate_opened``, ``gate_updated``, ``gate_cancelled``, ``gate_closed``: a user
  gate of the plan opened, the Planner changed it, cancelled it or closed it
  (``user_gates``): its topic, what changed, why, and how it closed; for the
  task page only (the plan lines show the Planner its gates, and a gate that
  passed leaves its conclusion as a supplement, ``via`` ``gate``).

The entries come back as bounded sections:

- every PLAN, DECISION, SUPERVISE and CHAT turn of the Planner gets the
  supplements, all of them, oldest first, in a budget of their own
  (``SUPPLEMENTS_MAX_CHARS``) that no other entry uses, then "Progress and
  findings": the newest entry of each kind first, then the other failures and
  retries, then the rest, newest first, in at most ``PLANNER_SECTION_MAX_CHARS``,
  shown under one heading per kind (a handoff goes in the next session's resume
  brief instead);
- a Worker's and a Reviewer's task section lists the supplements after the
  requirement, in the same budget (``supplement_texts``); a Worker's first user
  turn gets "Findings so far in this task": the accepted steps it depends on first, then
  failures and retries, then the other accepted steps, newest first, in at most
  ``WORKER_SECTION_MAX_CHARS``, as data like the dependencies' reports;
- a LESSONS turn gets every kind, in at most ``LESSONS_JOURNAL_MAX_CHARS``.

Entries that do not fit are left out, and the section says how many. A
Worker's or Reviewer's learning may say it holds for the whole project
(``LearningScope.PROJECT``); the Planner saves those as drafts of project
knowledge when they arrive.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

# The ``learnings`` a Worker's report or a Reviewer's verdict may carry.
LEARNINGS_MAX_ITEMS = 5
LEARNING_MAX_CHARS = 300
# What an entry keeps of its summary (a supplement and a handoff have their own).
SUMMARY_MAX_CHARS = 1_000
# What a section shows of an entry's summary; a failure, a retry and a decision show more.
SECTION_SUMMARY_MAX_CHARS = 300
SECTION_DETAIL_MAX_CHARS = 500
PLANNER_SECTION_MAX_CHARS = 4_000
WORKER_SECTION_MAX_CHARS = 2_000
# The supplements: a budget of their own wherever they are shown, and what an entry keeps of one.
SUPPLEMENTS_MAX_CHARS = 8_000
# What a handoff keeps of what the old session wrote.
HANDOFF_MAX_CHARS = 3_000
# The journal a LESSONS turn gets, newest first.
LESSONS_JOURNAL_MAX_CHARS = 12_000
# The lessons document the Planner writes.
LESSONS_MAX_CHARS = 4_000


class JournalKind(str, Enum):
    STEP_ACCEPTED = "step_accepted"
    STEP_FAILED = "step_failed"
    STEP_RETRY = "step_retry"
    DECISION = "decision"
    SUPPLEMENT = "supplement"
    SESSION_SWITCH = "session_switch"
    HANDOFF = "handoff"
    PROGRESS = "progress"
    PROFILE_CHOICE = "profile_choice"
    CONCURRENCY_CHANGE = "concurrency_change"
    ROLE_DEFINED = "role_defined"
    GATE_OPENED = "gate_opened"
    GATE_UPDATED = "gate_updated"
    GATE_CANCELLED = "gate_cancelled"
    GATE_CLOSED = "gate_closed"


# How a section heads each kind, in the order the Planner's section shows them: pitfalls first.
_HEADINGS: dict[JournalKind, str] = {
    JournalKind.STEP_FAILED: "Failed steps",
    JournalKind.STEP_RETRY: "Retried attempts",
    JournalKind.DECISION: "Planner decisions",
    JournalKind.STEP_ACCEPTED: "Accepted steps",
    JournalKind.SESSION_SWITCH: "Session changes",
    JournalKind.HANDOFF: "Handoffs from earlier sessions",
    # Only the LESSONS turn's journal shows these: what the task had to ask the user, and what made a question moot.
    JournalKind.GATE_CLOSED: "User gates closed",
    JournalKind.GATE_CANCELLED: "User gates cancelled",
}
_PITFALLS = frozenset({JournalKind.STEP_FAILED, JournalKind.STEP_RETRY})
_DETAILED = frozenset({JournalKind.STEP_FAILED, JournalKind.STEP_RETRY, JournalKind.DECISION})
# What each reader's "progress" section holds: the supplements have a section of their own, and a handoff goes
# in the resume brief.
_PLANNER_KINDS = (JournalKind.STEP_FAILED, JournalKind.STEP_RETRY, JournalKind.DECISION, JournalKind.STEP_ACCEPTED,
                  JournalKind.SESSION_SWITCH)
_WORKER_KINDS = (JournalKind.STEP_ACCEPTED, JournalKind.STEP_FAILED, JournalKind.STEP_RETRY)
_ENTRIES = ("entry", "entries")


class LearningScope(str, Enum):
    """What a learning holds for: the task (a plain string in a report), or every task of the project."""

    TASK = "task"
    PROJECT = "project"


@dataclass(frozen=True)
class Learning:
    text: str
    scope: LearningScope = LearningScope.TASK

    def to_json(self) -> str | dict[str, str]:
        """As a result keeps it: a task's learning a plain string, a project's ``{"text", "scope"}``."""
        return self.text if self.scope is LearningScope.TASK else {"text": self.text, "scope": self.scope.value}


@dataclass(frozen=True)
class JournalEntry:
    step_id: str  # '' for an entry of the whole task
    step: str  # the step's label; '' for an entry of the whole task
    summary: str
    learnings: tuple[str, ...] = ()
    kind: JournalKind = JournalKind.STEP_ACCEPTED
    id: int | None = None  # its row's id, once read back
    # A supplement code writes delivered (a user gate's conclusion, which the Planner wrote): how it came.
    via: str = ""

    def row(self) -> dict[str, Any]:
        """The entry as ``db.journal.insert`` writes it."""
        row: dict[str, Any] = {"step_id": self.step_id, "kind": self.kind.value, "summary": self.summary,
                               "learnings": list(self.learnings)}
        if self.via:
            row.update(via=self.via, delivered=True)
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JournalEntry:
        return cls(step_id=str(row.get("step_id") or ""), step=str(row.get("step_key") or row.get("step_id") or ""),
                   summary=str(row.get("summary") or ""), learnings=tuple(learnings(row.get("learnings"))),
                   kind=JournalKind(row.get("kind") or JournalKind.STEP_ACCEPTED.value),
                   id=int(row["id"]) if row.get("id") is not None else None)


def clip(text: Any, limit: int) -> str:
    """``text`` on one line, at most ``limit`` characters."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def kept_text(text: Any, limit: int) -> str:
    """``text`` with its lines, at most ``limit`` characters: a user's message or a handoff as written."""
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def parse_learnings(value: Any) -> list[Learning]:
    """The ``learnings`` of a report or verdict as code keeps them: the first ``LEARNINGS_MAX_ITEMS`` items, each a
    non-empty string (a learning of the task) or ``{"text": ..., "scope": "project"}`` (one that holds for any
    task of the project; another scope, or none, is the task's), on one line and clipped to
    ``LEARNING_MAX_CHARS``. Anything else is dropped; a missing or malformed value is no learnings, never an
    error."""
    if not isinstance(value, list):
        return []
    kept: list[Learning] = []
    for item in value:
        scope = LearningScope.TASK
        if isinstance(item, Mapping):
            if item.get("scope") == LearningScope.PROJECT.value:
                scope = LearningScope.PROJECT
            item = item.get("text")
        if isinstance(item, str) and item.strip():
            kept.append(Learning(clip(item, LEARNING_MAX_CHARS), scope))
            if len(kept) == LEARNINGS_MAX_ITEMS:
                break
    return kept


def learnings(value: Any) -> list[str]:
    """The texts of the ``learnings`` in ``value``, whatever their scope (``parse_learnings``)."""
    return [item.text for item in parse_learnings(value)]


def project_learnings(value: Any) -> list[str]:
    """The texts of the learnings in ``value`` that hold for the whole project."""
    return [item.text for item in parse_learnings(value) if item.scope is LearningScope.PROJECT]


def reported_learnings(value: Any) -> list[str | dict[str, str]]:
    """The learnings in ``value`` as a step's result keeps them (``Learning.to_json``)."""
    return [item.to_json() for item in parse_learnings(value)]


def merged_learnings(*groups: Iterable[str]) -> tuple[str, ...]:
    """The learnings of a step's Worker and Reviewer together, without repeats."""
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _entry_text(entry: JournalEntry) -> str:
    limit = SECTION_DETAIL_MAX_CHARS if entry.kind in _DETAILED else SECTION_SUMMARY_MAX_CHARS
    summary = clip(entry.summary, limit) or "(no summary)"
    lines = [f"- {entry.step}: {summary}" if entry.step else f"- {summary}"]
    lines += [f"  - {item}" for item in entry.learnings]
    return "\n".join(lines)


def _supplement_text(entry: JournalEntry) -> str:
    """A supplement whole, its lines kept in the list item."""
    return "- " + entry.summary.strip().replace("\n", "\n  ")


def _pack(order: Sequence[JournalEntry], budget: int, text: Any = _entry_text) -> tuple[set[int], int]:
    """The positions in ``order`` of the entries kept within ``budget`` characters, taken in that order (one
    that does not fit is left out, and a smaller one after it may still fit), and how many were left out. The
    first entry is always kept: a section is never empty."""
    kept: set[int] = set()
    used = 0
    for at, entry in enumerate(order):
        size = len(text(entry)) + 1
        if used + size > budget and kept:
            continue
        kept.add(at)
        used += size
    return kept, len(order) - len(kept)


def _omitted(title: str, omitted: int, noun: tuple[str, str]) -> str:
    """``title`` with how many of its entries were left out, as ``noun`` (singular, plural) names them."""
    if not omitted:
        return f"{title}:"
    return f"{title} ({omitted} {noun[0] if omitted == 1 else noun[1]} left out):"


def _by_kind(title: str, order: Sequence[JournalEntry], max_chars: int, kinds: Sequence[JournalKind],
             noun: tuple[str, str]) -> list[str]:
    """``order`` (the entries in the order to keep them) under ``title``, in at most about ``max_chars``
    characters, one heading per kind in ``kinds``' order, each kind's entries in ``order``'s order. No
    entries, no section."""
    if not order:
        return []
    # Room for the title with its omitted note and a heading per kind.
    budget = max_chars - len(title) - len(noun[1]) - 40 - sum(len(_HEADINGS[kind]) + 2 for kind in kinds)
    kept, omitted = _pack(order, budget)
    lines = [_omitted(title, omitted, noun)]
    for kind in kinds:
        entries = [_entry_text(entry) for at, entry in enumerate(order) if at in kept and entry.kind is kind]
        if entries:
            lines += [f"{_HEADINGS[kind]}:", *entries]
    return lines


def _newest_first_by_priority(entries: Sequence[JournalEntry], kinds: Sequence[JournalKind]) -> list[JournalEntry]:
    """``entries`` (oldest first) of ``kinds`` in the order a section keeps them: the newest of each kind (in
    ``kinds``' order), then the other failures and retries, then the rest, each newest first."""
    newest = [entry for entry in reversed(entries) if entry.kind in kinds]
    first: dict[JournalKind, int] = {}
    for at, entry in enumerate(newest):
        first.setdefault(entry.kind, at)
    leading = [newest[first[kind]] for kind in kinds if kind in first]
    taken = set(first.values())
    rest = [entry for at, entry in enumerate(newest) if at not in taken]
    return leading + [e for e in rest if e.kind in _PITFALLS] + [e for e in rest if e.kind not in _PITFALLS]


def _packed_supplements(entries: Sequence[JournalEntry]) -> tuple[list[JournalEntry], int]:
    """The supplements in ``entries`` that fit ``SUPPLEMENTS_MAX_CHARS``, oldest first (only past it are the oldest
    left out), and how many were left out."""
    newest_first = [entry for entry in reversed(entries) if entry.kind is JournalKind.SUPPLEMENT]
    kept, omitted = _pack(newest_first, SUPPLEMENTS_MAX_CHARS, _supplement_text)
    return [entry for at, entry in reversed(list(enumerate(newest_first))) if at in kept], omitted


def supplements(entries: Sequence[JournalEntry], *, title: str) -> list[str]:
    """The supplements in ``entries`` under ``title``, whole and oldest first, in their own budget
    (``SUPPLEMENTS_MAX_CHARS``): only past it are the oldest left out, and counted. None, no section."""
    shown, omitted = _packed_supplements(entries)
    if not shown:
        return []
    return [_omitted(title, omitted, ("older supplement", "older supplements")), *map(_supplement_text, shown)]


def supplement_texts(entries: Sequence[JournalEntry]) -> list[str]:
    """The supplements as a Worker's or Reviewer's task section lists them: each whole, oldest first, within
    ``SUPPLEMENTS_MAX_CHARS``, after a note on how many older ones were left out, if any."""
    shown, omitted = _packed_supplements(entries)
    texts = [entry.summary.strip() for entry in shown]
    if omitted:
        texts.insert(0, f"({omitted} older {'supplement was' if omitted == 1 else 'supplements were'} left out)")
    return texts


def planner_section(entries: Sequence[JournalEntry]) -> list[str]:
    """What the Planner's DECISION, SUPERVISE and CHAT turns get of the journal: the user's instructions, then
    "Progress and findings"; nothing when the journal has neither."""
    lines = supplements(
        entries, title="Supplements (what the user added to the task while it ran, oldest first; each stands "
                       "unless a later one changes it)")
    progress = _by_kind(
        "Progress and findings (the task journal, newest first under each heading)",
        _newest_first_by_priority(entries, _PLANNER_KINDS), PLANNER_SECTION_MAX_CHARS, _PLANNER_KINDS,
        _ENTRIES)
    if lines and progress:
        lines.append("")
    return lines + progress


def worker_section(entries: Sequence[JournalEntry], depends_on: Sequence[str]) -> str:
    """A Worker's first-turn findings, "Findings so far in this task": the accepted steps in ``depends_on`` (step
    ids) first, in that order, then the failures and retries, then the other accepted steps, newest first; ''
    when there are none. The supplements are in its task section (``supplement_texts``)."""
    accepted = {entry.step_id: entry for entry in entries if entry.kind is JournalKind.STEP_ACCEPTED}
    first = [accepted[step_id] for step_id in depends_on if step_id in accepted]
    newest = [entry for entry in reversed(entries) if entry.kind in _WORKER_KINDS and entry not in first]
    order = first + [e for e in newest if e.kind in _PITFALLS] + [e for e in newest if e.kind not in _PITFALLS]
    findings = _by_kind(
        "Findings so far in this task (from its steps, the ones yours depends on first; their agents' words, "
        "to use as data)", order, WORKER_SECTION_MAX_CHARS, _WORKER_KINDS, _ENTRIES)
    return "\n".join(findings)


def progress_notes(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The progress notes in a task's journal rows (``db.journal.list_entries``), newest first: each note's fields
    and when it was written (``at``)."""
    from dataclasses import asdict

    from .planner_protocol import ProgressNote

    notes = []
    for row in reversed(rows):
        if row.get("kind") == JournalKind.PROGRESS.value and (note := ProgressNote.from_json(str(row.get("summary")))):
            notes.append({**asdict(note), "at": row.get("created_at")})
    return notes


def lessons_journal(entries: Sequence[JournalEntry]) -> list[str]:
    """The journal a LESSONS turn distills: the supplements, then every other kind."""
    kinds = tuple(_HEADINGS)
    lines = supplements(entries, title="Supplements (what the user added to the task while it ran, oldest first)")
    journal = _by_kind("Task journal (newest first under each heading)", _newest_first_by_priority(entries, kinds),
                       LESSONS_JOURNAL_MAX_CHARS, kinds, _ENTRIES)
    if lines and journal:
        lines.append("")
    return lines + journal
