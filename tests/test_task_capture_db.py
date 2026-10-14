"""The task journal's kinds and the Manager's recent chats in a real PostgreSQL (M7c).

Every kind an entry can have is saved with the change it records and read
back in order, an entry of the whole task without a step; a journal from
before M7c, whose check held ``step_accepted`` alone, accepts the other kinds
after one migration. The Manager's new sessions read the recent chats from
the stored chat messages, never a task's Planner thread. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.tiers.journal import JournalKind
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(pool: Any) -> None:
    from looper.db import plans, projects, tasks

    project = await projects.create_project(name="calc", work_dir="/w", pool=pool)
    await tasks.create_task(task_id="task-a", requirement="build calc", goal_id="g1", project_id=project["id"],
                            work_dir="/w", correlation_id="corr-1", status="running", pool=pool)
    await plans.save("task-a", plan={"plan_id": "plan-1", "status": "executing"},
                     steps=[{"id": "step-1", "index": 0, "key": "s1", "instruction": "do s1"}], pool=pool)


def test_every_kind_is_saved_and_read_back_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import journal, plans, tasks

    async def test(pool: Any) -> None:
        await _task(pool)
        entries = [{"step_id": "" if kind in ("user_instruction", "session_switch", "handoff") else "step-1",
                    "kind": kind, "summary": f"{kind} happened", "learnings": ["a pitfall"]}
                   for kind in (k.value for k in JournalKind)]
        await plans.save("task-a", journal=entries, pool=pool)
        # A step accepted again keeps its first entry; the other kinds may repeat.
        await plans.save("task-a", journal=[entries[0], entries[2]], pool=pool)
        rows = await journal.list_entries("task-a", pool=pool)
        assert [(r["kind"], r["step_key"]) for r in rows] == [
            (e["kind"], "s1" if e["step_id"] else "") for e in entries] + [(entries[2]["kind"], "s1")]
        assert (await tasks.get_task_with_steps("task-a", pool=pool))["journal"] == rows  # the task page's list
        with pytest.raises(asyncpg.CheckViolationError):
            await plans.save("task-a", journal=[{"step_id": "", "kind": "rumor", "summary": "", "learnings": []}],
                             pool=pool)

    _run(test, monkeypatch)


def test_a_journal_from_before_takes_the_new_kinds_after_one_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection, plans

    decision = [{"step_id": "", "kind": "decision", "summary": "replan", "learnings": []}]

    async def test(pool: Any) -> None:
        await _task(pool)
        await pool.execute("""
            ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check;
            ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN ('step_accepted'));
            DELETE FROM schema_migrations WHERE name = 'task_journal_kinds_m7c';
        """)
        with pytest.raises(asyncpg.CheckViolationError):
            await plans.save("task-a", journal=decision, pool=pool)
        await connection.ensure_schema(pool)
        await plans.save("task-a", journal=decision, pool=pool)
        await connection.ensure_schema(pool)  # once only
        assert await pool.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE name = 'task_journal_kinds_m7c'") == 1
        assert await pool.fetchval("SELECT count(*) FROM task_journal") == 1

    _run(test, monkeypatch)


def test_the_recent_chats_are_the_managers_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        for session, count in (("session-old", 3), ("planner-task-a", 2), ("session-new", 4)):
            for n in range(count):
                await chat.save_message(session_id=session, message_id=f"{session}-{n}",
                                        role="user" if n % 2 == 0 else "assistant", content=f"{session} {n}",
                                        pool=pool)
        # The Planner's thread was written last, yet it is no conversation with the Manager.
        await chat.save_message(session_id="planner-task-a", message_id="late", role="user", content="late",
                                pool=pool)
        chats = await chat.recent_conversations(sessions=5, messages=2, pool=pool)
        assert [(c["session_id"], [m["content"] for m in c["messages"]]) for c in chats] == [
            ("session-new", ["session-new 2", "session-new 3"]),
            ("session-old", ["session-old 1", "session-old 2"]),
        ]
        assert [c["session_id"] for c in await chat.recent_conversations(sessions=1, messages=2, pool=pool)] == [
            "session-new"]

    _run(test, monkeypatch)
