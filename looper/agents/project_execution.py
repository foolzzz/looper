"""A project's execution settings: how the agents of its tasks run.

Every field is optional, and ``None`` inherits today's behavior, but for ``user_gates`` (unset is on):

- ``task_budget_usd``: the most a task may spend over all its runs, counted
  like the task's cost (``llm_records.task_costs``);
- ``task_budget_action``: what happens when a task reaches that cap: the task
  fails at once (``fail``, also when unset) or after one more Planner turn that
  sums up what was done (``wrap_up``);
- ``supervise_interval_seconds``: how often the Planner checks running Workers
  (0 = never), over the ``planner`` agent config;
- ``review_policy``: ``planner`` (the Planner decides which steps a Reviewer
  checks) or ``all`` (every step needs a passing review before it is accepted);
- ``distill_lessons``: what the Planner of a task that completes makes of the
  task's journal: nothing (``off``, the default), a lessons draft the user
  confirms into the project's knowledge or discards (``draft``), or a lessons
  document in it (``write``). ``true`` and ``false``, from before the three
  modes, are taken as ``write`` and ``off``;
- ``step_workspaces``: where a step that runs alongside others works: the
  task directory (``shared``, also when unset) or, when the task directory is
  a git checkout on a branch, a worktree of its own that is merged into the
  branch when the step is accepted (``worktree``, ``step_workspaces``);
- ``workspace_links``: the paths that Looper's worktrees of the project
  link back to the directory they come from (its ignored local state, such
  as ``.venv`` or ``.env``): a task's worktree to its workspace, a step's to
  its task directory; none when unset;
- ``task_workspaces``: where a task runs: in its workspace (``in_place``, also
  when unset) or in a git worktree of it, on a branch of its own
  (``worktree``, ``task_workspaces``); a new project gets ``worktree``;
- ``operator_notes``: whether the project's Planners, Workers and Reviewers
  get the operator's Claude Code notes for the directory they work in, read
  only (``on``, also when unset; ``off``; ``operator_notes``);
- ``max_step_retries``: how many times the Planner may retry one step, 0 to
  ``STEP_RETRIES_MAX`` (``DEFAULT_STEP_RETRIES`` when unset);
- ``planner_roles``: whether the project's Planners may define the roles their
  steps run as (``on``) or not (``off``, also when unset; ``task_roles``);
- ``role_text_max_chars``: how long such a role's prompt and its harness may
  each be, ``ROLE_TEXT_MIN_CHARS`` to ``ROLE_TEXT_MAX_CHARS``
  (``DEFAULT_ROLE_TEXT_CHARS`` when unset);
- ``user_gates``: whether the project's Planners may open user gates, where
  they discuss with the user what only the user can decide (``on``, also when
  unset), or not (``off``; ``user_gates``);
- ``gate_reminder_hours``: how many hours a gate waits for the user between
  reminders, 0 (none) to ``GATE_REMINDER_MAX_HOURS``
  (``DEFAULT_GATE_REMINDER_HOURS`` when unset);
- ``agent_room``: whether the agents of the project's tasks share a room, where
  they message each other (``on``, also when unset: on by default, the agent
  room RFC's D5), or not (``off``; ``room``);
- the room's limits, each a whole number in ``ROOM_LIMITS``' range (its
  default when unset): ``room_post_per_minute`` (messages a run may post in a
  minute), ``room_broadcasts_per_run`` (messages to everyone a run may post),
  ``room_message_max_chars``, ``room_messages_per_task``,
  ``room_ping_pong_rounds`` (messages two seats may trade before the Planner is
  told) and ``room_wait_max_seconds`` (how long an inbox call may wait).

The columns live on ``projects`` (``db.projects.EXECUTION_COLUMNS``); this
module owns their types and validation. Agents read them when a run or turn
starts, so a change reaches the next one. A project's harness, Agent Profile
(its model and effort), environment and per-run cap are agent settings: its
layer and its roles' layers (``agent_settings``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from ..db.projects import EXECUTION_COLUMNS
from .agent_settings import budget_usd

logger = logging.getLogger(__name__)

FIELDS = EXECUTION_COLUMNS
SUPERVISE_MAX_SECONDS = 86_400
DEFAULT_STEP_RETRIES = 2
STEP_RETRIES_MAX = 20
DEFAULT_ROLE_TEXT_CHARS = 8_000
ROLE_TEXT_MIN_CHARS = 500
ROLE_TEXT_MAX_CHARS = 20_000
DEFAULT_GATE_REMINDER_HOURS = 24
GATE_REMINDER_MAX_HOURS = 168
WORKSPACE_LINKS_MAX = 20
WORKSPACE_LINK_MAX_CHARS = 255
# Each room limit's range and its default (the agent room RFC, section 11), and what it counts.
ROOM_LIMITS: dict[str, tuple[int, int, int, str]] = {
    "room_post_per_minute": (1, 60, 6, "messages a run may post in a minute"),
    "room_broadcasts_per_run": (0, 50, 5, "messages to everyone a run may post"),
    "room_message_max_chars": (500, 20_000, 4_000, "characters a message may have"),
    "room_messages_per_task": (20, 5_000, 300, "messages a task's room may hold"),
    "room_ping_pong_rounds": (2, 50, 6, "messages two seats may trade before the Planner is told"),
    "room_wait_max_seconds": (0, 120, 60, "seconds an inbox call may wait"),
}


class ReviewPolicy(str, Enum):
    PLANNER = "planner"
    ALL = "all"


class TaskBudgetAction(str, Enum):
    WRAP_UP = "wrap_up"
    FAIL = "fail"


class StepWorkspaces(str, Enum):
    SHARED = "shared"
    WORKTREE = "worktree"


class TaskWorkspaces(str, Enum):
    IN_PLACE = "in_place"
    WORKTREE = "worktree"


class OperatorNotesMode(str, Enum):
    OFF = "off"
    ON = "on"


class PlannerRoles(str, Enum):
    OFF = "off"
    ON = "on"


class UserGates(str, Enum):
    OFF = "off"
    ON = "on"


class AgentRoom(str, Enum):
    OFF = "off"
    ON = "on"


@dataclass(frozen=True)
class RoomLimits:
    """A project's room limits, each set or its default."""

    post_per_minute: int = ROOM_LIMITS["room_post_per_minute"][2]
    broadcasts_per_run: int = ROOM_LIMITS["room_broadcasts_per_run"][2]
    message_max_chars: int = ROOM_LIMITS["room_message_max_chars"][2]
    messages_per_task: int = ROOM_LIMITS["room_messages_per_task"][2]
    ping_pong_rounds: int = ROOM_LIMITS["room_ping_pong_rounds"][2]
    wait_max_seconds: int = ROOM_LIMITS["room_wait_max_seconds"][2]


class LessonsMode(str, Enum):
    OFF = "off"
    DRAFT = "draft"
    WRITE = "write"


@dataclass(frozen=True)
class ProjectExecution:
    task_budget_usd: float | None = None
    task_budget_action: TaskBudgetAction | None = None
    supervise_interval_seconds: int | None = None
    review_policy: ReviewPolicy | None = None
    distill_lessons: LessonsMode = LessonsMode.OFF
    step_workspaces: StepWorkspaces | None = None
    workspace_links: tuple[str, ...] | None = None
    task_workspaces: TaskWorkspaces | None = None
    operator_notes: OperatorNotesMode | None = None
    max_step_retries: int | None = None
    planner_roles: PlannerRoles | None = None
    role_text_max_chars: int | None = None
    user_gates: UserGates | None = None
    gate_reminder_hours: int | None = None
    agent_room: AgentRoom | None = None
    room_post_per_minute: int | None = None
    room_broadcasts_per_run: int | None = None
    room_message_max_chars: int | None = None
    room_messages_per_task: int | None = None
    room_ping_pong_rounds: int | None = None
    room_wait_max_seconds: int | None = None

    @property
    def review_all(self) -> bool:
        return self.review_policy is ReviewPolicy.ALL

    @property
    def lessons_on(self) -> bool:
        return self.distill_lessons is not LessonsMode.OFF

    @property
    def isolate_steps(self) -> bool:
        """Whether steps that run alongside others get worktrees of their own."""
        return self.step_workspaces is StepWorkspaces.WORKTREE

    @property
    def link_paths(self) -> tuple[str, ...]:
        """The paths Looper's worktrees link back to the directory they come from."""
        return self.workspace_links or ()

    @property
    def task_mode(self) -> TaskWorkspaces:
        """Where a task that names no workspace of its own runs."""
        return self.task_workspaces or TaskWorkspaces.IN_PLACE

    @property
    def notes_on(self) -> bool:
        """Whether the project's agents get the operator's notes for their directory."""
        return self.operator_notes is not OperatorNotesMode.OFF

    @property
    def step_retries(self) -> int:
        """How many times the Planner may retry one step."""
        return DEFAULT_STEP_RETRIES if self.max_step_retries is None else self.max_step_retries

    @property
    def roles_on(self) -> bool:
        """Whether the project's Planners may define the roles their steps run as."""
        return self.planner_roles is PlannerRoles.ON

    @property
    def role_text_limit(self) -> int:
        """How long a role's prompt and its harness may each be."""
        return DEFAULT_ROLE_TEXT_CHARS if self.role_text_max_chars is None else self.role_text_max_chars

    @property
    def gates_on(self) -> bool:
        """Whether the project's Planners may open user gates: unless the project turned them off."""
        return self.user_gates is not UserGates.OFF

    @property
    def gate_reminder_interval_hours(self) -> int:
        """How many hours a gate waits for the user between reminders (0: none)."""
        return DEFAULT_GATE_REMINDER_HOURS if self.gate_reminder_hours is None else self.gate_reminder_hours

    @property
    def room_on(self) -> bool:
        """Whether the agents of the project's tasks share a room: on unless the project turned it off."""
        return self.agent_room is not AgentRoom.OFF

    @property
    def room_limits(self) -> RoomLimits:
        return RoomLimits(**{field.removeprefix("room_"): value for field in ROOM_LIMITS
                             if (value := getattr(self, field)) is not None})

    @property
    def on_task_budget(self) -> TaskBudgetAction:
        return self.task_budget_action or TaskBudgetAction.FAIL

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> ProjectExecution:
        """The settings of a project row; a stored value that no longer validates inherits, with a warning."""
        values: dict[str, Any] = {}
        for field in FIELDS:
            raw = (row or {}).get(field)
            if raw is None:
                continue
            try:
                values[field] = _parse(field, raw)
            except ValueError as exc:
                logger.warning("ignoring project %s's %s: %s", (row or {}).get("id"), field, exc)
        return cls(**values)

    def view(self) -> dict[str, Any]:
        """The fields for the API: plain JSON values, ``None`` where unset."""
        return {field: _stored(getattr(self, field)) for field in FIELDS}


def execution_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """The execution fields in ``data``, validated, as stored: ``None`` (or ``""``) clears one.

    Raises ``ValueError`` with a message for the API's 400.
    """
    return {field: _stored(_parse(field, data[field])) for field in FIELDS if field in data}


def _stored(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value.value if isinstance(value, Enum) else value


def format_usd(amount: float) -> str:
    """A dollar amount as ``--max-budget-usd`` and messages take it: ``0.5``, ``2``, ``0.0125``."""
    return f"{amount:.4f}".rstrip("0").rstrip(".")


def _parse(field: str, raw: Any) -> Any:
    if field == "distill_lessons":
        return _lessons_mode(raw)
    if raw is None or raw == "":
        return None
    if field == "task_budget_usd":
        return budget_usd(raw, field=field)
    if field == "task_budget_action":
        return _choice(field, raw, TaskBudgetAction)
    if field == "review_policy":
        return _choice(field, raw, ReviewPolicy)
    if field == "step_workspaces":
        return _choice(field, raw, StepWorkspaces)
    if field == "workspace_links":
        return _links(raw)
    if field == "task_workspaces":
        return _choice(field, raw, TaskWorkspaces)
    if field == "operator_notes":
        return _choice(field, raw, OperatorNotesMode)
    if field == "planner_roles":
        return _choice(field, raw, PlannerRoles)
    if field == "user_gates":
        return _choice(field, raw, UserGates)
    if field == "agent_room":
        return _choice(field, raw, AgentRoom)
    if field in ROOM_LIMITS:
        low, high, default, what = ROOM_LIMITS[field]
        if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
            raise ValueError(f"{field} ({what}) must be a whole number from {low} to {high}, or null for {default}")
        return raw
    if field == "gate_reminder_hours":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= GATE_REMINDER_MAX_HOURS:
            raise ValueError(f"gate_reminder_hours must be a whole number of hours from 0 (no reminders) to "
                             f"{GATE_REMINDER_MAX_HOURS}, or null for {DEFAULT_GATE_REMINDER_HOURS}")
        return raw
    if field == "role_text_max_chars":
        if isinstance(raw, bool) or not isinstance(raw, int) or not ROLE_TEXT_MIN_CHARS <= raw <= ROLE_TEXT_MAX_CHARS:
            raise ValueError(f"role_text_max_chars must be a whole number from {ROLE_TEXT_MIN_CHARS} to "
                             f"{ROLE_TEXT_MAX_CHARS}, or null for {DEFAULT_ROLE_TEXT_CHARS}")
        return raw
    if field == "max_step_retries":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= STEP_RETRIES_MAX:
            raise ValueError(f"max_step_retries must be a whole number from 0 to {STEP_RETRIES_MAX}, or null for "
                             f"{DEFAULT_STEP_RETRIES}")
        return raw
    if field == "supervise_interval_seconds":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= SUPERVISE_MAX_SECONDS:
            raise ValueError(f"supervise_interval_seconds must be a whole number of seconds from 0 (never) "
                             f"to {SUPERVISE_MAX_SECONDS}, or null to inherit")
        return raw
    raise ValueError(f"unknown execution field {field!r}")


def _lessons_mode(raw: Any) -> LessonsMode:
    """``distill_lessons``: never unset, so null and ``""`` are ``off``; a boolean is from before the modes."""
    if raw is None or raw == "":
        return LessonsMode.OFF
    if isinstance(raw, bool):
        return LessonsMode.WRITE if raw else LessonsMode.OFF
    try:
        return LessonsMode(raw)
    except ValueError:
        raise ValueError("distill_lessons must be one of off, draft, write, or null for off") from None


def _links(raw: Any) -> tuple[str, ...] | None:
    """``workspace_links``: distinct relative paths in the task directory, none inside another; [] is none."""
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
        raise ValueError("workspace_links must be a list of paths relative to the task directory, or null")
    if len(raw) > WORKSPACE_LINKS_MAX:
        raise ValueError(f"workspace_links may have at most {WORKSPACE_LINKS_MAX} paths, not {len(raw)}")
    paths: list[str] = []
    for item in raw:
        text = item.strip()
        parts = text.split("/")
        if not text or len(text) > WORKSPACE_LINK_MAX_CHARS or any(c in text for c in "\0\n\r\\"):
            problem = "must be 1 to 255 characters, without line breaks or backslashes"
        elif text.startswith("/"):
            problem = "must be relative to the task directory"
        elif any(part in ("", ".", "..") for part in parts):
            problem = "must not have empty, . or .. parts"
        elif ".git" in parts:
            problem = "must not be or be in .git"
        elif text in paths:
            problem = "is listed twice"
        else:
            nested = next((other for other in paths if PurePosixPath(text).is_relative_to(other)
                           or PurePosixPath(other).is_relative_to(text)), None)
            if nested is None:
                paths.append(text)
                continue
            problem = f"overlaps {nested!r}: a link cannot be inside another"
        raise ValueError(f"workspace_links: {item!r} {problem}")
    return tuple(paths) or None


def _choice(field: str, raw: Any, kind: type[Enum]) -> Any:
    try:
        return kind(raw)
    except ValueError:
        choices = ", ".join(str(member.value) for member in kind)
        raise ValueError(f"{field} must be one of {choices}, or null to inherit") from None
