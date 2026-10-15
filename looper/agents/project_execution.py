"""A project's execution settings: how the agents of its tasks run.

Every field is optional, and ``None`` inherits today's behavior:

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
- ``max_step_retries``: how many times the Planner may retry one step, 0 to
  ``STEP_RETRIES_MAX`` (``DEFAULT_STEP_RETRIES`` when unset).

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
from typing import Any

from ..db.projects import EXECUTION_COLUMNS
from .agent_settings import budget_usd

logger = logging.getLogger(__name__)

FIELDS = EXECUTION_COLUMNS
SUPERVISE_MAX_SECONDS = 86_400
DEFAULT_STEP_RETRIES = 2
STEP_RETRIES_MAX = 20


class ReviewPolicy(str, Enum):
    PLANNER = "planner"
    ALL = "all"


class TaskBudgetAction(str, Enum):
    WRAP_UP = "wrap_up"
    FAIL = "fail"


class StepWorkspaces(str, Enum):
    SHARED = "shared"
    WORKTREE = "worktree"


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
    max_step_retries: int | None = None

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
    def step_retries(self) -> int:
        """How many times the Planner may retry one step."""
        return DEFAULT_STEP_RETRIES if self.max_step_retries is None else self.max_step_retries

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


def _choice(field: str, raw: Any, kind: type[Enum]) -> Any:
    try:
        return kind(raw)
    except ValueError:
        choices = ", ".join(str(member.value) for member in kind)
        raise ValueError(f"{field} must be one of {choices}, or null to inherit") from None
