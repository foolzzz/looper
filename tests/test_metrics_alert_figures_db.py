"""Alert-only figures preserve the public baseline with explicit cost coverage."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import pytest

from looper.agents import metrics
from looper.db import metrics as db_metrics
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BASELINE = "c53b056de1999ef41d3f8fdac0f1c3c01975e2fb"
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('quality','Synthetic project')")
    await pool.execute("INSERT INTO tasks(id,requirement,status,project_id) "
                       "VALUES ('current','Synthetic task','running','quality'),"
                       "('previous','Synthetic task','completed','quality'),"
                       "('ongoing','Synthetic task','running','quality')")
    for task, days in (("current", 1), ("previous", 10)):
        moment = NOW - timedelta(days=days)
        for index in range(3):
            attempt_id = await pool.fetchval(
                "INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger,decision,started_at,decided_at) "
                "VALUES ($1,$2,1,1,'first','accept',$3,$3) RETURNING id", task, str(index), moment)
            for basis, cost in ((False, [1.0, 3.0, None][index]), (True, [2.0, 6.0, 10.0][index])):
                await pool.execute(
                    "INSERT INTO llm_records(agent_id,task_id,attempt_id,run_kind,cost_usd,metadata,created_at) "
                    "VALUES ('worker-synthetic',$1,$2,'worker',$3,$4::jsonb,$5)", task, attempt_id, cost,
                    json.dumps({"accounting_basis": "subscription_equivalent"} if basis else {}), moment)
        # An accepted step with no run of either basis must not become a free cost sample.
        await pool.execute(
            "INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger,decision,started_at,decided_at) "
            "VALUES ($1,'no-run',1,1,'first','accept',$2,$2)", task, moment)
        await pool.execute(
            "INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger,decision,started_at,decided_at) "
            "VALUES ($1,'reopened',1,1,'reopen_verification','fail',$2,$2)", task, moment)


def test_alert_figures_use_median_and_each_basis_own_known_accepted_steps(monkeypatch):
    async def check(pool):
        result = await metrics.compute(7, metrics.Filters(), now=NOW, pool=pool, alerts=True)
        figures = result["alert_figures"]
        assert figures["cost"] == {
            "provider_priced": {"n": 2, "median": 2.0}, "equivalent": {"n": 3, "median": 6.0},
            "previous": {"provider_priced": {"n": 2, "median": 2.0}, "equivalent": {"n": 3, "median": 6.0}}}
        assert figures["misses"] == {"n": 5, "count": 1, "previous": {"n": 5, "count": 1}}
        # Failed first attempts and child review runs belong to the accepted step's total.
        attempt_id = await pool.fetchval(
            "INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger,decision,started_at,decided_at) "
            "VALUES ('current','retry-step',1,1,'first','retry',$1,$1) RETURNING id", NOW - timedelta(days=2))
        await pool.execute(
            "INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger,decision,started_at,decided_at) "
            "VALUES ('current','retry-step',1,2,'retry','accept',$1,$1)", NOW - timedelta(days=1))
        parent = await pool.fetchval(
            "INSERT INTO llm_records(agent_id,task_id,attempt_id,run_kind,cost_usd) "
            "VALUES ('worker-synthetic','current',$1,'worker',8) RETURNING id", attempt_id)
        await pool.execute(
            "INSERT INTO llm_records(agent_id,task_id,parent_record_id,run_kind,cost_usd) "
            "VALUES ('reviewer-synthetic','current',$1,'reviewer',1)", parent)
        current = (await metrics.compute(7, metrics.Filters(), now=NOW, pool=pool, alerts=True))["alert_figures"]["cost"]
        assert current["provider_priced"] == {"n": 3, "median": 3.0}  # costs 1, 3, 8+1
        assert current["equivalent"] == {"n": 3, "median": 6.0}
    run_db(check, monkeypatch, setup=seed)


def test_public_metrics_match_the_fixed_baseline_with_explicit_cost_status(monkeypatch):
    source = subprocess.check_output(["git", "show", f"{BASELINE}:looper/agents/metrics.py"], text=True)
    baseline = ModuleType("looper.agents._metrics_alerts_baseline")
    monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
    exec(compile(source, "recorded-metrics-baseline", "exec"), baseline.__dict__)

    def with_cost_status(value):
        if isinstance(value, list):
            return [with_cost_status(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: with_cost_status(item) for key, item in value.items()}
        if "unknown_runs" in value or "mean_usd" in value:
            result["cost_status"] = "unknown" if value.get("unknown_runs", value.get("unknown", 0)) else "known"
        return result

    async def check(pool):
        expected = with_cost_status(await baseline.compute(7, baseline.Filters(), now=NOW, pool=pool))
        actual = await metrics.compute(7, metrics.Filters(), now=NOW, pool=pool)
        assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
        assert "alert_figures" not in actual
        expected_task = with_cost_status(await baseline.task_attempts("current", pool=pool))
        actual_task = await metrics.task_attempts("current", pool=pool)
        assert json.dumps(actual_task, sort_keys=True) == json.dumps(expected_task, sort_keys=True)
    run_db(check, monkeypatch, setup=seed)


def test_alert_figure_errors_are_isolated_from_the_other_rules(monkeypatch):
    original = metrics.attribute

    def broken(records):
        raise ValueError("synthetic unavailable cost tree")

    async def check(pool):
        monkeypatch.setattr(metrics, "attribute", broken)
        result = await metrics.compute(7, metrics.Filters(), now=NOW, pool=pool, alerts=True)
        assert "error" in result["alert_figures"]["cost"]
        assert result["alert_figures"]["misses"]["n"] == 5
        assert "error" not in result["tasks"]["success_rate"]
        monkeypatch.setattr(metrics, "attribute", original)
    run_db(check, monkeypatch, setup=seed)


def test_hourly_missing_attempts_include_ongoing_tasks_and_exclude_historical_runs(monkeypatch):
    async def check(pool):
        assert await db_metrics.missing_attempts(pool=pool) == 0  # linked controls are present
        for agent, days, kind in (("worker-before", 20, "worker"), ("worker-lost", 1, "worker"),
                                  ("planner-synthetic", 1, "planner")):
            await pool.execute(
                "INSERT INTO llm_records(agent_id,task_id,run_kind,created_at) VALUES ($1,'ongoing',$2,$3)",
                agent, kind, NOW - timedelta(days=days))
        assert await db_metrics.missing_attempts(pool=pool) == 1
        # No outcome exists for this ongoing task: the public completed-window count cannot see it.
        assert (await metrics.compute(7, metrics.Filters(), now=NOW, pool=pool))["missing_attempts"] == 0
    run_db(check, monkeypatch, setup=seed)
