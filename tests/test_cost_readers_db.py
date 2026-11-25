"""Unknown cost stays visible in Manager summaries and advisor settlement."""
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import advisor, advisor_alerts, metrics
from looper.agents.model_call import Execution
from looper.db import agent_spawns, llm_records, manager_events, projects
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("cost", [None, 0., .05])
def test_manager_summaries_keep_unknown_cost_separate_from_zero(monkeypatch, cost):
    async def check(pool):
        project = await projects.create_project(name="cost readers", pool=pool)
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task', $1, 'work', 'running')", project["id"])
        record = await llm_records.start_record(agent_id="manager", pool=pool)
        await llm_records.finish_record(record, status="completed", model="claude-haiku-5-5", cost_usd=cost, pool=pool)
        event = await manager_events.record(manager_id="manager", task_id="task", origin="chat", target_kind="web",
                                           channel_id="web", chat="main", dedupe_key="one", payload={}, fallback=[],
                                           state="skipped", reason="off", pool=pool)
        await pool.execute("UPDATE manager_events SET record_id=$2 WHERE id=$1", event.row["id"], record)
        spend = (await llm_records.manager_spend(["manager", "never"],
                 since=datetime.now(timezone.utc) - timedelta(hours=1), pool=pool))
        expected = "unknown" if cost is None else "known"
        assert spend["manager"]["cost_status"] == expected
        assert spend["manager"]["unpriced_runs"] == int(cost is None)
        assert spend["never"]["cost_status"] == "known"
        summary = await manager_events.summary(pool=pool)
        assert summary["cost_status"] == expected and summary["unpriced_runs"] == int(cost is None)
        assert summary["turn_cost_usd"] == (cost or 0.)
    run_db(check)


def test_advisor_unknown_booking_is_durable_idempotent_and_alerts(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(agent_spawns, "get_pool", lambda: pool)
        monkeypatch.setattr(llm_records, "get_pool", lambda: pool)
        monkeypatch.setattr(advisor_alerts, "get_pool", lambda: pool)
        from looper.db import system_settings
        monkeypatch.setattr(system_settings, "get_pool", lambda: pool)
        project = await projects.create_project(name="cost readers", pool=pool)
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task', $1, 'work', 'running')", project["id"])
        spawn = await agent_spawns.create(kind="advisor", task_id="task", step_id=None, parent_record_id=None,
                                         brief={}, inputs={}, budget_usd=1)
        record = await llm_records.start_record(agent_id="advisor-test", task_id="task", pool=pool)
        await llm_records.finish_record(record, status="completed", model="claude-haiku-5-5", pool=pool)
        await agent_spawns.note_run(spawn["id"], record)
        await advisor._book(spawn["id"], Execution("answer", record_id=record, cost_usd=None))
        row = await agent_spawns.get(spawn["id"])
        assert row["cost_status"] == "unknown"
        assert row["result"]["booked"] == [record]
        assert row["result"]["unpriced_runs"] == [record]
        from looper.db import advisor_advice
        assert (await advisor_advice.consults_view("task", pool=pool))[0]["cost_status"] == "unknown"
        assert await advisor_advice.summary_line("task", pool=pool) == (
            "请教 advisor 1 次，花费 0.00 美元，采纳 0 条，其中 0 条帮步骤通过")
        due = await advisor_alerts._due("task")
        assert any(kind == "spend" and "未知" in text for kind, text in due)
        known = await llm_records.start_record(agent_id="advisor-known", task_id="task", pool=pool)
        await advisor._book(spawn["id"], Execution("answer", record_id=known, cost_usd=0.05))
        row = await agent_spawns.get(spawn["id"])
        assert row["spent_usd"] == 0.05 and row["cost_status"] == "unknown"
        await advisor._book(spawn["id"], Execution("answer", record_id=known, cost_usd=0.05))
        assert await agent_spawns.get(spawn["id"]) == row
    run_db(check)


def test_cost_reads_are_quiet_and_always_carry_status(caplog):
    async def check(pool):
        await llm_records.start_record(agent_id="worker-test", task_id="task", pool=pool)
        failed = await llm_records.start_record(agent_id="worker-failed", task_id="task", pool=pool)
        await llm_records.finish_record(failed, status="failed", pool=pool)
        for _ in range(2):
            costs = await llm_records.task_costs(["task", "empty"], pool=pool)
            assert costs["empty"]["cost_status"] == "known"
            assert costs["task"]["cost_status"] == "unknown"
            assert costs["task"]["unknown_models"] == []
            assert all("cost_status" in row for row in costs["task"]["by_model"])
        assert not caplog.records
        assert (await llm_records.spend_since(None, pool=pool))["cost_status"] == "unknown"
        assert metrics.Cost().view()["equivalent"]["cost_status"] == "known"
    run_db(check)
