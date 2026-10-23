"""Channel constraint migrations preserve unrelated checks and accept bot-token channels."""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.db_support import run_with_channel_configs

pytestmark = pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                                reason="LOOPER_TEST_DATABASE_URL is not set")


def test_renamed_channel_checks_are_widened_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection
    from tests.test_gate_channels_db import _task_with_gate

    async def test(pool: Any) -> None:
        await pool.execute("ALTER TABLE task_user_gate_posts DROP CONSTRAINT task_user_gate_posts_channel_check")
        await pool.execute('ALTER TABLE task_user_gate_posts ADD CONSTRAINT "old gate channel" '
                           "CHECK (channel IN ('lark', 'telegram'))")
        await pool.execute("ALTER TABLE task_user_gate_posts ADD CONSTRAINT another_gate_channel "
                           "CHECK (channel IN ('telegram', 'lark'))")
        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_notify_channel_check")
        await pool.execute('ALTER TABLE schedules ADD CONSTRAINT "old notify channel" '
                           "CHECK (notify_channel IN ('lark', 'telegram'))")
        await pool.execute("ALTER TABLE schedules ADD CONSTRAINT another_notify_channel "
                           "CHECK (notify_channel IN ('telegram', 'lark'))")
        await pool.execute("DELETE FROM schema_migrations WHERE name IN ('task_user_gate_posts_lark_bot', "
                           "'schedules_notify_channel_lark_bot')")
        await _task_with_gate(pool, origin="channel-lark_bot", chat="ou_schema_test", kind="user")
        message_id = await pool.fetchval("SELECT id FROM task_user_gate_messages WHERE task_id = 'task-a' LIMIT 1")
        project_id = await pool.fetchval("SELECT project_id FROM tasks WHERE id = 'task-a'")

        async def checks() -> list[Any]:
            return await pool.fetch("SELECT conrelid, conname, oid, pg_get_constraintdef(oid) AS definition "
                                    "FROM pg_constraint WHERE conrelid IN (to_regclass('schedules'), "
                                    "to_regclass('task_user_gate_posts')) AND contype = 'c' "
                                    "ORDER BY conrelid, conname")

        old_names = {"old gate channel", "another_gate_channel", "old notify channel", "another_notify_channel"}
        unrelated = [row for row in await checks() if row["conname"] not in old_names]
        assert {"schedules_notify_check", "schedules_daily_budget_usd_check",
                "schedules_notify_target_check"} <= {row["conname"] for row in unrelated}

        async def write(post: bool) -> None:
            if post:
                await pool.execute("INSERT INTO task_user_gate_posts (message_id, task_id, channel, chat, external_id) "
                                   "VALUES ($1, 'task-a', 'lark_bot', 'ou_schema_test', 'om_schema_post')", message_id)
            else:
                await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, "
                                   "notify_channel, notify_chat) VALUES ('schedule-a', $1, 'daily', '0 1 * * *', "
                                   "'UTC', 'check', 'lark_bot', 'ou_schema_test')", project_id)

        for post in (False, True):
            with pytest.raises(asyncpg.CheckViolationError):
                await write(post)

        await connection.ensure_schema(pool)
        widened = await checks()
        assert not old_names & {row["conname"] for row in widened}
        assert [row for row in widened if row["conname"] not in {
            "task_user_gate_posts_channel_check", "schedules_notify_channel_check"}] == unrelated
        await connection.ensure_schema(pool)
        assert await checks() == widened
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                   "('task_user_gate_posts_lark_bot', 'schedules_notify_channel_lark_bot')") == 2

        await write(False)
        await write(True)
        assert await pool.fetchval("SELECT channel FROM task_user_gate_posts "
                                   "WHERE external_id = 'om_schema_post'") == "lark_bot"
        assert await pool.fetchval("SELECT notify_channel FROM schedules WHERE id = 'schedule-a'") == "lark_bot"
        for assignment in ("notify = 'invalid'", "daily_budget_usd = -1", "notify_chat = NULL"):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE schedules SET {assignment} WHERE id = 'schedule-a'")

    run_with_channel_configs(test, monkeypatch)
