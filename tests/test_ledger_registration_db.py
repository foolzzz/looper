"""The ledger's registration hooks and the reconciliation that catches what they miss (RFC 4.5)."""

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import projects as projects_api
from looper.agents.ops import Risk, Tool, Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.db import connection, ledger, plans
from tests import ledger_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def enable(pool, on=True):
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", "true" if on else "false")


async def titles(pool, status=None):
    return {i["id"]: (i["kind"], i["title"], i["status"]) for i in (await ledger.list_items(status=status, db=pool))[0]}


def test_nothing_is_registered_while_the_ledger_is_off(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        seeded = await sup.seed(pool)
        await ledger.register_gates(seeded["task"], db=pool)
        await ledger.register_task(seeded["task"], db=pool)
        await ledger.register_confirmation(seeded["action"], db=pool)
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 0
        monkeypatch.setattr(projects_api, "_root", str(tmp_path / "projects"))
        await projects_api.create_task_core({"requirement": "Do a thing", "project": {"name": "P"}})
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 0

    run_db(check, monkeypatch, timeout=30)


def test_a_new_task_a_gate_and_a_question_appear_and_a_scheduled_task_does_not(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(projects_api, "_root", str(tmp_path / "projects"))
        await enable(pool)
        created = await projects_api.create_task_core({"requirement": "Do a thing", "project": {"name": "P"}})
        (item,) = (await ledger.list_items(db=pool))[0]
        assert (item["kind"], item["source"]["ref"], item["owner"], item["assurance"]) == (
            "work_item", created["task_id"], f"task:{created['task_id']}", "system")
        # a gate that opens through the plan writer
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction, kind) "
                           "VALUES ('g1', $1, 0, 'Ask', 'gate')", created["task_id"])
        await plans.save(created["task_id"], gates=[{"step_id": "g1", "topic": "Pick a database", "state": "open"}],
                         pool=pool)
        assert "Pick a database" in {v[1] for v in (await titles(pool)).values()}
        # a task a schedule made is not registered (not by the hook, not by reconciliation)
        await pool.execute("INSERT INTO schedules (id, name, project_id, cron, timezone, requirement) "
                           "VALUES ('s1', 'S', $1, '0 * * * *', 'UTC', 'r')", await pool.fetchval("SELECT id FROM projects"))
        await pool.execute("INSERT INTO tasks (id, requirement, project_id, schedule_id, status) "
                           "VALUES ('scheduled', 'r', $1, 's1', 'ready')", await pool.fetchval("SELECT id FROM projects"))
        await ledger.register_task("scheduled", db=pool)
        await ledger.reconcile(db=pool)
        assert await pool.fetchval("SELECT count(*) FROM ledger_items WHERE task_id = 'scheduled'") == 0
        # the Manager's question to confirm
        async def run(call):
            return {}

        async def escalate(call):
            return "Delete it?"

        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1",
                         tools=(Tool("t_delete", "deletes", {"type": "object", "properties": {}}, Risk.WRITE, run,
                                     escalate=escalate),))
        ops._turn = Turn(channel="web", chat="c", message_id="m1", channel_id="channel-web")
        answer = await ops.call("t_delete", {})
        assert answer["status"] == "needs_confirmation", answer
        confirmations = await pool.fetch("SELECT i.id FROM ledger_items i WHERE i.source_kind = 'confirmation'")
        assert len(confirmations) == 1

    run_db(check, monkeypatch, timeout=60)


def test_reconciliation_registers_what_a_missing_hook_left_and_says_so(monkeypatch, caplog):
    async def check(pool):
        seeded = await sup.seed(pool)
        await pool.execute("INSERT INTO tasks (id, requirement, project_id, status) VALUES ('done', 'r', 'proj', 'completed')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id, status) VALUES ('failed', 'r', 'proj', 'failed')")
        old = await pool.fetchval(
            "INSERT INTO manager_actions (tool, risk, status, confirmation_id, created_at) "
            "VALUES ('looper_task_control', 'high', 'needs_confirmation', 'old', $1) RETURNING id",
            datetime.now(timezone.utc) - timedelta(seconds=ledger.CONFIRMATION_SECONDS + 60))
        answered = await pool.fetchval(
            "INSERT INTO manager_actions (tool, risk, status, confirmation_id) "
            "VALUES ('looper_task_control', 'high', 'needs_confirmation', 'answered') RETURNING id")
        await pool.execute("INSERT INTO manager_actions (tool, risk, status, confirmation_id) "
                           "VALUES ('looper_task_control', 'high', 'ok', 'answered')")
        with caplog.at_level("INFO"):
            assert await ledger.reconcile(db=pool) == 3  # the live task, the open gate, the unanswered question
        assert any(r.levelname == "WARNING" and "registered=3" in r.message for r in caplog.records)
        sourced = await pool.fetch("SELECT source_kind, task_id, gate_step_id, action_id FROM ledger_items")
        assert sorted(r["source_kind"] for r in sourced) == ["confirmation", "gate", "task"]
        assert {r["task_id"] for r in sourced if r["task_id"]} == {seeded["task"]}  # not the ended ones
        assert not {r["action_id"] for r in sourced} & {old, answered}  # not a lapsed or an answered question
        caplog.clear()
        assert await ledger.reconcile(db=pool) == 0  # and again changes nothing
        assert not any(r.levelname == "WARNING" for r in caplog.records)

    run_db(check, monkeypatch, timeout=30)


def test_the_reconciler_waits_until_the_ledger_is_on_then_catches_up_and_repeats_every_interval(monkeypatch):
    from looper.agents.ledger_reconciler import LedgerReconciler

    async def check(pool):
        seeded = await sup.seed(pool)
        reconciler = LedgerReconciler(interval=300)
        assert await reconciler.run_once(0) is None  # off: nothing
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 0
        await enable(pool)
        assert await reconciler.run_once(10) == 3  # the first tick after turning it on catches up
        assert await reconciler.run_once(200) is None  # not yet
        await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('t2', 'r', 'proj')")
        assert await reconciler.run_once(311) == 1  # and every interval after
        await enable(pool, False)
        assert await reconciler.run_once(900) is None
        await enable(pool)
        assert await reconciler.run_once(901) == 0  # switched on again: reconciles at once
        assert seeded

    run_db(check, monkeypatch, timeout=30)
