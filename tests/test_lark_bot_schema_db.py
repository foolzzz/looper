"""Current channel constraints accept bot-token channels and preserve unrelated checks."""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.db_support import run_with_channel_configs

pytestmark = pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                                reason="LOOPER_TEST_DATABASE_URL is not set")


def test_current_lark_bot_constraints_survive_second_start(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection
    from tests.test_gate_channels_db import _task_with_gate

    async def test(pool: Any) -> None:
        await _task_with_gate(pool, origin="channel-lark_bot", chat="ou_schema_test", kind="user")
        message_id = await pool.fetchval("SELECT id FROM task_user_gate_messages WHERE task_id = 'task-a' LIMIT 1")
        project_id = await pool.fetchval("SELECT project_id FROM tasks WHERE id = 'task-a'")
        await pool.execute("INSERT INTO task_user_gate_posts "
                           "(message_id, task_id, channel, channel_id, chat, external_id) "
                           "VALUES ($1, 'task-a', 'lark_bot', 'channel-lark_bot', 'ou_schema_test', 'om_schema_post')",
                           message_id)
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, "
                           "notify_channel, notify_chat) VALUES ('schedule-a', $1, 'daily', '0 1 * * *', "
                           "'UTC', 'check', 'lark_bot', 'ou_schema_test')", project_id)
        before = await pool.fetch("SELECT * FROM task_user_gate_posts")
        schedules = await pool.fetch("SELECT * FROM schedules")
        checks = await pool.fetch("SELECT oid, pg_get_constraintdef(oid) FROM pg_constraint "
                                  "WHERE conrelid IN ('schedules'::regclass, 'task_user_gate_posts'::regclass) "
                                  "ORDER BY oid")
        for assignment in ("notify = 'invalid'", "daily_budget_usd = -1", "notify_chat = NULL"):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE schedules SET {assignment} WHERE id = 'schedule-a'")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE task_user_gate_posts SET channel = 'invalid'")
        await connection.ensure_schema(pool)
        assert await pool.fetch("SELECT * FROM task_user_gate_posts") == before
        assert await pool.fetch("SELECT * FROM schedules") == schedules
        assert await pool.fetch("SELECT oid, pg_get_constraintdef(oid) FROM pg_constraint "
                                "WHERE conrelid IN ('schedules'::regclass, 'task_user_gate_posts'::regclass) "
                                "ORDER BY oid") == checks

    run_with_channel_configs(test, monkeypatch)
