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
