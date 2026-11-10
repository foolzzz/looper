"""Workflow rule events survive catalog upgrades and task-detail readback."""

import json

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_workflow_rule_journal_upgrade_and_readback(monkeypatch):
    from looper.agents import projects
    from looper.agents.tiers.journal import JournalEntry, JournalKind
    from looper.db import connection, journal, projects as db_projects, tasks

    async def test(pool):
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await pool.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
        await pool.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
                           "CHECK (kind IN ('step_accepted', 'profile_fallback', 'condition_blocked'))")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'workflow_rules'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'workflow_rules'") == 1
        project = await db_projects.create_project(name="Example", pool=pool)
        await tasks.create_task(task_id="task", requirement="Verify the change", project_id=project["id"], pool=pool)
        events = [JournalEntry("", "", json.dumps({"round": 2, "head": "abc", "passed": False}),
                               kind=JournalKind.VERIFICATION_ROUND),
                  JournalEntry("", "", "Ask the user before more work", kind=JournalKind.ESCALATION)]
        await journal.insert(pool, "task", [event.row() for event in events])
        detail = await projects.task_detail("task")
        loaded = [JournalEntry.from_row(row) for row in detail["journal"]]
        assert [(row.kind, row.summary) for row in loaded] == [(row.kind, row.summary) for row in events]
        assert {row.kind.value for row in loaded} <= set(journal.KINDS)

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("purpose", ["exhaustion", "verification_exhaustion"])
def test_exhaustion_gate_purpose_upgrade_and_plan_readback(monkeypatch, purpose):
    from looper.agents.user_gates import Gate
    from looper.db import connection, plans, projects as db_projects, tasks

    async def test(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await tasks.create_task(task_id="task", requirement="Verify the change", project_id=project["id"], pool=pool)
        gate = {"step_id": "step-g1", "topic": "Continue verification?", "state": "pending"}
        await plans.save("task", plan={"plan_id": "plan-1", "status": "executing"},
                         steps=[{"id": "step-g1", "index": 0, "key": "g1", "instruction": gate["topic"],
                                 "status": "pending", "kind": "gate"}], gates=[gate], pool=pool)
        # Recreate the released layout, including an ordinary gate that must retain its meaning.
        await pool.execute("ALTER TABLE task_user_gates DROP COLUMN IF EXISTS purpose")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'gate_purpose'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'gate_purpose'") == 1
        stored = await plans.load("task", pool=pool)
        assert stored["gates"]["step-g1"]["purpose"] == "discussion"

        # The escalation purpose survives a full plan readback and a stale ordinary-gate write.
        await plans.save("task", gates=[{**gate, "purpose": purpose}], pool=pool)
        await plans.save("task", gates=[gate], pool=pool)
        stored = await plans.load("task", pool=pool)
        row = stored["gates"]["step-g1"]
        assert row["purpose"] == purpose
        assert Gate.from_row(row).row("step-g1")["purpose"] == purpose
        await plans.save("task", gates=[{**gate, "purpose": purpose, "state": "open"}], pool=pool)
        row = (await plans.load("task", pool=pool))["gates"]["step-g1"]
        assert (row["state"], row["purpose"]) == ("open", purpose)

    run_db(test, monkeypatch, timeout=30)
