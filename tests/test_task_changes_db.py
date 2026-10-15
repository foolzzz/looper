"""A task's changes in a real PostgreSQL: supplements, requirement versions, and what its Planner has had.

A supplement and a requirement edit are pending until ``mark_delivered``; the
first edit keeps the text the task had as version 1; the user instructions
from before become supplements their Planner already had. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_task_capture_db import _task
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_supplements_and_requirement_edits_are_pending_until_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import task_changes, tasks
    from looper.db.task_changes import RequirementEdit

    async def test(pool: Any) -> None:
        await _task(pool)
        first = await task_changes.add_supplement("task-a", "use CSV", via="chat", pool=pool)
        assert await task_changes.edit_requirement("task-a", "build calc, with a CLI", via="task_page",
                                                   delivered=False, pool=pool) == 2
        pending = await task_changes.pending("task-a", pool=pool)
        assert [(s.id, s.text, s.via) for s in pending.supplements] == [(first, "use CSV", "chat")]
        assert pending.requirement == RequirementEdit(2, "build calc, with a CLI", 1, "build calc")
        assert (await tasks.get_task("task-a", pool=pool))["requirement"] == "build calc, with a CLI"

        # A change made while the Planner's turn ran is not delivered with the changes that turn had.
        second = await task_changes.add_supplement("task-a", "and a JSON export", via="manager:lark", pool=pool)
        assert await task_changes.edit_requirement("task-a", "build calc, with a CLI and docs", via="task_page",
                                                   delivered=False, pool=pool) == 3
        await task_changes.mark_delivered("task-a", pending, pool=pool)
        later = await task_changes.pending("task-a", pool=pool)
        assert [s.id for s in later.supplements] == [second]
        assert later.requirement == RequirementEdit(3, "build calc, with a CLI and docs", 2, "build calc, with a CLI")
        await task_changes.mark_delivered("task-a", later, pool=pool)
        assert (await task_changes.pending("task-a", pool=pool)).empty

        versions = await task_changes.requirement_versions("task-a", pool=pool)
        assert [(v["version"], v["text"], v["via"], v["delivered_at"] is not None) for v in versions] == [
            (1, "build calc", "created", True), (2, "build calc, with a CLI", "task_page", True),
            (3, "build calc, with a CLI and docs", "task_page", True)]
        # An edit of a task without a Planner yet is delivered at once; an unknown task has no version.
        assert await task_changes.edit_requirement("task-a", "v4", via="task_page", delivered=True, pool=pool) == 4
        assert (await task_changes.pending("task-a", pool=pool)).empty
        assert await task_changes.edit_requirement("task-nope", "x", via="task_page", delivered=True,
                                                   pool=pool) is None

    _run(test, monkeypatch)


def test_user_instructions_from_before_become_delivered_supplements(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection, journal, task_changes

    async def test(pool: Any) -> None:
        await _task(pool)
        await pool.execute("""
            ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check;
            ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
                'step_accepted', 'step_failed', 'step_retry', 'decision', 'user_instruction', 'session_switch',
                'handoff', 'progress'));
            INSERT INTO task_journal (task_id, step_id, kind, summary) VALUES ('task-a', '', 'user_instruction', 'old');
            UPDATE task_journal SET via = '', delivered_at = NULL;
            DELETE FROM schema_migrations WHERE name = 'task_journal_supplements';
        """)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)  # once only
        (row,) = await journal.list_entries("task-a", pool=pool)
        assert (row["kind"], row["summary"], row["via"], row["delivered_at"]) == (
            "supplement", "old", "chat", row["created_at"])
        assert (await task_changes.pending("task-a", pool=pool)).empty

    _run(test, monkeypatch)
