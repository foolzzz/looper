"""Concurrency is per task: converting the project-level limits, once.

A project used to limit its tasks: its Planner's limit (a role in the project,
else the Planner's role layer, else 8) was how many of its tasks ran at once,
and its Workers' and Reviewers' limits how many of them ran at once across its
tasks. Now nothing limits how many tasks run, and a task's Workers and
Reviewers are limited in that task only (``agent_settings.resolve_task_concurrency``).
``migrate`` keeps what the new rules can still say:

- a project's own Worker and Reviewer limits become the ``task_limits`` of
  each of its schedules that has none, so every task a schedule creates runs at
  most that many of them (the project's limit was shared by all its tasks; per
  task is the closest the new rule comes);
- a project's Planner limit caps each of its parallel schedules'
  ``max_parallel_runs`` (still 1 to 10), so no more of its runs go at once;

then it clears ``max_concurrency`` from every role-in-a-project layer and from
the Planner's role layer (a layer left with nothing set goes), and the check
constraint takes it on the Workers' and Reviewers' role layers only. A task
created by hand in such a project runs with the per-task defaults. Every
conversion is logged. It runs from ``connection.ensure_schema``, after
``schema.sql`` and in its transaction.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MIGRATION = "concurrency_per_task"
# The Planner's built-in limit before: how many of a project's tasks ran at once when no layer set one.
_PLANNER_BUILTIN = 8
_TASK_ROLES = ("worker", "reviewer")
CONSTRAINT = ("CHECK (max_concurrency IS NULL OR (max_concurrency BETWEEN 1 AND 64 AND scope = 'role' "
              "AND role IN ('worker', 'reviewer')))")


async def migrate(conn: Any) -> None:
    """Once (``schema_migrations`` ``MIGRATION``): convert the project-level limits, clear them, and narrow the
    constraint."""
    if await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name = $1", MIGRATION):
        return
    planner_role = await conn.fetchval(
        "SELECT max_concurrency FROM agent_setting_layers WHERE scope = 'role' AND role = 'planner'")
    projects: dict[str, dict[str, int]] = {}
    for row in await conn.fetch("SELECT project_id, role, max_concurrency FROM agent_setting_layers "
                                "WHERE scope = 'role_project' AND max_concurrency IS NOT NULL"):
        projects.setdefault(row["project_id"], {})[row["role"]] = row["max_concurrency"]
    for schedule in await conn.fetch(
        "SELECT s.id, s.name, s.project_id, s.overlap, s.max_parallel_runs, p.name AS project_name, "
        "EXISTS (SELECT 1 FROM schedule_task_limits l WHERE l.schedule_id = s.id) AS has_limits "
        "FROM schedules s JOIN projects p ON p.id = s.project_id ORDER BY lower(p.name), lower(s.name), s.id"
    ):
        limits = projects.get(schedule["project_id"], {})
        place = f"schedule {schedule['name']!r} of project {schedule['project_name']!r}"
        task_limits = {role: limits[role] for role in _TASK_ROLES if role in limits}
        if task_limits and not schedule["has_limits"]:
            await conn.executemany(
                "INSERT INTO schedule_task_limits (schedule_id, role, max_concurrency) VALUES ($1, $2, $3)",
                [(schedule["id"], role, value) for role, value in task_limits.items()])
            logger.info("concurrency is per task now: %s gives each of its tasks the project's limits %s", place,
                        ", ".join(f"{role} {value}" for role, value in task_limits.items()))
        if schedule["overlap"] == "parallel":
            planner = limits.get("planner") or planner_role or _PLANNER_BUILTIN
            # Both are at least 1 and max_parallel_runs at most 10, so the lower one is within its 1 to 10.
            capped = min(schedule["max_parallel_runs"], planner)
            if capped != schedule["max_parallel_runs"]:
                await conn.execute("UPDATE schedules SET max_parallel_runs = $2 WHERE id = $1", schedule["id"], capped)
                logger.info("concurrency is per task now: %s runs at most %d at once (max_parallel_runs %d), the "
                            "project's Planner limit", place, capped, schedule["max_parallel_runs"])
    cleared = await conn.fetch(
        "UPDATE agent_setting_layers l SET max_concurrency = NULL, updated_at = now() FROM ("
        "  SELECT scope, role, project_id, max_concurrency FROM agent_setting_layers "
        "  WHERE max_concurrency IS NOT NULL AND (scope <> 'role' OR role NOT IN ('worker', 'reviewer'))"
        ") old WHERE l.scope = old.scope AND l.role IS NOT DISTINCT FROM old.role "
        "AND l.project_id IS NOT DISTINCT FROM old.project_id "
        "RETURNING l.scope, l.role, l.project_id, old.max_concurrency, "
        "(SELECT name FROM projects p WHERE p.id = l.project_id) AS project_name")
    for row in cleared:
        where = f"the {row['role']} in project {row['project_name']!r}" if row["project_id"] else \
            f"the {row['role']} role layer"
        logger.info("concurrency is per task now: cleared max_concurrency %d of %s", row["max_concurrency"], where)
        await conn.execute(
            "DELETE FROM agent_setting_layers WHERE scope = $1 AND role IS NOT DISTINCT FROM $2 "
            "AND project_id IS NOT DISTINCT FROM $3 AND harness_prompt IS NULL AND profile_id IS NULL "
            "AND env = '[]'::jsonb AND run_budget_usd IS NULL AND max_concurrency IS NULL AND boundaries IS NULL",
            row["scope"], row["role"], row["project_id"])
    await conn.execute("ALTER TABLE agent_setting_layers DROP CONSTRAINT IF EXISTS "
                       "agent_setting_layers_max_concurrency_check")
    await conn.execute(f"ALTER TABLE agent_setting_layers ADD CONSTRAINT agent_setting_layers_max_concurrency_check "
                       f"{CONSTRAINT}")
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", MIGRATION)
