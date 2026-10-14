"""A project's execution settings: how the agents of its tasks run.

Every field is optional, and ``None`` inherits today's behavior:

- ``model``: the model of the project's Planners, Workers and Reviewers, over
  the agent's own model and the default in Settings (the Manager spans every
  project and keeps its own);
- ``run_budget_usd``: ``--max-budget-usd`` on every Planner, Worker and Reviewer
  Claude Code process of the project's tasks;
- ``task_budget_usd``: the most a task may spend over all its runs, counted
  like the task's cost (``llm_records.task_costs``);
- ``task_budget_action``: what happens when a task reaches that cap: the task
  fails at once (``fail``, also when unset) or after one more Planner turn that
  sums up what was done (``wrap_up``);
- ``supervise_interval_seconds``: how often the Planner checks running Workers
  (0 = never), over the ``planner`` agent config;
- ``review_policy``: ``planner`` (the Planner decides which steps a Reviewer
  checks) or ``all`` (every step needs a passing review before it is accepted).

The columns live on ``projects`` (``db.projects.EXECUTION_COLUMNS``); this
module owns their types and validation. Agents read them when a run or turn
starts, so a change reaches the next one.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db.projects import EXECUTION_COLUMNS
from .agent_settings import SettingsError, model_name

logger = logging.getLogger(__name__)

FIELDS = EXECUTION_COLUMNS
BUDGET_MAX_USD = 100_000
SUPERVISE_MAX_SECONDS = 86_400


class ReviewPolicy(str, Enum):
    PLANNER = "planner"
    ALL = "all"


class TaskBudgetAction(str, Enum):
    WRAP_UP = "wrap_up"
    FAIL = "fail"


@dataclass(frozen=True)
class ProjectExecution:
    model: str | None = None
    run_budget_usd: float | None = None
    task_budget_usd: float | None = None
    task_budget_action: TaskBudgetAction | None = None
    supervise_interval_seconds: int | None = None
    review_policy: ReviewPolicy | None = None

    @property
    def review_all(self) -> bool:
        return self.review_policy is ReviewPolicy.ALL

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
    if raw is None or raw == "":
        return None
    if field == "model":
        try:
            return model_name(raw) or None
        except SettingsError as exc:
            raise ValueError(str(exc)) from None
    if field in ("run_budget_usd", "task_budget_usd"):
        return _dollars(field, raw)
    if field == "task_budget_action":
        return _choice(field, raw, TaskBudgetAction)
    if field == "review_policy":
        return _choice(field, raw, ReviewPolicy)
    if field == "supervise_interval_seconds":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= SUPERVISE_MAX_SECONDS:
            raise ValueError(f"supervise_interval_seconds must be a whole number of seconds from 0 (never) "
                             f"to {SUPERVISE_MAX_SECONDS}, or null to inherit")
        return raw
    raise ValueError(f"unknown execution field {field!r}")


def _dollars(field: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"{field} must be a number of US dollars, or null for no cap")
    amount = float(raw)
    if not 0 < amount <= BUDGET_MAX_USD or round(amount, 4) != amount:
        raise ValueError(f"{field} must be more than 0 and at most {BUDGET_MAX_USD} US dollars, with at most "
                         f"4 decimals; got {raw}")
    return amount


def _choice(field: str, raw: Any, kind: type[Enum]) -> Any:
    try:
        return kind(raw)
    except ValueError:
        choices = ", ".join(str(member.value) for member in kind)
        raise ValueError(f"{field} must be one of {choices}, or null to inherit") from None
