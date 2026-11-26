"""The shared read-only task spend gate; callers own warnings and stopping."""
from __future__ import annotations

from dataclasses import dataclass

from ..db import llm_records as db_llm_records, projects as db_projects
from .project_execution import ProjectExecution
from .run_usage import CostStatus


@dataclass(frozen=True)
class Gate:
    allowed: bool
    spent: float
    cap: float | None
    unknown_models: tuple[str, ...] = ()


async def gate(task_id: str, *, execution: ProjectExecution | None = None) -> Gate:
    """Read current project settings, or use the Planner's already-read settings.

    Uncapped tasks need no cost query. Read failures propagate so each caller
    can retain its existing warning and fallback behavior.
    """
    if execution is None:
        project = await db_projects.get_task_project(task_id)
        execution = ProjectExecution.from_row(project).overlaid((project or {}).get("task_workflow"))
    cap = execution.task_budget_usd
    if cap is None:
        return Gate(True, 0.0, None)
    costs = (await db_llm_records.task_costs([task_id]))[task_id]
    spent = float(costs["cost_usd"])
    unknown = ()
    if costs.get("cost_status") == CostStatus.UNKNOWN.value:
        unknown = tuple(sorted(costs.get("unknown_models", [
            str(row["model"]) for row in costs.get("by_model", []) if row["unpriced_runs"] and any(
                row.get(key, 0) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))])))
    return Gate(spent < cap, spent, cap, unknown)
