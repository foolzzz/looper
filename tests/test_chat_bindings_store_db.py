"""Durable bot chat bindings and metadata, against a disposable PostgreSQL schema."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from looper.db import channel_configs, chat, connection, managers
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BOT = "channel-telegram-201"
OTHER_BOT = "channel-telegram-202"
TARGET = "manager-review"


async def _seed(pool: Any) -> None:
    await pool.execute("""
        INSERT INTO managers (id, name, system_prompt) VALUES
          ('manager-review', 'Review', 'Review public examples.'),
          ('manager-other', 'Other', 'Inspect public examples.');
        INSERT INTO channel_configs
          (channel_id, channel_type, display_name, enabled, config, manager_id) VALUES
          ('channel-telegram-201', 'telegram', 'First bot', false, '{"allowed_user_ids":["7"]}', 'manager'),
          ('channel-telegram-202', 'telegram', 'Second bot', false, '{}', 'manager'),
          ('channel-lark-203', 'lark', 'Lark bot', false, '{}', 'manager'),
          ('channel-lark_bot-204', 'lark_bot', 'Native Lark bot', false, '{}', 'manager');
    """)


async def _put(pool: Any, chat_id: str = "7", manager_id: str = TARGET, channel_id: str = BOT) -> dict:
    from looper.db import chat_bindings as store

    async with pool.acquire() as conn, conn.transaction():
        return await store.put_binding(channel_id, chat_id, manager_id, conn=conn)


def test_schema_upgrade_preserves_existing_bot_and_transcript(tmp_path, monkeypatch):
    source = connection.SCHEMA_PATH
    sql = source.read_text(encoding="utf-8")
    start = sql.index("-- Durable per-bot chat bindings and metadata (no routing is enabled here).")
    end = sql.index("-- End durable chat storage.", start) + len("-- End durable chat storage.")
    old_schema = tmp_path / "schema-before-chat-storage.sql"
    old_schema.write_text(sql[:start] + sql[end:], encoding="utf-8")

    async def run():
        async with fresh_schema() as pool:
            monkeypatch.setattr(connection, "SCHEMA_PATH", old_schema)
            await connection.ensure_schema(pool)
            await _seed(pool)
            await chat.save_message(session_id="telegram-7", message_id="message-one", role="user",
                                    content="Synthetic public example", channel="telegram", pool=pool)
            before = dict(await pool.fetchrow("SELECT * FROM channel_configs WHERE channel_id = $1", BOT))
            monkeypatch.setattr(connection, "SCHEMA_PATH", source)
            await connection.ensure_schema(pool)
            after = dict(await pool.fetchrow("SELECT * FROM channel_configs WHERE channel_id = $1", BOT))
            assert after.pop("bound_chats_only") is False
            assert after == before
            assert (await chat.list_messages("telegram-7", pool=pool))[0]["content"] == "Synthetic public example"
            for table in ("channel_seen_chats", "channel_unbound_chats"):
                await pool.execute(f"INSERT INTO {table} (channel_id, chat_id) VALUES ($1, '8')", OTHER_BOT)
                assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", OTHER_BOT) == 1
                assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", BOT) == 0

    asyncio.run(asyncio.wait_for(run(), 30))


def test_repeated_schema_ensure_preserves_bindings_policy_and_observations():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        binding = await _put(pool)
        await store.record_seen(BOT, "7", "Example group", suppressed=True, pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            await store.set_bound_chats_only(BOT, True, conn=conn)
        before = await store.list_channel_chats(BOT, pool=pool)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        assert await store.list_bindings(channel_id=BOT, pool=pool) == [binding]
        assert await store.list_channel_chats(BOT, pool=pool) == before
        assert await pool.fetchval("SELECT bound_chats_only FROM channel_configs WHERE channel_id = $1", BOT)

    run_db(run, timeout=30)


@pytest.mark.parametrize("table", ["manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"])
@pytest.mark.parametrize("violation", ["blank", "channel_fk"])
def test_storage_rejects_blank_chat_ids_and_missing_channels(table, violation):
    async def run(pool):
        await _seed(pool)
        columns = "channel_id, chat_id" + (", manager_id" if table == "manager_chat_bindings" else "")
        values = "$1, $2" + (", 'manager'" if table == "manager_chat_bindings" else "")
        await pool.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", BOT, "7")
        expected = asyncpg.CheckViolationError if violation == "blank" else asyncpg.ForeignKeyViolationError
        with pytest.raises(expected):
            await pool.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})",
                               BOT if violation == "blank" else "channel-telegram-999", " " if violation == "blank" else "8")

    run_db(run, timeout=30)


def test_binding_manager_foreign_key_restricts_hard_delete():
    async def run(pool):
        await _seed(pool)
        await _put(pool)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("DELETE FROM managers WHERE id = $1", TARGET)

    run_db(run, timeout=30)


@pytest.mark.parametrize("table", ["channel_seen_chats", "channel_unbound_chats"])
@pytest.mark.parametrize("column,value", [("count", 0), ("name", "n" * 201)])
def test_metadata_constraints_reject_nonpositive_counts_and_oversized_names(table, column, value):
    async def run(pool):
        await _seed(pool)
        await pool.execute(f"INSERT INTO {table} (channel_id, chat_id) VALUES ($1, '7')", BOT)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(f"UPDATE {table} SET {column} = $2 WHERE channel_id = $1", BOT, value)

    run_db(run, timeout=30)


def test_explicit_bot_owner_binding_is_stored_with_raw_creation_time():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        row = await _put(pool, manager_id="manager")
        assert row == (await store.list_bindings(channel_id=BOT, pool=pool))[0]
        assert row["manager_id"] == "manager" and isinstance(row["created_at"], datetime)

    run_db(run, timeout=30)


def test_rebind_and_repeat_upsert_preserve_original_creation_time():
    async def run(pool):
        await _seed(pool)
        original = await _put(pool)
        rebound = await _put(pool, manager_id="manager-other")
        assert rebound["manager_id"] == "manager-other"
        assert rebound["created_at"] == original["created_at"]
        assert await _put(pool, manager_id="manager-other") == rebound

    run_db(run, timeout=30)


def test_delete_is_idempotent_and_does_not_delete_another_bots_same_chat():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await _put(pool)
        other = await _put(pool, channel_id=OTHER_BOT)
        async with pool.acquire() as conn, conn.transaction():
            assert await store.delete_binding(BOT, "7", conn=conn) is True
            assert await store.delete_binding(BOT, "7", conn=conn) is False
        assert await store.list_bindings(channel_id=OTHER_BOT, pool=pool) == [other]
        assert await store.list_bindings(channel_id=BOT, pool=pool) == []

    run_db(run, timeout=30)


def test_binding_filters_apply_channel_and_manager_together():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        first = await _put(pool)
        await _put(pool, "8", manager_id="manager-other")
        await _put(pool, channel_id=OTHER_BOT)
        assert await store.list_bindings(channel_id=BOT, manager_id=TARGET, pool=pool) == [first]
        assert len(await store.list_bindings(manager_id=TARGET, pool=pool)) == 2
        assert (await store.list_bindings(channel_id=BOT, manager_id="manager-other", pool=pool))[0]["chat_id"] == "8"
        assert await store.list_bindings(channel_id=OTHER_BOT, manager_id="manager-other", pool=pool) == []

    run_db(run, timeout=30)


@pytest.mark.parametrize("manager_id", ["manager-missing", "manager-review"])
def test_binding_filter_rejects_missing_or_deleted_manager(manager_id):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await pool.execute("UPDATE managers SET enabled = false, deleted_at = now() WHERE id = $1", TARGET)
        with pytest.raises(LookupError):
            await store.list_bindings(manager_id=manager_id, pool=pool)

    run_db(run, timeout=30)


@pytest.mark.parametrize("channel_id,chat_id", [(BOT, "-1001234567890"),
    ("channel-lark-203", "oc_PublicExample"), ("channel-lark_bot-204", "oc_PublicExample")])
def test_each_native_bot_type_accepts_its_native_chat_id(channel_id, chat_id):
    async def run(pool):
        await _seed(pool)
        assert (await _put(pool, chat_id, channel_id=channel_id))["chat_id"] == chat_id

    run_db(run, timeout=30)


@pytest.mark.parametrize("channel_id,chat_id", [(BOT, ""), (BOT, " 7"), (BOT, "0"), (BOT, "oc_example"),
    (BOT, "1" * 21), ("channel-lark-203", "7"), ("channel-lark_bot-204", "oc_" + "x" * 126)])
def test_malformed_native_chat_ids_are_rejected(channel_id, chat_id):
    async def run(pool):
        await _seed(pool)
        with pytest.raises(ValueError):
            await _put(pool, chat_id, channel_id=channel_id)

    run_db(run, timeout=30)


@pytest.mark.parametrize("manager_id", ["manager-missing", "manager-review"])
def test_missing_or_deleted_binding_target_is_refused(manager_id):
    async def run(pool):
        await _seed(pool)
        await pool.execute("UPDATE managers SET enabled = false, deleted_at = now() WHERE id = $1", TARGET)
        with pytest.raises(LookupError):
            await _put(pool, manager_id=manager_id)

    run_db(run, timeout=30)


def test_disabled_target_can_be_bound_and_disabling_it_retains_the_binding():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await pool.execute("UPDATE managers SET enabled = false WHERE id = $1", TARGET)
        row = await _put(pool)
        await pool.execute("UPDATE managers SET enabled = true WHERE id = $1", TARGET)
        await pool.execute("UPDATE managers SET enabled = false WHERE id = $1", TARGET)
        assert await store.list_bindings(manager_id=TARGET, pool=pool) == [row]

    run_db(run, timeout=30)


@pytest.mark.parametrize("channel_id,expected", [("channel-telegram-999", LookupError), ("channel-web", ValueError)])
def test_unknown_or_nonbot_channel_cannot_be_bound(channel_id, expected):
    async def run(pool):
        await _seed(pool)
        if channel_id == "channel-web":
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type) VALUES ('channel-web', 'web')")
        with pytest.raises(expected):
            await _put(pool, channel_id=channel_id)

    run_db(run, timeout=30)


def test_native_looking_id_does_not_make_a_nonbot_channel_bindable():
    async def run(pool):
        await _seed(pool)
        await pool.execute("UPDATE channel_configs SET channel_type = 'web' WHERE channel_id = $1", BOT)
        with pytest.raises(ValueError):
            await _put(pool)

    run_db(run, timeout=30)


@pytest.mark.parametrize("helper", ["put", "delete", "snapshot", "policy"])
def test_routing_helpers_require_a_caller_transaction(helper):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        async with pool.acquire() as conn:
            calls = {"put": lambda: store.put_binding(BOT, "7", TARGET, conn=conn),
                     "delete": lambda: store.delete_binding(BOT, "7", conn=conn),
                     "snapshot": lambda: store.routing_snapshot(BOT, conn=conn),
                     "policy": lambda: store.set_bound_chats_only(BOT, True, conn=conn)}
            with pytest.raises(ValueError, match="transaction"):
                await calls[helper]()

    run_db(run, timeout=30)


def test_default_false_projection_and_config_save_cannot_change_policy(monkeypatch):
    from looper.agents import api_v1
    from looper.agents.channels.allowlist import channel_view
    from looper.agents.v1_domains.channel_configs import CHANNEL_CONFIG, _FIELDS
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        row = await channel_configs.get_config(BOT, pool=pool)
        assert row["bound_chats_only"] is False
        view = channel_view(row)
        api_v1.validate_arguments(CHANNEL_CONFIG, view)
        fixture = {k: v for k, v in row.items() if k != "bound_chats_only"}
        assert channel_view(fixture)["bound_chats_only"] is False
        assert "bound_chats_only" not in _FIELDS
        await channel_configs.set_config(BOT, {"bound_chats_only": True}, pool=pool)
        assert (await channel_configs.get_config(BOT, pool=pool))["bound_chats_only"] is False
        async with pool.acquire() as conn, conn.transaction():
            snapshot = await store.set_bound_chats_only(BOT, True, conn=conn)
            assert snapshot["bound_chats_only"] is True
        await channel_configs.set_config(BOT, {"config": {"allowed_user_ids": ["8"]},
                                              "bound_chats_only": False}, pool=pool)
        assert (await channel_configs.get_config(BOT, pool=pool))["bound_chats_only"] is True
        async with pool.acquire() as conn, conn.transaction():
            new = await channel_configs.create_bot("telegram", "manager", display_name="New bot", enabled=False,
                                                  config={}, secret=None, conn=conn)
        assert new["bound_chats_only"] is False

    run_db(run, monkeypatch, timeout=30)


def test_caller_rollback_reverts_binding_and_policy():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        before = await _put(pool)
        async with pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await store.put_binding(BOT, "7", "manager-other", conn=conn)
            await store.set_bound_chats_only(BOT, True, conn=conn)
            await transaction.rollback()
        assert await store.list_bindings(channel_id=BOT, pool=pool) == [before]
        async with pool.acquire() as conn, conn.transaction():
            snapshot = await store.routing_snapshot(BOT, conn=conn)
        assert snapshot == {"channel_id": BOT, "manager_id": "manager", "bound_chats_only": False,
                            "bindings": [before]}

    run_db(run, timeout=30)


@pytest.mark.parametrize("first", ["put", "delete"])
def test_put_and_delete_of_the_same_pair_serialize_on_the_channel(first):
    from looper.db import chat_bindings as store

    async def remove(pool):
        async with pool.acquire() as conn, conn.transaction():
            return await store.delete_binding(BOT, "7", conn=conn)

    async def run(pool):
        await _seed(pool)
        await _put(pool)
        async with pool.acquire() as conn, conn.transaction():
            if first == "put":
                await store.put_binding(BOT, "7", "manager-other", conn=conn)
                other = asyncio.create_task(remove(pool))
            else:
                assert await store.delete_binding(BOT, "7", conn=conn)
                other = asyncio.create_task(_put(pool, manager_id="manager-other"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(other), 0.05)
        result = await other
        if first == "put":
            assert result is True
            await _put(pool, "8")
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings "
                                       "WHERE channel_id = $1 AND chat_id = '8'", BOT) == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings "
                                       "WHERE channel_id = $1 AND chat_id = '7'", BOT) == 0
        else:
            assert await store.list_bindings(channel_id=BOT, pool=pool) == [result]
            assert result["manager_id"] == "manager-other"

    run_db(run, timeout=30)


@pytest.mark.parametrize("winner", ["bind", "delete"])
def test_binding_creation_and_manager_delete_have_one_consistent_winner(monkeypatch, winner):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        control = await _put(pool, manager_id="manager-other", channel_id=OTHER_BOT)
        async with pool.acquire() as conn, conn.transaction():
            if winner == "bind":
                await store.put_binding(BOT, "7", TARGET, conn=conn)
                other = asyncio.create_task(managers.delete_manager(TARGET))
            else:
                @asynccontextmanager
                async def borrowed_connection():
                    yield conn

                with monkeypatch.context() as patch:
                    patch.setattr(managers, "get_pool", lambda: SimpleNamespace(acquire=borrowed_connection))
                    assert await managers.delete_manager(TARGET) == "deleted"
                other = asyncio.create_task(_put(pool))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(other), 0.05)
        if winner == "bind":
            assert await other == "has_bindings"
            assert (await store.list_bindings(manager_id=TARGET, pool=pool))[0]["chat_id"] == "7"
            assert (await managers.get_manager(TARGET, pool=pool))["deleted_at"] is None
        else:
            with pytest.raises(LookupError):
                await other
            assert await managers.get_manager(TARGET, include_deleted=True, pool=pool)
            assert await store.list_bindings(manager_id="manager-other", pool=pool) == [control]
            with pytest.raises(LookupError):
                await store.list_bindings(manager_id=TARGET, pool=pool)
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE manager_id = $1", "manager-other") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE manager_id = $1", TARGET) == 0

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("winner", ["bind", "remove"])
def test_binding_creation_and_bot_removal_never_leave_dangling_rows(winner):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await _put(pool, channel_id=OTHER_BOT)
        async with pool.acquire() as conn, conn.transaction():
            if winner == "bind":
                await store.put_binding(BOT, "7", TARGET, conn=conn)
                other = asyncio.create_task(channel_configs.delete_bot(BOT, pool=pool))
            else:
                await conn.execute("DELETE FROM channel_configs WHERE channel_id = $1", BOT)
                other = asyncio.create_task(_put(pool))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(other), 0.05)
        if winner == "bind":
            assert await other == "deleted"
        else:
            with pytest.raises(LookupError):
                await other
        assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE channel_id = $1", OTHER_BOT) == 1
        assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings WHERE channel_id = $1", BOT) == 0

    run_db(run, timeout=30)


def test_remove_bot_cascades_metadata_but_preserves_thread_messages_tasks_and_sequence():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        async with pool.acquire() as conn, conn.transaction():
            bot = await channel_configs.create_bot("telegram", "manager", display_name="Disposable bot",
                                                  enabled=False, config={}, secret=None, conn=conn)
        bot_id = bot["channel_id"]
        session_id = f"{bot_id}:7"
        await _put(pool, channel_id=bot_id)
        await store.record_seen(bot_id, "7", "Synthetic group", suppressed=True, pool=pool)
        await chat.claim_thread(session_id, TARGET, pool=pool)
        await chat.save_message(session_id=session_id, message_id="message-one", role="user",
                                content="Synthetic public example", pool=pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-example', 'Example')")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, manager_id, origin_channel_id, chat_id) "
                           "VALUES ('task-example', 'proj-example', 'Inspect example', $1, $2, '7')", TARGET, bot_id)
        await pool.execute("INSERT INTO manager_actions (channel, chat, tool, risk, status, manager_id, task_id) "
                           "VALUES ($1, '7', 'read_example', 'read', 'ok', $2, 'task-example')", bot_id, TARGET)
        task_before = await pool.fetchrow("SELECT * FROM tasks WHERE id = 'task-example'")
        audit_before = await pool.fetch("SELECT * FROM manager_actions")
        await _put(pool, channel_id=OTHER_BOT)
        await store.record_seen(OTHER_BOT, "7", suppressed=True, pool=pool)
        assert await channel_configs.delete_bot(bot_id, pool=pool) == "deleted"
        for table in ("manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", OTHER_BOT) == 1
            assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", bot_id) == 0
        assert await chat.thread_owner(session_id, pool=pool) == TARGET
        assert (await chat.list_messages(session_id, pool=pool))[0]["content"] == "Synthetic public example"
        assert await pool.fetchrow("SELECT * FROM tasks WHERE id = 'task-example'") == task_before
        assert await pool.fetch("SELECT * FROM manager_actions") == audit_before
        async with pool.acquire() as conn, conn.transaction():
            replacement = await channel_configs.create_bot("telegram", "manager", display_name="Replacement",
                                                          enabled=False, config={}, secret=None, conn=conn)
        assert int(replacement["channel_id"].rsplit("-", 1)[1]) > int(bot_id.rsplit("-", 1)[1])

    run_db(run, timeout=30)


def test_seen_and_suppressed_counts_are_independent_and_binding_keeps_suppression_history():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await store.record_seen(BOT, "7", "First", suppressed=False, pool=pool)
        await store.record_seen(BOT, "7", "Suppressed", suppressed=True, pool=pool)
        await _put(pool)
        await store.record_seen(BOT, "7", "Latest", suppressed=False, pool=pool)
        row = (await store.list_channel_chats(BOT, pool=pool))[0]
        assert row["count"] == 3 and row["unbound_count"] == 1
        assert row["name"] == "Latest" and row["manager_id"] == TARGET
        assert isinstance(row["last_at"], datetime) and isinstance(row["unbound_last_at"], datetime)

    run_db(run, timeout=30)


def test_names_are_nullable_bounded_and_last_nonempty_observation_wins():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await store.record_seen(BOT, "7", suppressed=True, pool=pool)
        assert (await store.list_channel_chats(BOT, pool=pool))[0]["name"] is None
        await store.record_seen(BOT, "7", "名" * 201, suppressed=True, pool=pool)
        for name in (None, "", "   "):
            await store.record_seen(BOT, "7", name, suppressed=True, pool=pool)
        assert (await store.list_channel_chats(BOT, pool=pool))[0]["name"] == "名" * 200
        await store.record_seen(BOT, "7", "Renamed", suppressed=True, pool=pool)
        for table in ("channel_seen_chats", "channel_unbound_chats"):
            assert await pool.fetchval(f"SELECT name FROM {table} WHERE channel_id = $1", BOT) == "Renamed"

    run_db(run, timeout=30)


def test_concurrent_observations_keep_all_seen_chats_but_only_twenty_suppressed():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await store.record_seen(OTHER_BOT, "1", "Other bot", suppressed=True, pool=pool)
        await asyncio.gather(*(store.record_seen(BOT, str(n), suppressed=True, pool=pool) for n in range(1, 22)))
        assert len(await store.list_channel_chats(BOT, pool=pool)) == 21
        assert await pool.fetchval("SELECT count(*) FROM channel_unbound_chats WHERE channel_id = $1", BOT) == 20
        assert (await store.list_channel_chats(OTHER_BOT, pool=pool))[0]["unbound_count"] == 1
        await asyncio.gather(*(store.record_seen(BOT, "1", suppressed=True, pool=pool) for _ in range(3)))
        assert await pool.fetchval("SELECT count FROM channel_seen_chats WHERE channel_id = $1 AND chat_id = '1'", BOT) == 4

    run_db(run, timeout=30)


def test_pruning_uses_newest_timestamp_and_native_id_as_stable_tiebreaker():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        older = datetime.now(timezone.utc) - timedelta(days=1)
        ids = [str(n) for n in range(1, 21)]
        await pool.executemany("INSERT INTO channel_unbound_chats (channel_id, chat_id, last_at) VALUES ($1, $2, $3)",
                               [(BOT, value, older) for value in ids])
        await store.record_seen(BOT, "99", suppressed=True, pool=pool)
        rows = await pool.fetch("SELECT chat_id FROM channel_unbound_chats WHERE channel_id = $1 "
                                "ORDER BY last_at DESC, chat_id ASC", BOT)
        assert [r["chat_id"] for r in rows] == ["99", *sorted(ids)[:19]]

    run_db(run, timeout=30)


def test_metadata_tables_contain_no_message_sender_profile_or_credential_fields():
    async def run(pool):
        for table in ("channel_seen_chats", "channel_unbound_chats"):
            columns = await pool.fetch("SELECT column_name FROM information_schema.columns "
                                       "WHERE table_schema = current_schema() AND table_name = $1", table)
            assert {row["column_name"] for row in columns} == {"channel_id", "chat_id", "name", "count", "last_at"}

    run_db(run, timeout=30)


def test_catalog_pagination_unions_seen_and_never_observed_bindings():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        for chat_id in ("1", "2", "10"):
            await store.record_seen(BOT, chat_id, suppressed=False, pool=pool)
        await _put(pool, "3")
        await _put(pool, "10")
        first = await store.list_channel_chats(BOT, limit=2, pool=pool)
        second = await store.list_channel_chats(BOT, limit=2, after=first[-1]["chat_id"], pool=pool)
        assert [row["chat_id"] for row in first + second] == ["1", "10", "2", "3"]
        unseen = second[-1]
        assert unseen["count"] is None and unseen["last_at"] is None and unseen["manager_id"] == TARGET
        assert isinstance(unseen["binding_created_at"], datetime)
        assert all(row["unbound_count"] is None and row["unbound_last_at"] is None for row in first + second)
        assert await store.list_channel_chats(BOT, after="3", pool=pool) == []

    run_db(run, timeout=30)


@pytest.mark.parametrize("limit", [0, 201])
def test_catalog_rejects_out_of_range_page_limits(limit):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        with pytest.raises(ValueError):
            await store.list_channel_chats(BOT, limit=limit, pool=pool)

    run_db(run, timeout=30)


def test_recording_from_removed_bot_is_a_noop_without_affecting_another_bot():
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await store.record_seen(OTHER_BOT, "7", suppressed=True, pool=pool)
        assert await channel_configs.delete_bot(BOT, pool=pool) == "deleted"
        await store.record_seen(BOT, "7", "Stale adapter", suppressed=True, pool=pool)
        for table in ("channel_seen_chats", "channel_unbound_chats"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", OTHER_BOT) == 1
            assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", BOT) == 0

    run_db(run, timeout=30)


def test_metadata_failure_rolls_back_both_counters_and_logs_without_raising(caplog):
    from looper.db import chat_bindings as store

    async def run(pool):
        await _seed(pool)
        await store.record_seen(BOT, "7", suppressed=True, pool=pool)
        await pool.execute("ALTER TABLE channel_unbound_chats ADD CONSTRAINT reject_new_count CHECK (count = 1)")
        await store.record_seen(BOT, "7", "Next observation", suppressed=True, pool=pool)
        assert await pool.fetchval("SELECT count FROM channel_seen_chats WHERE channel_id = $1", BOT) == 1
        assert await pool.fetchval("SELECT count FROM channel_unbound_chats WHERE channel_id = $1", BOT) == 1
        assert any(record.name == store.__name__ for record in caplog.records)

    run_db(run, timeout=30)
