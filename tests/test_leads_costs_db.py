"""Extraction and its check contribute to subscription costs, including an unknown check price."""

import json
from datetime import timedelta

import pytest

from looper.agents import processor
from looper.agents.agent_settings import AgentType
from looper.db import llm_records, schedules, subscriptions
from tests.db_support import DSN, run_db
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
        check_record_id = run["stream"]["check_record_id"]
        assert check_record_id is not None
        assert await env.pool.fetchval("SELECT run_kind FROM llm_records WHERE id = $1", check_record_id) == "leads_check"
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
            if role == "leads_check":
                await env.pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, check_record_id) "
                                       "VALUES ($1, 0, 0, $2)", run["id"], rid)
        await env.pool.execute("UPDATE schedule_runs SET cost_usd = 0.1 WHERE id = $1", run["id"])
        assert await schedules.spent_since(env.pool, sid, env.clock.now - timedelta(hours=1)) == pytest.approx(0.12)

    run_scheduled(test, monkeypatch, tmp_path)


def test_subscription_check_record_upgrade_backfills_and_retains_the_foreign_key():
    async def test(pool):
        from looper.db.migrations import subscription_check_record as migration

        assert migration.order == migration.pending_order == 1820
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Project')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
                           "VALUES ('s', 'p', 'Schedule', '0 * * * *', 'UTC', 'Check')")
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                           "VALUES ('r', 's', now(), 'manual', 'completed')")
        await pool.execute("ALTER TABLE subscription_runs DROP COLUMN check_record_id")
        await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq) VALUES ('r', 0, 0)")
        for _ in range(2):
            rid = await llm_records.start_record(agent_id="leads_check-s", run_kind="leads_check",
                                                 metadata={"leads_check": {"run_id": "r"}}, pool=pool)
        await migration.apply(pool, sql="", state="current")
        assert not await migration.pending(pool)
        assert (await subscriptions.get_runs(["r"], db=pool))["r"]["check_record_id"] == rid
        await migration.apply(pool, sql="", state="current")
        await pool.execute("DELETE FROM llm_records WHERE id = $1", rid)
        assert await pool.fetchval("SELECT check_record_id FROM subscription_runs WHERE run_id = 'r'") is None

    run_db(test)


def test_subscription_daily_spend_uses_indexed_record_links_at_volume(monkeypatch, tmp_path):
    """200 firings among 200k unrelated records must never scan the record table per firing."""
    async def test(env):
        sid = (await env.enabled())["schedule_id"]
        await env.pool.execute("""
            INSERT INTO llm_records (agent_id, task_id, cost_usd)
            SELECT 'worker-unrelated-' || i, 'unrelated-' || i, 1 FROM generate_series(1, 200000) i
        """)
        await env.pool.execute("""
            INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, cost_usd)
            SELECT 'volume-' || i, $1, $2, $2, 'manual', 'completed', 0.1 FROM generate_series(1, 200) i
        """, sid, env.clock.now)
        await env.pool.execute("""
            WITH extracted AS (
                INSERT INTO llm_records (agent_id, run_kind, cost_usd)
                SELECT 'volume-' || i, 'processor', 0.1 FROM generate_series(1, 200) i
                RETURNING id, agent_id
            ), checked AS (
                INSERT INTO llm_records (agent_id, run_kind, cost_usd, input_tokens, model)
                SELECT 'volume-' || i, 'leads_check', CASE WHEN i = 200 THEN NULL ELSE 0.02 END, 1,
                       'claude-haiku-5-5' FROM generate_series(1, 200) i
                RETURNING id, agent_id
            )
            INSERT INTO subscription_runs (run_id, from_seq, to_seq, record_id, check_record_id)
            SELECT e.agent_id, 0, 0, e.id, c.id FROM extracted e JOIN checked c USING (agent_id)
        """)
        await env.pool.execute("ANALYZE llm_records, subscription_runs, schedule_runs, schedules")

        class IndexedReads:
            async def fetch(self, sql, *args):
                plan = "\n".join(row[0] for row in await env.pool.fetch("EXPLAIN " + sql, *args))
                assert "Seq Scan on llm_records" not in plan, plan
                return await env.pool.fetch(sql, *args)

        statuses = {}
        spent = await schedules.spent_since_each(IndexedReads(), [sid], env.clock.now - timedelta(hours=1),
                                                statuses=statuses)
        assert spent[sid] == pytest.approx(20 + 199 * 0.02)
        assert statuses == {sid: "unknown"}

    run_scheduled(test, monkeypatch, tmp_path)
