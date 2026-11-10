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
