"""The Manager's task read keeps its old shape until explorers have actually run."""

from __future__ import annotations

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_manager_task_cost_split_is_present_only_with_explorer_runs(monkeypatch):
    from looper.agents.ops.tasks import _task_view
    from looper.db import llm_records, projects

    async def test(pool):
        project = await projects.create_project(name="Cost fixture", pool=pool)
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task-cost', $1, 'Inspect the fixture.', 'completed')", project["id"])
        for kind, cost in (("planner", 0.1), ("worker", 0.2), ("reviewer", 0.3)):
            record = await llm_records.start_record(agent_id=f"{kind}-fixture", task_id="task-cost", pool=pool)
            await pool.execute("UPDATE llm_records SET status='completed', cost_usd=$2, finished_at=now() "
                               "WHERE id=$1", record, cost)
        before = await _task_view("task-cost")
        assert before["cost_usd"] == 0.6
        assert "cost_by_role" not in before
        await pool.execute("UPDATE projects SET subagents='on' WHERE id=$1", project["id"])
        assert await _task_view("task-cost") == before

        record = await llm_records.start_record(agent_id="explorer-fixture", run_kind="explorer",
                                               task_id="task-cost", pool=pool)
        await pool.execute("UPDATE llm_records SET status='completed', cost_usd=0.05, finished_at=now() "
                           "WHERE id=$1", record)
        with_explorer = await _task_view("task-cost")
        split = with_explorer.pop("cost_by_role")
        assert split == {"planner": {"cost_usd": 0.1, "runs": 1}, "worker": {"cost_usd": 0.2, "runs": 1},
                         "reviewer": {"cost_usd": 0.3, "runs": 1}, "explorer": {"cost_usd": 0.05, "runs": 1}}
        assert with_explorer.pop("cost_usd") == 0.65
        assert with_explorer == {key: value for key, value in before.items() if key != "cost_usd"}
        await pool.execute("UPDATE projects SET subagents='off' WHERE id=$1", project["id"])
        assert (await _task_view("task-cost"))["cost_by_role"] == split

    run_db(test, monkeypatch)
