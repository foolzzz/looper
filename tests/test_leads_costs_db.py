"""Extraction and its check contribute to subscription costs, including an unknown check price."""

import json
from datetime import timedelta

import pytest

from looper.agents import processor
from looper.agents.agent_settings import AgentType
from looper.db import llm_records, schedules
from tests.db_support import DSN
from tests.lark_cli_stand_in import lark_cli_stand_in  # noqa: F401
from tests.schedule_support import run_scheduled
from tests.subscription_support import add, confirm, fire, minutes_ago, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("check_cost", [None, 0.02])
@pytest.mark.parametrize("daily_cap", [None, 0.5])
def test_subscription_daily_spend_includes_the_check_and_preserves_unknown(
    monkeypatch, tmp_path, caplog, check_cost, daily_cap,
):
    async def test(env):
        await source(env.pool)
        await env.pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        fields = (await env.ok("POST", "/api/v1/schedule_admin/subscription_preset",
                               {"output_kind": "leads"}))["fields"]
        fields["subscription"]["check_items"] = True
        fields.update(cron="*/5 * * * *", timezone="America/Los_Angeles", daily_budget_usd=daily_cap)
        sid = (await env.schedule(**fields))["schedule_id"]
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"enabled": True}})
        await confirm(env, sid)
        await add(env.pool, "Release moves to Friday", sent_at=minutes_ago(env, 2))

        async def run(**kwargs):
            checking = kwargs.get("role", AgentType.PROCESSOR) is AgentType.LEADS_CHECK
            if checking and daily_cap is not None:
                assert kwargs["budget_usd"] == pytest.approx(daily_cap - 0.1)
            cost = check_cost if checking else 0.1
            rid = await llm_records.start_record(
                agent_id=f"{'leads_check' if checking else 'processor'}:{sid}",
                metadata=dict(kwargs["metadata"]), pool=env.pool)
            await llm_records.finish_record(rid, status="completed", input_tokens=1, cost_usd=cost,
                                           model="claude-haiku-5-5" if checking else "sonnet", pool=env.pool)
            reply = ({"verdicts": [{"item": "X1", "verdict": "keep"}]} if checking else
                     {"leads": [{"kind": "decision", "summary": "Release moves to Friday",
                                 "confidence": "high", "sources": ["m1"]}]})
            return processor.ProcessorRun(json.dumps(reply), rid, cost)

        monkeypatch.setattr(processor, "run", run)
        run = await fire(env, sid)
        assert run["status"] == "completed", run
        counts = json.loads(run["stream"]["extract_counts"])
        assert counts["check_cost_usd"] == check_cost
        assert run["task_id"] is None
        if check_cost is None:
            assert run["cost_usd"] is None
        else:
            assert float(run["cost_usd"]) == pytest.approx(0.12)
        # An old settlement may predate the check: the reader still includes its record exactly once.
        await env.pool.execute("UPDATE schedule_runs SET cost_usd = 0.1 WHERE id = $1", run["id"])
        caplog.clear()
        since = env.clock.now - timedelta(hours=12)
        expected = 0.1 + (check_cost or 0)
        assert await schedules.spent_since(env.pool, sid, since) == pytest.approx(expected)
        assert await schedules.spent_since(env.pool, sid, since) == pytest.approx(expected)
        warnings = [r for r in caplog.records if "daily spend cap" in r.message]
        assert len(warnings) == (1 if daily_cap is not None and check_cost is None else 0)
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert view["cost_status"] == ("unknown" if check_cost is None else "known")
        assert view["spent_today_usd"] == pytest.approx(expected)

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("same_task", [False, True])
def test_linked_check_cost_is_counted_once_with_a_task(monkeypatch, tmp_path, same_task):
    async def test(env):
        sid = (await env.enabled())["schedule_id"]
        run = await fire(env, sid)
        for role, cost, task_id in (("worker", 0.1, run["task_id"]),
                                    ("leads_check", 0.02, run["task_id"] if same_task else None)):
            rid = await llm_records.start_record(
                agent_id=f"{role}-{sid}", run_kind="worker" if role == "worker" else "leads_check",
                task_id=task_id, metadata={"leads_check": {"run_id": run["id"]}} if role == "leads_check" else {},
                pool=env.pool)
            await llm_records.finish_record(rid, status="completed", cost_usd=cost, pool=env.pool)
        await env.pool.execute("UPDATE schedule_runs SET cost_usd = 0.1 WHERE id = $1", run["id"])
        assert await schedules.spent_since(env.pool, sid, env.clock.now - timedelta(hours=1)) == pytest.approx(0.12)

    run_scheduled(test, monkeypatch, tmp_path)
