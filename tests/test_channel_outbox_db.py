"""Durable channel ordering, idempotency and retention on disposable PostgreSQL."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.db_support import DSN, drop_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def row(key, chat="chat-a", **extra):
    return dict(channel_id="channel-test", idempotency_key=key, kind="send", chat=chat,
                body={"text": key}, source="answer", **extra)


def test_idempotency_ordering_and_terminal_states(monkeypatch):
    from looper.db import channel_outbox as db

    async def check(pool):
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        first = await db.insert_or_get(row("one"))
        assert (await db.insert_or_get(row("one")))["id"] == first["id"]
        second = await db.insert_or_get(row("two"))
        other = await db.insert_or_get(row("other", "chat-b"))
        assert (await db.next_due("channel-test", now))["id"] == first["id"]
        assert await db.mark_sending(first["id"])
        assert not await db.mark_sending(first["id"])
        assert (await db.next_due("channel-test", now))["id"] == other["id"]
        await db.mark_failed(other["id"], "rejected", "denied")
        await db.retry_later(first["id"], now + timedelta(minutes=1), "rate_limited", "later")
        assert await db.next_due("channel-test", now) is None  # second must not overtake first
        await db.mark_sending(first["id"])
        await db.mark_sent(first["id"], "message-one")
        assert (await db.get(first["id"]))["external_id"] == "message-one"
        assert (await db.get(first["id"]))["attempts"] == 2
        assert (await db.next_due("channel-test", now))["id"] == second["id"]
        await db.mark_uncertain(second["id"], "delivery unknown")
        assert await db.counts("channel-test") == {"pending": 0, "sending": 0, "sent": 1, "failed": 1, "uncertain": 1}
    run_db(check, monkeypatch)


def test_recovery_window_and_retention(monkeypatch):
    from looper.db import channel_outbox as db

    async def check(pool):
        now = datetime.now(timezone.utc)
        recent = await db.insert_or_get(row("recent"))
        old = await db.insert_or_get(row("old", "chat-b"))
        for item in (recent, old):
            await db.mark_sending(item["id"])
            await db.mark_attempted(item["id"], now)
        await pool.execute("UPDATE channel_outbox SET created_at=$1, first_possible_at=$1 WHERE id=$2", now-timedelta(hours=2), old["id"])
        await db.recover("channel-test", now, 3600)
        assert (await db.get(recent["id"]))["state"] == "pending"
        assert (await db.get(old["id"]))["state"] == "uncertain"
        await db.mark_sending(recent["id"])
        await db.recover("channel-test", now, 0)
        assert (await db.get(recent["id"]))["state"] == "uncertain"
        sent = await db.insert_or_get(row("sent"))
        await db.mark_sent(sent["id"], "message-sent")
        await pool.execute("UPDATE channel_outbox SET created_at=$1, sent_at=$1 WHERE id=$2", now-timedelta(days=8), sent["id"])
        await pool.execute("UPDATE channel_outbox SET created_at=$1 WHERE id=$2", now-timedelta(days=31), old["id"])
        assert await db.purge(now) == 2
        assert await db.get(sent["id"]) is None
        assert await db.get(recent["id"]) is not None
    run_db(check, monkeypatch)


def test_upgrade_adds_outbox_and_marker_once(monkeypatch):
    from looper.db import connection

    async def check(pool):
        restore = await drop_tables(pool, "channel_outbox")
        await pool.execute("DELETE FROM schema_migrations WHERE name='channel_outbox'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await restore()
        assert await pool.fetchval("SELECT to_regclass('channel_outbox') IS NOT NULL")
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='channel_outbox'") == 1
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
    run_db(check, monkeypatch)


def test_upgrade_adds_terminal_time_without_retimestamping_legacy_outcomes(monkeypatch):
    from looper.db import connection, channel_outbox as db

    async def check(pool):
        item = await db.insert_or_get(row("legacy-failure"))
        await db.mark_failed(item["id"], "rejected", "denied")
        created_at = item["created_at"]
        await pool.execute("ALTER TABLE channel_outbox DROP COLUMN terminal_at")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        migrated = await db.get(item["id"])
        assert migrated["terminal_at"] is None
        assert migrated["created_at"] == created_at
        assert (await db.metric_counts(created_at))["channel-test"]["failed_24h"] == {"rejected": 1}
        assert await db.metric_counts(created_at + timedelta(microseconds=1)) == {}
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE name='channel_outbox_terminal_at'") == 1
    run_db(check, monkeypatch)


def test_circuit_deferral_preserves_attempt_budget_and_last_failure(monkeypatch):
    from looper.db import channel_outbox as db

    async def check(pool):
        item = await db.insert_or_get(row("deferred"))
        at = datetime.now(timezone.utc) + timedelta(seconds=60)
        await db.retry_later(item['id'], at, 'rate_limited', 'busy')
        await db.defer_unsent(item['id'], at)
        assert (await db.get(item['id']))['attempts'] == 0
        assert await db.mark_sending(item['id'])
        await db.defer_unsent(item['id'], at)
        actual = await db.get(item['id'])
        assert actual['state'] == 'pending' and actual['attempts'] == 0
        assert actual['error_class'] == 'rate_limited' and actual['error'] == 'busy'
        await db.mark_sent(item['id'], 'om_sent')
        await db.defer_unsent(item['id'], at)
        assert (await db.get(item['id']))['state'] == 'sent'
    run_db(check, monkeypatch)


def test_upgrade_backfills_possible_delivery_conservatively(monkeypatch):
    from looper.db import connection, channel_outbox as db

    async def check(pool):
        item = await db.insert_or_get(row('legacy-timeout'))
        await db.mark_sending(item['id'])
        now = datetime.now(timezone.utc)
        await db.retry_later(item['id'], now, 'unknown', 'response lost')
        await pool.execute("UPDATE channel_outbox SET created_at=$1 WHERE id=$2", now-timedelta(hours=2), item['id'])
        await pool.execute('ALTER TABLE channel_outbox DROP COLUMN first_possible_at, DROP COLUMN claim_token')
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        migrated = await db.get(item['id'])
        assert migrated['first_possible_at'] == migrated['created_at']
        await db.recover('channel-test', now, 3600)
        assert (await db.get(item['id']))['state'] == 'uncertain'
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='channel_outbox'") == 1
    run_db(check, monkeypatch)
