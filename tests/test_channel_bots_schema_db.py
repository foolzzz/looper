"""Each bot's Manager and the bot a gate's channel message went through, on a real PostgreSQL: the schema, the
channel_configs functions and the gate-post writes (RFC scenario-managers §9.1).

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from tests.db_support import DSN, run_with_channel_configs
from tests.test_gate_channels_db import _task_with_gate

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BUILTIN = ["channel-lark", "channel-lark_bot", "channel-telegram"]


async def _manager(pool: Any, manager_id: str = "manager-review", *, enabled: bool = True,
                   deleted: bool = False) -> None:
    await pool.execute(
        "INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) "
        "VALUES ($1, $1, 'You review.', $2, CASE WHEN $3 THEN now() END)", manager_id, enabled and not deleted, deleted)


async def _bot(pool: Any, channel_type: str = "telegram", manager_id: str = "manager", secret: str | None = None,
               **extra: Any) -> dict[str, Any]:
    from looper.db import channel_configs

    async with pool.acquire() as conn, conn.transaction():
        return await channel_configs.create_bot(
            channel_type, manager_id, display_name=extra.get("display_name", "Bot"), enabled=True,
            config=extra.get("config", {"k": 1}), secret=secret, conn=conn)


def test_builtin_rows_belong_to_the_default_manager_and_a_second_start_changes_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import channel_configs, connection

    async def test(pool: Any) -> None:
        rows = await channel_configs.list_configs(pool=pool)
        assert [r["channel_id"] for r in rows] == BUILTIN
        assert {r["manager_id"] for r in rows} == {"manager"}
        assert all("manager_name" in r for r in rows)
        seq = await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq")
        await connection.ensure_schema(pool)
        assert [r["channel_id"] for r in await channel_configs.list_configs(pool=pool)] == BUILTIN
        assert await pool.fetchrow("SELECT last_value, is_called FROM channel_bot_seq") == seq

    run_with_channel_configs(test, monkeypatch)


def test_added_bots_get_growing_ids_that_are_never_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import channel_configs

    async def test(pool: Any) -> None:
        assert (await _bot(pool))["channel_id"] == "channel-telegram-2"
        assert (await _bot(pool))["channel_id"] == "channel-telegram-3"
        assert await channel_configs.delete_bot("channel-telegram-3", pool=pool) == "deleted"
        assert await channel_configs.delete_bot("channel-telegram-3", pool=pool) == "missing"
        bot = await _bot(pool, secret="s3cret")
        assert bot["channel_id"] == "channel-telegram-4"
        assert await channel_configs.get_bot_token("channel-telegram-4", pool=pool) == "s3cret"
        rows = await channel_configs.list_configs(pool=pool)
        assert [r["channel_id"] for r in rows] == BUILTIN + ["channel-telegram-2", "channel-telegram-4"]
        added = rows[-1]
        assert added["has_bot_token"] is True and "s3cret" not in repr(rows)
        assert [k["channel_id"] for k in await channel_configs.bot_keys("telegram", conn=pool)][-1] \
            == "channel-telegram-4"
        keys = await channel_configs.bot_keys("telegram", conn=pool)
        assert keys[-1]["secret"] == "s3cret" and keys[-1]["config"] == {"k": 1}

    run_with_channel_configs(test, monkeypatch)


def test_create_bot_needs_a_live_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        with pytest.raises(LookupError):
            await _bot(pool, manager_id="manager-nope")
        await _manager(pool, "manager-gone", deleted=True)
        with pytest.raises(LookupError):
            await _bot(pool, manager_id="manager-gone")
        await _manager(pool, "manager-off", enabled=False)
        bot = await _bot(pool, manager_id="manager-off")
        assert bot["manager_id"] == "manager-off" and bot["manager_name"] == "manager-off"

    run_with_channel_configs(test, monkeypatch)


def test_a_bots_manager_cannot_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import channel_configs

    async def test(pool: Any) -> None:
        await _manager(pool)
        bot = await _bot(pool)
        with pytest.raises(asyncpg.CheckViolationError, match="cannot change"):
            await pool.execute("UPDATE channel_configs SET manager_id = 'manager-review' WHERE channel_id = $1",
                               bot["channel_id"])
        await pool.execute("UPDATE channel_configs SET manager_id = manager_id WHERE channel_id = $1",
                           bot["channel_id"])
        updated = await channel_configs.set_config(bot["channel_id"], {"enabled": False, "config": {"a": 2}},
                                                   pool=pool)
        assert updated["enabled"] is False and updated["config"] == {"a": 2} and updated["manager_id"] == "manager"
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("DELETE FROM managers WHERE id = 'manager'")

    run_with_channel_configs(test, monkeypatch)


def test_delete_bot_keeps_builtin_rows_and_chat_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import channel_configs

    async def test(pool: Any) -> None:
        assert await channel_configs.delete_bot("channel-telegram", pool=pool) == "builtin"
        assert await channel_configs.get_config("channel-telegram", pool=pool) is not None
        bot = await _bot(pool, secret="tok")
        cid = bot["channel_id"]
        await channel_configs.record_rejected(cid, "u1", pool=pool)
        await pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content) "
                           "VALUES ($1, 'm1', 'user', 'hi')", f"{cid}:42")
        assert await channel_configs.delete_bot(cid, pool=pool) == "deleted"
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets WHERE channel_id = $1", cid) == 0
        assert await pool.fetchval("SELECT count(*) FROM channel_rejected_senders WHERE channel_id = $1", cid) == 0
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id = $1", f"{cid}:42") == 1

    run_with_channel_configs(test, monkeypatch)


def test_a_manager_with_a_bot_is_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import channel_configs, managers

    async def test(pool: Any) -> None:
        await _manager(pool)
        bot = await _bot(pool, manager_id="manager-review")
        assert await managers.delete_manager("manager-review") == "has_bots"
        assert await pool.fetchval("SELECT deleted_at FROM managers WHERE id = 'manager-review'") is None
        await channel_configs.delete_bot(bot["channel_id"], pool=pool)
        assert await managers.delete_manager("manager-review") == "deleted"

    run_with_channel_configs(test, monkeypatch)


def test_a_bot_is_not_added_to_a_manager_being_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def test(pool: Any) -> None:
        await _manager(pool)
        locked, release = asyncio.Event(), asyncio.Event()

        async def deleting() -> None:
            async with pool.acquire() as conn, conn.transaction():
                await managers.lock_for_update("manager-review", conn=conn)
                locked.set()
                await release.wait()
                await conn.execute("UPDATE managers SET deleted_at = now(), enabled = false "
                                   "WHERE id = 'manager-review'")

        async def adding() -> None:
            await locked.wait()
            await _bot(pool, manager_id="manager-review")

        a = asyncio.create_task(deleting())
        b = asyncio.create_task(adding())
        await locked.wait()
        await asyncio.sleep(0.3)
        assert not b.done()
        release.set()
        await a
        with pytest.raises(LookupError):
            await b
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE manager_id = 'manager-review'") == 0

    run_with_channel_configs(test, monkeypatch)


def test_gate_posts_are_keyed_by_bot_and_each_bot_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import user_gates

    async def test(pool: Any) -> None:
        await _task_with_gate(pool)
        mid = await pool.fetchval("SELECT id FROM task_user_gate_messages LIMIT 1")
        await user_gates.record_post(mid, "task-a", "sent", channel="telegram", channel_id="channel-telegram-2",
                                     chat="42", external_ids=["7"], pool=pool)
        await user_gates.record_post(mid, "task-a", "sent", channel="telegram", channel_id="channel-telegram",
                                     chat="42", external_ids=["7"], pool=pool)
        # Two bots' message 7 in chat 42 are two posts.
        rows = await pool.fetch("SELECT channel, channel_id FROM task_user_gate_posts ORDER BY id")
        assert [tuple(r) for r in rows] == [("telegram", "channel-telegram-2"), ("telegram", "channel-telegram")]
        await user_gates.record_post(mid, "task-a", "sent", channel="telegram", channel_id="channel-telegram-2",
                                     chat="42", external_ids=["7"], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM task_user_gate_posts") == 2
        await user_gates.record_post(mid, "task-a", "sent", channel="lark", chat="c", external_ids=["9"], pool=pool)
        assert await pool.fetchval("SELECT channel_id FROM task_user_gate_posts WHERE external_id = '9'") \
            == "channel-lark"

    run_with_channel_configs(test, monkeypatch)


def test_a_gate_message_stores_its_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import user_gates

    async def test(pool: Any) -> None:
        await _task_with_gate(pool)
        base = {"step_id": "step-g1", "role": "user", "kind": "reply", "text": "x", "chat": "c", "external_id": "1"}
        async with pool.acquire() as conn:
            a = await conn.fetchrow(user_gates._ADD_MESSAGE, *user_gates._message_args(
                "task-a", {**base, "channel": "lark"}))
            b = await conn.fetchrow(user_gates._ADD_MESSAGE, *user_gates._message_args(
                "task-a", {**base, "channel": "lark", "channel_id": "channel-lark-5", "external_id": "2"}))
            c = await conn.fetchrow(user_gates._ADD_MESSAGE, *user_gates._message_args("task-a", {**base}))
        assert (a["channel_id"], b["channel_id"], c["channel_id"]) == ("channel-lark", "channel-lark-5", None)

    run_with_channel_configs(test, monkeypatch)
