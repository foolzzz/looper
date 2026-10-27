"""Physical cleanup contracts, on shared disposable PostgreSQL schema fixtures only."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from looper.db import chat, manager_cleanup as cleanup, managers
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TARGET = "manager-cleanup"


async def _manager(pool: Any, manager_id: str = TARGET, *, age: int | None = 31) -> None:
    await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) "
                       "VALUES ($1, $1, 'Help.', $2::int IS NULL, "
                       "CASE WHEN $2::int IS NULL THEN NULL ELSE now() - $2 * interval '1 day' END)", manager_id, age)


async def _history(pool: Any, owner: str = TARGET, *, session: str = "arbitrary-thread") -> int:
    await chat.claim_thread(session, owner, client_id="public-client", pool=pool)
    await chat.save_message(session_id=session, message_id="hello", role="user", content="Public sample.", pool=pool)
    record = await pool.fetchval("INSERT INTO llm_records (agent_id, status, input_text, output_text) "
                                 "VALUES ($1, 'completed', 'Public sample.', 'Done.') RETURNING id", owner)
    await pool.execute("INSERT INTO llm_record_events (record_id, seq, event) VALUES ($1, 1, '{}')", record)
    await pool.execute("INSERT INTO manager_actions (manager_id, tool, risk, status) VALUES ($1, 'example', 'read', 'ok')",
                       owner)
    return record


async def _contents(pool: Any) -> dict[str, list[Any]]:
    return {table: [dict(row) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("managers", "chat_threads", "chat_messages", "llm_records", "llm_record_events",
                          "manager_actions")}


@pytest.mark.parametrize("target,age", [("manager", None), (TARGET, None), ("manager-missing", None)])
def test_default_live_and_missing_targets_cannot_purge(target: str, age: int | None) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool, age=age)
        await _history(pool)
        before = await _contents(pool)
        if target != TARGET:
            for operation in (cleanup.preview, cleanup.purge):
                with pytest.raises(cleanup.CleanupIdentityError):
                    await operation(target, pool=pool)
        else:
            preview = await cleanup.preview(target, pool=pool)
            assert not preview["eligible"] and not preview["purged"] and preview["blockers"]
            with pytest.raises(cleanup.CleanupConflict):
                await cleanup.purge(target, pool=pool)
        assert await _contents(pool) == before

    run_db(test, timeout=15)


@pytest.mark.parametrize("retention", [29, 3651, True, 30.0, "30", None])
def test_invalid_retention_is_rejected_without_writes(retention: Any) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        before = await _contents(pool)
        for operation in (cleanup.preview, cleanup.purge):
            with pytest.raises(ValueError, match="retention_days"):
                await operation(TARGET, retention, pool=pool)
        assert await _contents(pool) == before

    run_db(test, timeout=15)


def test_retention_boundary_uses_database_transaction_time() -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn, conn.transaction():
            await _manager(conn, age=30)
            await _history(conn)
            at_boundary = await cleanup.preview(TARGET, pool=conn)
            assert at_boundary["eligible"] and at_boundary["retention_days"] == 30
            await conn.execute("UPDATE managers SET deleted_at = deleted_at + interval '1 microsecond' WHERE id = $1", TARGET)
            before = await _contents(conn)
            assert not (await cleanup.preview(TARGET, pool=conn))["eligible"]
            with pytest.raises(cleanup.CleanupConflict):
                await cleanup.purge(TARGET, pool=conn)
            assert await _contents(conn) == before
            await conn.execute("UPDATE managers SET deleted_at = now() - interval '3650 days' WHERE id = $1", TARGET)
            assert (await cleanup.purge(TARGET, 3650, pool=conn))["purged"]

    run_db(test, timeout=15)


def test_exact_owner_cleanup_preserves_neighbors_and_identity_tombstones() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _manager(pool, "manager-cleanup-neighbor", age=None)
        target_record = await _history(pool, session="unrelated-public-session")
        await _history(pool, "manager", session="manager-cleanup:default-looking-name")
        await _history(pool, "manager-cleanup-neighbor", session="scenario")
        # Planner and legacy unowned history are positive controls, regardless of session/role naming.
        await chat.save_message(session_id="planner-example", message_id="planner", role=TARGET,
                                content="Planner sample.", pool=pool)
        await chat.save_message(session_id="legacy", message_id="legacy", role="user", content="Legacy.", pool=pool)
        await pool.execute("INSERT INTO llm_records (agent_id, status) VALUES ('planner-example', 'completed')")
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type) VALUES ('sample-bot', 'lark')")
        await pool.execute("INSERT INTO channel_seen_chats (channel_id, chat_id) VALUES ('sample-bot', 'seen')")
        await pool.execute("INSERT INTO channel_unbound_chats (channel_id, chat_id) VALUES ('sample-bot', 'unbound')")
        await pool.execute("INSERT INTO channel_rejected_senders (channel_id, sender_id) VALUES ('sample-bot', 'rejected')")
        before = await _contents(pool)
        metadata_tables = ("channel_seen_chats", "channel_unbound_chats", "channel_rejected_senders")
        metadata = {name: await pool.fetch(f"SELECT * FROM {name}") for name in metadata_tables}
        preview = await cleanup.preview(TARGET, pool=pool)
        assert preview["counts"] == {"threads": 1, "chat_messages": 1, "llm_records": 1,
                                     "llm_record_events": 1, "manager_actions": 1}
        assert not preview["purged"] and await _contents(pool) == before
        purged = await cleanup.purge(TARGET, pool=pool)
        assert purged == {**preview, "purged": True}
        after = await _contents(pool)
        assert {name: await pool.fetch(f"SELECT * FROM {name}") for name in metadata_tables} == metadata
        assert after["managers"] == before["managers"] and after["chat_threads"] == before["chat_threads"]
        assert after["chat_messages"] == [r for r in before["chat_messages"] if r["session_id"] != "unrelated-public-session"]
        assert after["llm_records"] == [r for r in before["llm_records"] if r["agent_id"] != TARGET]
        assert after["llm_record_events"] == [r for r in before["llm_record_events"] if r["record_id"] != target_record]
        assert after["manager_actions"] == [r for r in before["manager_actions"] if r["manager_id"] != TARGET]
        assert await managers.key_used(TARGET, pool=pool)
        assert await chat.list_messages("unrelated-public-session", pool=pool) == []
        for claimant in ("manager", "manager-cleanup-neighbor"):
            assert await chat.claim_thread("unrelated-public-session", claimant, pool=pool) == TARGET
        assert await chat.thread_client("unrelated-public-session", pool=pool) == "public-client"
        with pytest.raises(asyncpg.UniqueViolationError):
            await _manager(pool)
        again = await cleanup.purge(TARGET, pool=pool)
        assert again["purged"] and again["counts"] == {**dict.fromkeys(preview["counts"], 0), "threads": 1}

    run_db(test, timeout=15)


def test_running_status_blocks_but_completed_legacy_null_finished_at_does_not() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        record = await _history(pool)
        assert await pool.fetchval("SELECT finished_at FROM llm_records WHERE id = $1", record) is None
        assert (await cleanup.preview(TARGET, pool=pool))["eligible"]
        await pool.execute("UPDATE llm_records SET status = 'running', finished_at = now() WHERE id = $1", record)
        before = await _contents(pool)
        with pytest.raises(cleanup.CleanupConflict, match="running"):
            await cleanup.purge(TARGET, pool=pool)
        assert await _contents(pool) == before
        await pool.execute("UPDATE llm_records SET status = 'completed', finished_at = NULL WHERE id = $1", record)
        assert (await cleanup.purge(TARGET, pool=pool))["counts"]["llm_records"] == 1

    run_db(test, timeout=15)


@pytest.mark.parametrize("reference", ["tasks", "bots", "bindings", "schedules"])
def test_any_remaining_reference_blocks_cleanup(reference: str) -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        if reference == "tasks":
            await pool.execute("INSERT INTO projects (id, name) VALUES ('sample-project', 'Sample')")
            await pool.execute("INSERT INTO tasks (id, project_id, requirement, manager_id) "
                               "VALUES ('sample-task', 'sample-project', 'Public sample.', $1)", TARGET)
        elif reference == "bots":
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, manager_id) VALUES ('sample-bot', 'lark', $1)", TARGET)
        elif reference == "bindings":
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type) VALUES ('sample-bot', 'lark')")
            await pool.execute("INSERT INTO manager_chat_bindings (channel_id, chat_id, manager_id) VALUES ('sample-bot', 'chat', $1)", TARGET)
        else:
            await pool.execute("INSERT INTO projects (id, name) VALUES ('sample-project', 'Sample')")
            await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, manager_id) "
                               "VALUES ('sample-schedule', 'sample-project', 'Sample', '0 0 * * *', 'UTC', 'Sample.', $1)", TARGET)
        before = await _contents(pool)
        preview = await cleanup.preview(TARGET, pool=pool)
        assert not preview["eligible"] and any(reference in b for b in preview["blockers"])
        with pytest.raises(cleanup.CleanupConflict):
            await cleanup.purge(TARGET, pool=pool)
        assert await _contents(pool) == before

    run_db(test, timeout=15)


def test_enabled_deleted_inconsistent_manager_is_refused() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await pool.execute("ALTER TABLE managers DROP CONSTRAINT managers_deleted_check")
        await pool.execute("UPDATE managers SET enabled = true WHERE id = $1", TARGET)
        with pytest.raises(cleanup.CleanupConflict, match="enabled"):
            await cleanup.purge(TARGET, pool=pool)

    run_db(test, timeout=15)


def test_purge_rechecks_eligibility_instead_of_trusting_preview() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        assert (await cleanup.preview(TARGET, pool=pool))["eligible"]
        await pool.execute("UPDATE managers SET deleted_at = now() WHERE id = $1", TARGET)
        before = await _contents(pool)
        with pytest.raises(cleanup.CleanupConflict, match="retention"):
            await cleanup.purge(TARGET, pool=pool)
        assert await _contents(pool) == before

    run_db(test, timeout=15)


def test_concurrent_purges_serialize_under_manager_row_lock() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        entered, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            entered.set()
            await release.wait()

        async def note() -> None:
            second_entered.set()

        first = asyncio.create_task(cleanup.purge(TARGET, pool=pool, before_purge=hold))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(cleanup.purge(TARGET, pool=pool, before_purge=note))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_entered.wait(), 0.05)
            release.set()
            one, two = await asyncio.gather(first, second)
            assert one["counts"]["chat_messages"] == 1
            assert two["counts"] == {"threads": 1, "chat_messages": 0, "llm_records": 0,
                                     "llm_record_events": 0, "manager_actions": 0}
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)

    run_db(test, timeout=15)


def test_database_failure_rolls_back_all_deletions() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        before = await _contents(pool)
        await pool.execute("CREATE FUNCTION refuse_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$ "
                           "BEGIN RAISE EXCEPTION 'injected cleanup failure'; END $$")
        await pool.execute("CREATE TRIGGER refuse_cleanup BEFORE DELETE ON manager_actions "
                           "FOR EACH ROW EXECUTE FUNCTION refuse_cleanup()")
        with pytest.raises(asyncpg.RaiseError, match="injected cleanup failure"):
            await cleanup.purge(TARGET, pool=pool)
        assert await _contents(pool) == before

    run_db(test, timeout=15)


def test_runtime_recheck_failure_is_before_any_deletion() -> None:
    async def test(pool: Any) -> None:
        await _manager(pool)
        await _history(pool)
        before = await _contents(pool)

        async def still_retiring() -> None:
            raise RuntimeError("retirement is pending")

        with pytest.raises(RuntimeError, match="pending"):
            await cleanup.purge(TARGET, pool=pool, before_purge=still_retiring)
        assert await _contents(pool) == before

    run_db(test, timeout=15)
