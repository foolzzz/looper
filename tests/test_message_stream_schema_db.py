"""The message stream tables: what the schema refuses, and the upgrade of a database that predates them."""

import asyncpg
import pytest

from looper.db import connection
from tests.db_support import DSN, run_db

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
    # A database older than the stream is older than the leads and intents made from it too.
    leads = ("intent_posts", "intent_sources", "intents", "intent_alert_senders", "message_chat_projects",
             "intent_type_projects", "intent_type_versions", "intent_types", "message_lead_people",
             "message_lead_sources", "message_leads")
    tables = (*leads, "subscription_deliveries", "subscription_runs", "subscriptions", "source_events", "source_runs", "source_threads", "source_people", "source_chats", "event_sources")

    async def check(pool):
        await pool.execute("; ".join(f"DROP TABLE {table}" for table in tables)
                           + "; DELETE FROM schema_migrations WHERE name IN ('message_stream', 'subscriptions', "
                           "'message_leads')")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        for table in tables:
            assert await pool.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        await source(pool)

    run_db(check, monkeypatch, timeout=20)


def test_deleting_a_run_does_not_scan_the_events(monkeypatch):
    async def check(pool):
        index = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE tablename = 'source_events' "
                                    "AND indexdef LIKE '%(run_id)%'")
        assert index and "WHERE" in index

    run_db(check, monkeypatch, timeout=20)
