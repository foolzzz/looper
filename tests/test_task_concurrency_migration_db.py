"""The upgrade to per-task concurrency on a real PostgreSQL: a project's limits become its schedules' task limits and
a cap on its parallel schedules, then they are cleared, once.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database, in a
new schema of its own, dropped afterwards. The database before the upgrade is
this schema with the old check constraint (a role in a project and the Planner
may hold a limit) and without the upgrade's ``schema_migrations`` row.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tests.test_catalog_db import DSN, _fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_OLD_CONSTRAINT = """
DELETE FROM schema_migrations WHERE name = 'concurrency_per_task';
ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_max_concurrency_check;
ALTER TABLE agent_setting_layers ADD CONSTRAINT agent_setting_layers_max_concurrency_check CHECK (max_concurrency IS NULL
    OR (max_concurrency BETWEEN 1 AND 64 AND scope IN ('role', 'role_project') AND role <> 'manager'));
"""

# alarm: the live case (Planner 3, Worker 8, Reviewer 3), with its parallel schedule and a disabled one, and one
# schedule that has task limits of its own already. big: its Planner limit only, under a parallel schedule's runs.
# plain: no limits of its own, so the Planner's role layer (or the old built-in 8) caps its parallel schedule.
_BEFORE = """
INSERT INTO projects (id, name) VALUES ('proj-alarm', 'alarm'), ('proj-big', 'big'), ('proj-plain', 'plain');
INSERT INTO agent_setting_layers (scope, role, project_id, harness_prompt, max_concurrency) VALUES
    ('role', 'worker', NULL, NULL, 5),
    ('role_project', 'planner', 'proj-alarm', NULL, 3),
    ('role_project', 'worker', 'proj-alarm', 'Alarm workers.', 8),
    ('role_project', 'reviewer', 'proj-alarm', NULL, 3),
    ('role_project', 'planner', 'proj-big', NULL, 2);
INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, overlap, max_parallel_runs, enabled,
                       next_fire_at) VALUES
    ('sched-triage', 'proj-alarm', 'alarm-triage', '* * * * *', 'UTC', 'triage', 'parallel', 3, true, now()),
    ('sched-warning', 'proj-alarm', 'alarm-triage-warning', '* * * * *', 'UTC', 'warn', 'skip', 3, false, NULL),
    ('sched-own', 'proj-alarm', 'alarm-own', '0 * * * *', 'UTC', 'own', 'skip', 3, false, NULL),
    ('sched-big', 'proj-big', 'big-runs', '* * * * *', 'UTC', 'big', 'parallel', 5, false, NULL),
    ('sched-plain', 'proj-plain', 'plain-runs', '* * * * *', 'UTC', 'plain', 'parallel', 10, false, NULL);
INSERT INTO schedule_task_limits (schedule_id, role, max_concurrency) VALUES ('sched-own', 'reviewer', 1);
"""


async def _state(pool: Any) -> dict[str, Any]:
    return {
        "task_limits": {(r["schedule_id"], r["role"]): r["max_concurrency"] for r in await pool.fetch(
            "SELECT schedule_id, role, max_concurrency FROM schedule_task_limits")},
        "max_parallel_runs": {r["id"]: r["max_parallel_runs"] for r in await pool.fetch(
            "SELECT id, max_parallel_runs FROM schedules")},
        "layers": {(r["scope"], r["role"], r["project_id"]): (r["harness_prompt"], r["max_concurrency"])
                   for r in await pool.fetch("SELECT scope, role, project_id, harness_prompt, max_concurrency "
                                             "FROM agent_setting_layers")},
    }


@pytest.mark.parametrize(("planner_role", "plain_cap"), [(4, 4), (None, 8)])
def test_the_upgrade_converts_a_projects_limits_clears_them_and_a_second_run_changes_nothing(
    caplog: pytest.LogCaptureFixture, planner_role: int | None, plain_cap: int,
) -> None:
    import asyncpg

    from looper.agents import schedules
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            await pool.execute(_OLD_CONSTRAINT + _BEFORE)
            if planner_role is not None:
                await pool.execute("INSERT INTO agent_setting_layers (scope, role, max_concurrency) VALUES "
                                   "('role', 'planner', $1)", planner_role)

            with caplog.at_level(logging.INFO, logger="looper.db.task_concurrency"):
                await connection.ensure_schema(pool)
            converted = await _state(pool)
            assert converted["task_limits"] == {
                # The project's Worker and Reviewer limits, on each of its schedules that had none, enabled or not.
                ("sched-triage", "worker"): 8, ("sched-triage", "reviewer"): 3,
                ("sched-warning", "worker"): 8, ("sched-warning", "reviewer"): 3,
                ("sched-own", "reviewer"): 1,  # it had its own: kept as it was
            }
            # A parallel schedule runs no more at once than its project's Planner limit let run.
            assert converted["max_parallel_runs"] == {"sched-triage": 3, "sched-warning": 3, "sched-own": 3,
                                                      "sched-big": 2, "sched-plain": plain_cap}
            # Only the Workers' role layer keeps a limit; a layer left with nothing else set is gone.
            assert converted["layers"] == {("role", "worker", None): (None, 5),
                                           ("role_project", "worker", "proj-alarm"): ("Alarm workers.", None)}
            logged = [r.getMessage() for r in caplog.records if r.name == "looper.db.task_concurrency"]
            assert "concurrency is per task now: schedule 'alarm-triage' of project 'alarm' gives each of its tasks " \
                   "the project's limits worker 8, reviewer 3" in logged
            assert "concurrency is per task now: schedule 'big-runs' of project 'big' runs at most 2 at once " \
                   "(max_parallel_runs 5), the project's Planner limit" in logged
            assert "concurrency is per task now: cleared max_concurrency 3 of the planner in project 'alarm'" in logged
            # Two schedules' task limits, two caps, and each layer cleared (the Planner's role layer when it had one).
            assert len(logged) == 2 + 2 + 4 + (1 if planner_role else 0)

            # The schedule views carry them, both roles named.
            answer = await _view(pool, "sched-triage")
            assert answer["task_limits"] == {"worker": 8, "reviewer": 3}
            # The constraint takes a limit on the Workers' and the Reviewers' role layers only now.
            for scope, role, project_id in (("role_project", "worker", "proj-alarm"), ("role", "planner", None)):
                with pytest.raises(asyncpg.CheckViolationError):
                    await pool.execute("INSERT INTO agent_setting_layers (scope, role, project_id, max_concurrency) "
                                       "VALUES ($1, $2, $3, 2) ON CONFLICT (scope, (COALESCE(role, '')), "
                                       "(COALESCE(project_id, ''))) DO UPDATE SET max_concurrency = 2",
                                       scope, role, project_id)

            caplog.clear()
            with caplog.at_level(logging.INFO, logger="looper.db.task_concurrency"):
                await connection.ensure_schema(pool)
            assert await _state(pool) == converted
            assert [r for r in caplog.records if r.name == "looper.db.task_concurrency"] == []

    async def _view(pool: Any, schedule_id: str) -> dict[str, Any]:
        from looper.db import schedules as db_schedules

        return schedules.schedule_view(await db_schedules.get_schedule(schedule_id, pool=pool))

    asyncio.run(_main())


def test_a_new_database_has_the_narrow_constraint() -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            assert await pool.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'concurrency_per_task'") == 1
            await pool.execute("INSERT INTO agent_setting_layers (scope, role, max_concurrency) VALUES "
                               "('role', 'reviewer', 2)")
            await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-a', 'a')")
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("INSERT INTO agent_setting_layers (scope, role, project_id, max_concurrency) "
                                   "VALUES ('role_project', 'reviewer', 'proj-a', 2)")

    asyncio.run(_main())
