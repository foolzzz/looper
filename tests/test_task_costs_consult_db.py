"""Room consult spend is part of a task's cost, and of the budgets that read it.

A task with no consult row stays byte-identical to task_costs at
0cda75a9a937439c04f58de0826a9c22ab42a428 (that query is unchanged through
a6edcd784146a3c4128cb2cebcaa3f2a00b5737d), including key order.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents.api_v1 import validate_arguments
from looper.agents.v1_domains.costs import TASK_COST
from looper.db import llm_records
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Captured from task_costs before consult spend was included. json.dumps keeps key order.
RECORDED_AT = "0cda75a9a937439c04f58de0826a9c22ab42a428"
RECORDED_COST = (
    '{"cost_usd": 0.4, "runs": 5, "running": 0, "unpriced_runs": 0, "equivalent_runs": 0, '
    '"provider_price_runs": 0, "by_role": {"planner": {"cost_usd": 0.14, "runs": 2}, '
    '"worker": {"cost_usd": 0.2, "runs": 1}, "reviewer": {"cost_usd": 0.05, "runs": 1}, '
    '"explorer": {"cost_usd": 0.01, "runs": 1}}, "by_model": ['
    '{"model": "worker-model", "cost_usd": 0.2, "runs": 1, "running": 0, "unpriced_runs": 0, '
    '"interrupted_runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, '
    '"cache_creation_tokens": 0, "cache_reported_runs": 0, "cache_missing_runs": 1, '
    '"cache_hit_prompt_tokens": 0, "cache_hit_read_tokens": 0}, '
    '{"model": "planner-model", "cost_usd": 0.1, "runs": 1, "running": 0, "unpriced_runs": 0, '
    '"interrupted_runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, '
    '"cache_creation_tokens": 0, "cache_reported_runs": 0, "cache_missing_runs": 1, '
    '"cache_hit_prompt_tokens": 0, "cache_hit_read_tokens": 0}, '
    '{"model": "reviewer-model", "cost_usd": 0.05, "runs": 1, "running": 0, "unpriced_runs": 0, '
    '"interrupted_runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, '
    '"cache_creation_tokens": 0, "cache_reported_runs": 0, "cache_missing_runs": 1, '
    '"cache_hit_prompt_tokens": 0, "cache_hit_read_tokens": 0}, '
    '{"model": "advisor-model", "cost_usd": 0.04, "runs": 1, "running": 0, "unpriced_runs": 0, '
    '"interrupted_runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, '
    '"cache_creation_tokens": 0, "cache_reported_runs": 0, "cache_missing_runs": 1, '
    '"cache_hit_prompt_tokens": 0, "cache_hit_read_tokens": 0}, '
    '{"model": "explorer-model", "cost_usd": 0.01, "runs": 1, "running": 0, "unpriced_runs": 0, '
    '"interrupted_runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, '
    '"cache_creation_tokens": 0, "cache_reported_runs": 0, "cache_missing_runs": 1, '
    '"cache_hit_prompt_tokens": 0, "cache_hit_read_tokens": 0}], "manager_by_model": []}'
)


async def _finish(pool, agent, task, cost, model, *, run_kind=None, metadata=None):
    record = await llm_records.start_record(
        agent_id=agent, task_id=task, run_kind=run_kind, metadata=metadata, pool=pool)
    await llm_records.finish_record(
        record, status="completed", cost_usd=cost, model=model, pool=pool)
    return record


def test_spend_role_counts_consult_without_changing_the_run_listing_filter():
    assert RECORDED_AT == "0cda75a9a937439c04f58de0826a9c22ab42a428"
    branches = " ".join(
        f"WHEN {llm_records._ROLE_FILTERS[role]} THEN '{role}'" for role in llm_records.TASK_ROLES)
    consult = f"WHEN {llm_records.RUN_KIND} = 'consult' THEN 'consult'"
    assert llm_records._TASK_ROLE.endswith(f"{branches} END")
    assert consult not in llm_records._TASK_ROLE
    assert llm_records.TASK_RUN == f"{llm_records._TASK_ROLE} IS NOT NULL"
    assert consult in llm_records._SPEND_ROLE
    assert llm_records._SPEND_ROLE.index(consult) < llm_records._SPEND_ROLE.index(f"{branches} END")
    assert llm_records.TASK_SPEND == f"{llm_records._SPEND_ROLE} IS NOT NULL"


def test_a_task_without_consults_keeps_the_recorded_cost():
    async def check(pool):
        task = "fixture-task"
        await _finish(pool, "planner-fixture", task, 0.10, "planner-model")
        await _finish(pool, "worker-fixture", task, 0.20, "worker-model")
        await _finish(pool, "reviewer-fixture", task, 0.05, "reviewer-model")
        await _finish(pool, "advisor-fixture", task, 0.04, "advisor-model", run_kind="advisor")
        await _finish(pool, "explorer-fixture", task, 0.01, "explorer-model", run_kind="explorer")
        costs = await llm_records.task_costs([task, "empty-task"], pool=pool)
        assert json.dumps(costs[task]) == RECORDED_COST
        assert list(costs[task]["by_role"]) == ["planner", "worker", "reviewer", "explorer"]
        assert "consult" not in costs[task]["by_role"]
        empty = costs["empty-task"]
        assert list(empty["by_role"]) == ["planner", "worker", "reviewer"]
        assert empty["cost_usd"] == 0 and empty["runs"] == 0
    run_db(check)


def test_a_room_consult_is_in_the_task_cost_and_can_cross_its_budget():
    async def check(pool):
        task = "consult-task"
        await _finish(pool, "worker-consult-task", task, 0.10, "worker-model")
        priced = await _finish(pool, "consult-abcdef012345", task, 0.05, "consult-model")
        # A row recorded before run_kind was stored still counts by its agent id.
        await pool.execute("UPDATE llm_records SET run_kind = NULL WHERE id = $1", priced)
        await _finish(pool, "consult-unpriced0001", task, None, "consult-gap")
        await _finish(pool, "consult-oauth0000001", task, 0.02, "consult-oauth",
                      metadata={"profile": {"provider": {"auth": "oauth"}}})
        await _finish(pool, "manager", task, 5, "manager-model")
        cost = (await llm_records.task_costs([task], pool=pool))[task]
        assert cost["cost_usd"] == 0.17
        assert cost["runs"] == 4
        assert cost["unpriced_runs"] == 1
        assert cost["equivalent_runs"] == 1
        assert cost["by_role"]["consult"] == {"cost_usd": 0.07, "runs": 3}
        assert list(cost["by_role"]) == ["planner", "worker", "reviewer", "consult"]
        assert {row["model"] for row in cost["by_model"]} >= {"consult-model", "consult-gap", "consult-oauth"}
        assert sum(row["cost_usd"] or 0 for row in cost["by_model"]) == cost["cost_usd"]
        assert cost["manager_by_model"][0]["cost_usd"] == 5
        # The Planner stops when task_costs cost_usd is at least the cap. 0.10 alone would not.
        cap = 0.12
        assert cost["cost_usd"] >= cap
        assert cost["cost_usd"] - cost["by_role"]["consult"]["cost_usd"] < cap
        validate_arguments(TASK_COST, cost)
        runs = await llm_records.task_run_rows(task, pool=pool)
        listed = await llm_records.process_record_runs(task, pool=pool)
        assert [row["agent_id"] for row in runs] == ["worker-consult-task"]
        assert [row["role"] for row in runs] == ["worker"]
        assert [row["id"] for row in listed] == [row["id"] for row in runs]
    run_db(check)


def test_an_explicit_consult_kind_wins_over_a_worker_id_prefix():
    async def check(pool):
        task = "prefix-task"
        await _finish(pool, "worker-real", task, 0.10, "worker-model")
        await _finish(pool, "worker-disguised", task, 0.03, "consult-model", run_kind="consult")
        cost = (await llm_records.task_costs([task], pool=pool))[task]
        assert cost["cost_usd"] == 0.13
        assert cost["by_role"]["worker"] == {"cost_usd": 0.10, "runs": 1}
        assert cost["by_role"]["consult"] == {"cost_usd": 0.03, "runs": 1}
    run_db(check)


def test_schedule_spend_includes_a_running_tasks_consult():
    async def check(pool):
        from looper.db.projects import create_project
        from looper.db.schedules import spent_since_each

        project = await create_project(name="Consult budget", pool=pool)
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status) VALUES ('sched-task', $1, 'Run', 'running')",
            project["id"])
        await _finish(pool, "worker-sched", "sched-task", 0.10, "worker-model")
        await _finish(pool, "consult-sched000001", "sched-task", 0.05, "consult-model")
        await _finish(pool, "manager", "sched-task", 5, "manager-model")
        fired = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        await pool.execute(
            "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
            "VALUES ('sch', $1, 'nightly', '0 0 * * *', 'UTC', 'do the thing')",
            project["id"])
        await pool.execute(
            "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, task_id) "
            "VALUES ('run', 'sch', $1, $1, 'scheduled', 'running', 'sched-task')",
            fired)
        cost = (await llm_records.task_costs(["sched-task"], pool=pool))["sched-task"]
        spent = await spent_since_each(pool, ["sch"], fired - timedelta(seconds=1))
        assert cost["cost_usd"] == 0.15
        assert spent["sch"] == cost["cost_usd"]
        assert cost["by_role"]["consult"]["cost_usd"] == 0.05
    run_db(check)


def test_explorer_admission_refuses_when_consult_spend_uses_the_rest(tmp_path, monkeypatch):
    from looper.agents import subagents
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.spawn import SpawnParent

    async def check(pool):
        from looper.db.projects import create_project

        project = await create_project(name="Exploration", pool=pool)
        await pool.execute(
            "UPDATE projects SET subagents = 'on', task_budget_usd = 0.20 WHERE id = $1", project["id"])
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status) VALUES ('t', $1, 'Inspect source', 'running')",
            project["id"])
        parent_record = await llm_records.start_record(agent_id="worker-parent", task_id="t", pool=pool)
        await _finish(pool, "worker-spent", "t", 0.05, "worker-model")
        await _finish(pool, "consult-aabbccddeeff", "t", 0.10, "consult-model")
        monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=AgentSettings(
            profile=builtin_resolved_profile())))
        parent = subagents.Spawner(SpawnParent("t", record_id=parent_record), "worker", tmp_path)
        service = subagents.SpawnService(tmp_path / "runtime", pool=pool)
        brief = {"goal": "Find the parser", "deliverable": "Paths and evidence", "scope": ["."]}
        # Without the consult, 0.15 remains, which admits. With it, 0.05 remains, under the 0.10 minimum.
        with pytest.raises(subagents.Refused) as refused:
            await service.admit(parent, brief, key="blocked", budget_usd=0.15)
        assert refused.value.reason is subagents.RefusalReason.BUDGET
        await pool.execute("DELETE FROM llm_records WHERE agent_id = 'consult-aabbccddeeff'")
        admitted = await service.admit(parent, brief, key="after", budget_usd=0.15)
        assert admitted["budget_usd"] == 0.15
    run_db(check, monkeypatch)


def _money(row: dict) -> float:
    return float(row["provider_priced_usd"]) + float(row["equivalent_usd"])


def test_schedule_spend_uses_the_live_task_cost_when_a_settlement_left_out_a_consult():
    """A finished run with no consult keeps its recorded cost. A consult already in the settlement is not added again."""
    async def check(pool):
        from looper.db.projects import create_project
        from looper.db.schedules import spent_since_each

        project = await create_project(name="Settled", pool=pool)
        fired = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status) VALUES "
            "('frozen-task', $1, 'Run', 'completed'), ('included-task', $1, 'Run', 'completed')",
            project["id"])
        await pool.execute(
            "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) VALUES "
            "('frozen', $1, 'frozen', '0 0 * * *', 'UTC', 'do the thing'), "
            "('included', $1, 'included', '0 1 * * *', 'UTC', 'do the thing')",
            project["id"])
        await _finish(pool, "worker-frozen", "frozen-task", 0.10, "worker-model")
        await _finish(pool, "worker-frozen-later", "frozen-task", 0.04, "worker-model")
        await _finish(pool, "worker-included", "included-task", 0.10, "worker-model")
        await _finish(pool, "consult-included0001", "included-task", 0.05, "consult-model")
        await pool.execute(
            "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, task_id, cost_usd) "
            "VALUES ('frozen-run', 'frozen', $1, $1, 'scheduled', 'completed', 'frozen-task', 0.10), "
            "('included-run', 'included', $1, $1, 'scheduled', 'completed', 'included-task', 0.15)",
            fired)
        since = fired - timedelta(seconds=1)
        spent = await spent_since_each(pool, ["frozen", "included"], since)
        assert spent["frozen"] == 0.10
        assert spent["included"] == 0.15
        await pool.execute("UPDATE schedule_runs SET cost_usd = 0.10 WHERE id = 'included-run'")
        assert (await spent_since_each(pool, ["included"], since))["included"] == 0.15
    run_db(check)


def test_a_settled_consult_stops_the_next_schedule_run_at_the_daily_budget(monkeypatch, tmp_path):
    from tests.schedule_support import run_scheduled, with_sender
    from tests.test_schedule_gate_budget_alerts_db import _fire

    async def test(env):
        from looper.db.schedules import spent_since

        with_sender(env)
        sid = (await env.enabled(daily_budget_usd=0.15))["schedule_id"]
        first = await _fire(env, sid)
        task_id = first["task_id"]
        await _finish(env.pool, f"worker-{task_id}", task_id, 0.10, "worker-model")
        await env.end_task(task_id, "completed")
        await _finish(env.pool, "consult-schedcap0001", task_id, 0.05, "consult-model")
        settled = next(run for run in await env.runs(sid) if run["task_id"] == task_id)
        cost = (await llm_records.task_costs([task_id], pool=env.pool))[task_id]["cost_usd"]
        since = env.scheduler.day_start(env.clock.now)
        assert settled["cost_usd"] == 0.10
        assert cost == 0.15
        assert await spent_since(env.pool, sid, since) == 0.15

        skipped = await _fire(env, sid)
        assert (skipped["status"], skipped["task_id"]) == ("skipped_budget", None)
        assert "daily budget is $0.15" in skipped["error"]
        assert env.bus.of("task_start") == [task_id]
        assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE schedule_id = $1", sid) == 1

        await env.pool.execute("UPDATE schedule_runs SET cost_usd = 0.15 WHERE task_id = $1", task_id)
        assert await spent_since(env.pool, sid, since) == 0.15

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_projects_total_includes_a_room_consult_the_way_its_task_total_does():
    async def check(pool):
        from looper.db.projects import create_project

        grouped = await create_project(name="Grouped", pool=pool)
        other = await create_project(name="Other spend", pool=pool)
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status) VALUES "
            "('grouped-task', $1, 'Run', 'running'), ('other-task', $2, 'Run', 'running')",
            grouped["id"], other["id"])
        await _finish(pool, "worker-grouped", "grouped-task", 0.10, "worker-model")
        await _finish(pool, "consult-grouped0001", "grouped-task", 0.05, "consult-model")
        await _finish(pool, "manager", "grouped-task", 5.0, "manager-model")
        await _finish(pool, "worker-other", "other-task", 0.40, "worker-model")
        assert (await llm_records.task_costs(["grouped-task"], pool=pool))["grouped-task"]["cost_usd"] == 0.15

        project_total = await llm_records.spend_since(None, project_id=grouped["id"], pool=pool)
        assert _money(project_total) == pytest.approx(0.15) and project_total["runs"] == 2
        [grouped_row] = await llm_records.task_spend(None, project_id=grouped["id"], pool=pool)
        assert grouped_row["task_id"] == "grouped-task"
        assert _money(grouped_row) == pytest.approx(0.15) and grouped_row["runs"] == 2
        other_total = await llm_records.spend_since(None, project_id=other["id"], pool=pool)
        assert _money(other_total) == pytest.approx(0.40) and other_total["runs"] == 1
        [other_row] = await llm_records.task_spend(None, project_id=other["id"], pool=pool)
        assert other_row["task_id"] == "other-task"
        assert _money(other_row) == pytest.approx(0.40) and other_row["runs"] == 1
        # No project filter: the global total already included the consult (and the Manager).
        overall = await llm_records.spend_since(None, pool=pool)
        assert _money(overall) == pytest.approx(5.55) and overall["runs"] == 4
    run_db(check)


def test_a_consult_makes_the_planner_stop_at_the_task_cap(tmp_path, monkeypatch):
    async def check(pool):
        from types import SimpleNamespace

        from looper.agents.project_execution import ProjectExecution
        from looper.agents.tiers.planner import PlannerAgent, TaskPlan
        from looper.db.projects import create_project

        project = await create_project(name="Capped", pool=pool)
        await pool.execute(
            "INSERT INTO tasks (id, project_id, requirement, status, phase) "
            "VALUES ('capped-task', $1, 'Run', 'running', 'executing')",
            project["id"])
        await _finish(pool, "worker-capped", "capped-task", 0.10, "worker-model")
        planner = PlannerAgent(task_id="capped-task", runtime_root=tmp_path)
        planner._execution = ProjectExecution(task_budget_usd=0.15)
        planner._bus = SimpleNamespace(publish=AsyncMock())
        plan = TaskPlan(plan_id="plan-capped", goal_id="goal-capped", requirement="Run",
                        work_dir=str(tmp_path), status="executing")
        assert await planner._within_budget(plan) is True
        planner._bus.publish.assert_not_awaited()
        assert plan.status == "executing"
        assert await pool.fetchval("SELECT phase FROM tasks WHERE id = 'capped-task'") == "executing"

        await _finish(pool, "consult-capped00001", "capped-task", 0.05, "consult-model")
        assert await planner._within_budget(plan) is False
        assert plan.status == "failed"
        planner._bus.publish.assert_awaited_once()
        message = planner._bus.publish.await_args.args[0]
        assert message.payload["fail_kind"] == "task_budget"
        assert message.payload["status"] == "failed"
        assert await pool.fetchval("SELECT phase FROM tasks WHERE id = 'capped-task'") == "failed"
    run_db(check, monkeypatch)
