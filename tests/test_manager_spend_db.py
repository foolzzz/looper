"""Each Manager's runs and spend (scenario Managers RFC §6, §10): the Manager role counts every Manager's runs, and
``manager_spend`` and ``/api/managers`` split them per Manager. Needs LOOPER_TEST_DATABASE_URL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_task_agents_db import _call
from tests.test_usage_api_db import _task

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _record(pool: Any, agent_id: str, cost: float | None, *, ago: timedelta = timedelta(0),
                  task_id: str | None = None) -> None:
    await pool.execute(
        "INSERT INTO llm_records (agent_id, task_id, status, cost_usd, created_at) "
        "VALUES ($1, $2, 'completed', $3, $4)", agent_id, task_id, cost, datetime.now(timezone.utc) - ago)


def test_manager_role_counts_every_managers_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        for agent in ("manager", "manager-review", "planner-t1"):
            await _record(pool, agent, 1.0)
        rows = await llm_records.list_records(role="manager", pool=pool)
        assert sorted(r["agent_id"] for r in rows) == ["manager", "manager-review"]
        rows = await llm_records.list_records(agent_id="manager-review", pool=pool)
        assert [r["agent_id"] for r in rows] == ["manager-review"]

    run_db(test, monkeypatch)


def test_task_costs_reports_any_managers_run_as_the_managers(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import llm_records, projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        await _task(pool, "task-a", project["id"])
        await _record(pool, "manager", 1.0, task_id="task-a")
        await _record(pool, "manager-review", 2.0, task_id="task-a")
        await _record(pool, "planner-task-a", 4.0, task_id="task-a")
        cost = (await llm_records.task_costs(["task-a"], pool=pool))["task-a"]
        assert cost["cost_usd"] == pytest.approx(4.0)
        assert sum(m["cost_usd"] for m in cost["manager_by_model"]) == pytest.approx(3.0)

    run_db(test, monkeypatch)


def test_manager_spend_sums_per_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.overview import local_midnight
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        since = local_midnight()
        await _record(pool, "manager", 1.0)
        await _record(pool, "manager", None)
        await _record(pool, "manager-review", 2.0)
        await _record(pool, "manager-review", 4.0, ago=timedelta(days=1) + (datetime.now(timezone.utc) - since))
        await _record(pool, "manager-review", 8.0, ago=timedelta(days=8))
        spend = await llm_records.manager_spend(["manager", "manager-review", "manager-none"], since=since, pool=pool)
        assert spend["manager"] == {"today_usd": 1.0, "last_7d_usd": 1.0, "runs_today": 2}
        assert spend["manager-review"] == {"today_usd": 2.0, "last_7d_usd": 6.0, "runs_today": 1}
        assert spend["manager-none"] == {"today_usd": 0.0, "last_7d_usd": 0.0, "runs_today": 0}

    run_db(test, monkeypatch)


def test_api_managers_has_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        status, _ = await _call("POST", "/api/managers", {"key": "review", "name": "Review", "system_prompt": "x"})
        assert status == 201
        await _record(pool, "manager-review", 2.5)
        status, answer = await _call("GET", "/api/managers")
        assert status == 200
        by_id = {m["id"]: m for m in answer["managers"]}
        assert by_id["manager-review"]["spend"] == {"today_usd": 2.5, "last_7d_usd": 2.5, "runs_today": 1}
        assert by_id["manager"]["spend"] == {"today_usd": 0.0, "last_7d_usd": 0.0, "runs_today": 0}

    run_db(test, monkeypatch)
