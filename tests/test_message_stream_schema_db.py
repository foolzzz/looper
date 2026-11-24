"""The message stream tables: what the schema refuses, and the upgrade of a database that predates them."""

import asyncpg
import pytest

from looper.db import connection
from tests import memory_support as memory
from tests.bridge_schema_support import structure
from tests.db_support import DSN, dropped_tables, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def source(pool, source_id="s1", **columns):
    columns = {"kind": "lark_user", "name": "S", **columns}
    names = ", ".join(["id", *columns])
    marks = ", ".join(f"${n}" for n in range(1, len(columns) + 2))
    await pool.execute(f"INSERT INTO event_sources ({names}) VALUES ({marks})", source_id, *columns.values())


def test_the_schema_refuses_an_untyped_state(monkeypatch):
    async def check(pool):
        for columns in ({"ingest_mode": "bridge"}, {"ingest_mode": "mirror"}, {"kind": "slack"}, {"auth_state": "ok"}):
            with pytest.raises(asyncpg.CheckViolationError):
                await source(pool, "bad", **columns)
        await source(pool, "s1", ingest_mode="bridge", bridge_dir="/somewhere")
        await pool.execute("INSERT INTO source_chats (source_id, chat_id, kind) VALUES ('s1', 'c', 'group')")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE source_chats SET inclusion = 'excluded' WHERE chat_id = 'c'")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO source_runs (source_id, mode, trigger, status) "
                               "VALUES ('s1', 'bridge', 'scheduled', 'done')")

    run_db(check, monkeypatch, timeout=20)


def test_an_event_is_unique_per_source_and_goes_with_its_source(monkeypatch):
    async def check(pool):
        insert = ("INSERT INTO source_events (source_id, external_id, chat_id, sender_kind, sent_at, msg_type, "
                  "mentions_me, text, body_bytes, origin) VALUES ($1, $2, 'c', 'user', now(), 'text', false, 't', 1, 'live')")
        for source_id in ("s1", "s2"):
            await source(pool, source_id)
            await pool.execute("INSERT INTO source_chats (source_id, chat_id, kind) VALUES ($1, 'c', 'group')", source_id)
            await pool.execute(insert, source_id, "m1")
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute(insert, "s1", "m1")
        await pool.execute("DELETE FROM event_sources WHERE id = 's1'")
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 1
        assert await pool.fetchval("SELECT count(*) FROM source_chats") == 1

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_tables_gets_them_on_upgrade(monkeypatch):
    # The old stream predates explorer's widening of the same role constraint.
    modules = connection._MIGRATIONS
    monkeypatch.setattr(connection, "_MIGRATIONS", tuple(
        migration for migration in modules if migration.name != "platform_subagents"))
    # A database older than the stream is older than the leads and intents made from it too, and than the memory
    # candidates that point at leads and triage events that point at stream runs.
    leads = ("intent_posts", "intent_sources", "intents", "intent_alert_senders", "message_chat_projects",
             "intent_type_projects", "intent_type_versions", "intent_types", "message_lead_people",
             "message_lead_sources", "message_leads")
    tables = ("morning_brief_triage", "triage_event_sources", "triage_events", *leads, "subscription_deliveries", "subscription_runs", "subscription_daily_claims", "subscriptions",
              "source_deletions", "source_events", "source_runs", "source_threads", "source_people", "source_chats",
              "event_sources")

    async def check(pool):
        monkeypatch.setattr(connection, "_MIGRATIONS", modules)
        async with fresh_connection() as fresh:
            await connection.ensure_schema(fresh)
            expected = await structure(fresh)
        await pool.execute(memory.DROP_MEMORY + "DROP FUNCTION triage_drop_event() CASCADE")
        # Recreating intents must repair its later additions even if their marker survives.
        await pool.execute("INSERT INTO schema_migrations(name) VALUES('intent_group_reads') ON CONFLICT DO NOTHING")
        async with dropped_tables(pool, *tables):
            await pool.execute("DELETE FROM schema_migrations WHERE name IN ('message_stream', 'subscriptions', "
                               "'subscription_rules', 'subscription_lookback', 'message_leads', 'event_triage', "
                               "'event_triage_online')")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            assert await structure(conn) == expected
        for table in tables:
            assert await pool.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        await source(pool)

    run_db(check, monkeypatch, timeout=20)


def test_deleting_a_run_does_not_scan_the_events(monkeypatch):
    async def check(pool):
        index = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE tablename = 'source_events' "
                                    "AND indexdef LIKE '%(run_id)%' AND schemaname = current_schema()")
        assert index and "WHERE" in index

    run_db(check, monkeypatch, timeout=20)


def test_existing_stream_gets_deletion_and_retention_schema(monkeypatch):
    async def check(pool):
        async with dropped_tables(pool, "source_deletions"):
            await pool.execute("ALTER TABLE event_sources DROP COLUMN retention_last_at; "
                               "ALTER TABLE source_runs DROP COLUMN IF EXISTS skipped_by_deletion; "
                               "DELETE FROM schema_migrations WHERE name = 'message_stream_deletions'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            async with pool.acquire() as conn, conn.transaction():
                await connection.ensure_schema(conn)
                assert not await connection.schema_change_pending(conn)
                locks = await conn.fetch(
                    "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND relation = 'source_events'::regclass AND granted")
                assert not {row["mode"] for row in locks} & {
                    "ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"}
        await source(pool)
        assert await pool.fetchval("SELECT retention_last_at FROM event_sources") is None
        await pool.execute("INSERT INTO source_runs (source_id, mode, trigger, status) "
                           "VALUES ('s1', 'import', 'manual', 'ok')")
        assert await pool.fetchval("SELECT skipped_by_deletion FROM source_runs") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_deletions") == 0

    run_db(check, monkeypatch, timeout=20)
