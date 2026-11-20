"""Delivery rows, reply matching, retention and upgrading an older schema."""
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_reply_window_and_first_reply_are_preserved(monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        now = datetime.now(timezone.utc)
        for external, age in [("quoted", 5), ("other", 29), ("boundary", 30),
                              ("old", 31), ("future", -1)]:
            await store.insert({"source": "gate", "channel_id": "synthetic-channel", "chat": "synthetic-chat",
                "group_chat": True, "outcome": "sent", "external_id": external,
                "sent_at": now - timedelta(minutes=age)})
        await store.mark_replied("synthetic-channel", "synthetic-chat", "quoted", now)
        await store.insert({"source": "gate", "channel_id": "other-channel", "chat": "synthetic-chat",
                            "group_chat": True, "outcome": "sent", "external_id": "other-channel", "sent_at": now})
        await store.insert({"source": "gate", "channel_id": "synthetic-channel", "chat": "other-chat",
                            "group_chat": True, "outcome": "sent", "external_id": "other-chat", "sent_at": now})
        await store.mark_replied("synthetic-channel", "synthetic-chat", "other", now + timedelta(seconds=1))
        rows = await pool.fetch("SELECT external_id, reply_match, replied_at FROM proactive_deliveries ORDER BY id")
        assert [tuple(row) for row in rows] == [
            ("quoted", "quoted", now), ("other", "next_message", now), ("boundary", "next_message", now),
            ("old", None, None), ("future", None, None), ("other-channel", None, None), ("other-chat", None, None)]
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE proactive_deliveries SET reply_match='quoted' WHERE external_id='old'")
    run_db(check, monkeypatch)


@pytest.mark.parametrize("upgrade", [False, True])
def test_delivery_table_has_nullable_decision_columns_and_one_migration(upgrade, monkeypatch):
    async def check(pool):
        from looper.db import connection
        if upgrade:
            await pool.execute("DROP TABLE proactive_deliveries; DELETE FROM schema_migrations WHERE name='proactive_deliveries'")
        async with pool.acquire() as conn:
            await connection.ensure_schema(conn)
            await connection.ensure_schema(conn)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        # Fresh schemas use the baseline marker; upgrades record the one named migration.
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='proactive_deliveries'") == int(upgrade)
        columns = await pool.fetch("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                   "WHERE table_schema=current_schema() AND table_name='proactive_deliveries'")
        expected = {name: "text" for name in ("dedupe_key", "kind", "urgency", "chat_kind", "task_id", "step_id",
                                              "schedule_id", "decision", "reason")}
        expected.update(counted="boolean", local_date="date", digest_date="date", decided_at="timestamp with time zone")
        assert {row["column_name"]: row["data_type"] for row in columns if row["column_name"] in expected} == expected
        assert all(row["is_nullable"] == "YES" for row in columns if row["column_name"] in expected)
        from looper.db import proactive_deliveries as store
        row = {"source": "gate", "channel_id": "synthetic-channel", "chat": "synthetic-chat",
               "group_chat": True, "outcome": "sent"}
        for _ in range(2):
            stored = await store.insert(row)
            assert all(stored[name] is None for name in expected)
        for decision in ("send", "merge", "defer", "digest", "drop"):
            await pool.execute("UPDATE proactive_deliveries SET decision=$1", decision)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE proactive_deliveries SET decision='invalid'")
        await pool.execute("UPDATE proactive_deliveries SET dedupe_key='synthetic-message' WHERE id=$1", stored["id"])
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute("UPDATE proactive_deliveries SET dedupe_key='synthetic-message'")
    run_db(check, monkeypatch)


def test_housekeeping_retains_ninety_days_and_drains_batches_only_when_enabled(monkeypatch, tmp_path):
    from looper.agents import chat_settings, housekeeping

    async def check(pool):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=90)
        await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, group_chat, outcome, sent_at) "
                           "SELECT 'gate', 'synthetic-channel', 'synthetic-chat', false, 'sent', $1::timestamptz "
                           "- interval '1 second' FROM generate_series(1, 5001)", cutoff)
        for sent in (cutoff, now):
            await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, group_chat, outcome, sent_at) "
                               "VALUES ('gate', 'synthetic-channel', 'synthetic-chat', false, 'sent', $1)", sent)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
        report = await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)
        assert report.proactive_deliveries == 0
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 5003
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        report = await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)
        assert report.proactive_deliveries == 5001 and not report.failed_steps
        assert await pool.fetchval("SELECT array_agg(sent_at ORDER BY sent_at) FROM proactive_deliveries") == [cutoff, now]
        assert (await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)).proactive_deliveries == 0
    run_db(check, monkeypatch)


def test_daily_counts_use_the_server_timezone(monkeypatch):
    from looper.agents import cron
    from looper.db import proactive_deliveries as store

    monkeypatch.setattr(cron, 'local_zone_name', lambda: 'Pacific/Kiritimati')
    async def check(pool):
        sent = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=12)
        await store.insert({'source': 'schedule', 'channel_id': 'synthetic-channel', 'chat': 'synthetic-chat',
                            'group_chat': False, 'outcome': 'sent', 'sent_at': sent})
        result = await store.summary(7)
        assert result['per_day'] == [{'date': (sent + timedelta(hours=14)).date().isoformat(),
                                     'by_source': {'schedule': 1}}]
    run_db(check, monkeypatch)
