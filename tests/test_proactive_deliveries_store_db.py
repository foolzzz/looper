"""Delivery rows, reply matching, retention and upgrading an older schema."""
import contextlib
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.db_support import DSN, dropped_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_reply_window_and_first_reply_are_preserved(monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        now = datetime.now(timezone.utc)
        for external, age in [("quoted", 5), ("other", 29), ("boundary", 30),
                              ("old", 31), ("future", -1)]:
            await store.insert({"source": "gate", "channel_id": "synthetic-channel", "chat": "synthetic-chat",
                "chat_kind": "group", "outcome": "sent", "external_id": external,
                "sent_at": now - timedelta(minutes=age)})
        await store.mark_replied("synthetic-channel", "synthetic-chat", "quoted", now)
        await store.insert({"source": "gate", "channel_id": "other-channel", "chat": "synthetic-chat",
                            "chat_kind": "group", "outcome": "sent", "external_id": "other-channel", "sent_at": now})
        await store.insert({"source": "gate", "channel_id": "synthetic-channel", "chat": "other-chat",
                            "chat_kind": "group", "outcome": "sent", "external_id": "other-chat", "sent_at": now})
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
        async with dropped_tables(pool, "proactive_deliveries") if upgrade else contextlib.nullcontext():
            if upgrade:
                await pool.execute("DELETE FROM schema_migrations WHERE name='proactive_deliveries'")
            async with pool.acquire() as conn:
                await connection.ensure_schema(conn)
                await connection.ensure_schema(conn)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        assert await pool.fetchval("SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                                   "AND conrelid = 'morning_brief_consumed'::regclass "
                                   "AND confrelid = 'proactive_deliveries'::regclass") == 1
        # Fresh schemas use the baseline marker; upgrades record the one named migration.
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='proactive_deliveries'") == int(upgrade)
        columns = await pool.fetch("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                   "WHERE table_schema=current_schema() AND table_name='proactive_deliveries'")
        expected = {name: "text" for name in ("dedupe_key", "urgency", "task_id", "step_id",
                                              "schedule_id", "decision", "reason")}
        expected.update(counted="boolean", local_date="date", digest_date="date", decided_at="timestamp with time zone")
        assert {row["column_name"]: row["data_type"] for row in columns if row["column_name"] in expected} == expected
        assert all(row["is_nullable"] == "YES" for row in columns if row["column_name"] in expected)
        counted_index = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                                            "AND indexname='idx_proactive_deliveries_counted'")
        assert counted_index.endswith("(channel_id, chat, local_date) WHERE counted")
        from looper.db import proactive_deliveries as store
        row = {"source": "gate", "channel_id": "synthetic-channel", "chat": "synthetic-chat",
               "chat_kind": "group", "outcome": "sent"}
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


def test_housekeeping_retains_ninety_days_and_drains_batches_when_rows_exist(monkeypatch, tmp_path):
    """Old rows are removed in batches even when metrics are off. Delivery writes this table without that switch.
    The cutoff row and a current row stay. A later pass with nothing older deletes nothing."""
    from looper.agents import chat_settings, housekeeping

    async def check(pool):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=90)
        await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, outcome, sent_at) "
                           "SELECT 'gate', 'synthetic-channel', 'synthetic-chat', 'direct', 'sent', $1::timestamptz "
                           "- interval '1 second' FROM generate_series(1, 5001)", cutoff)
        for sent in (cutoff, now):
            await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, outcome, sent_at) "
                               "VALUES ('gate', 'synthetic-channel', 'synthetic-chat', 'direct', 'sent', $1)", sent)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
        report = await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)
        assert report.proactive_deliveries == 5001 and not report.failed_steps
        assert await pool.fetchval("SELECT array_agg(sent_at ORDER BY sent_at) FROM proactive_deliveries") == [cutoff, now]
        assert (await housekeeping.run_once(tmp_path, now, current_runner=None, commands=None)).proactive_deliveries == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("decision", ["digest", "drop"])
def test_purge_unsent_decisions_uses_decided_at_and_preserves_the_cutoff(decision, monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=store.RETENTION_DAYS)
        for ref, decided in (("expired", cutoff - timedelta(seconds=1)), ("boundary", cutoff), ("recent", now)):
            await pool.execute("INSERT INTO proactive_deliveries (source, source_ref, channel_id, chat, chat_kind, "
                               "decision, decided_at) VALUES ('subscription', $1, 'synthetic-channel', "
                               "'synthetic-chat', 'direct', $2, $3)", ref, decision, decided)
        # A recent receipt takes precedence over the older policy decision.
        await pool.execute("INSERT INTO proactive_deliveries (source, source_ref, channel_id, chat, chat_kind, "
                           "decision, decided_at, outcome, sent_at) VALUES ('gate_reminder', 'sent', "
                           "'synthetic-channel', 'synthetic-chat', 'direct', 'send', $1, 'sent', $2)",
                           cutoff - timedelta(seconds=1), now)
        assert await store.purge(cutoff) == 1
        assert await pool.fetchval("SELECT array_agg(source_ref ORDER BY id) FROM proactive_deliveries") == [
            "boundary", "recent", "sent"]
        assert await store.purge(cutoff) == 0
    run_db(check, monkeypatch)


def test_daily_counts_use_the_server_timezone(monkeypatch):
    from looper.agents import cron
    from looper.db import proactive_deliveries as store

    monkeypatch.setattr(cron, 'local_zone_name', lambda: 'Pacific/Kiritimati')
    async def check(pool):
        sent = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=12)
        await store.insert({'source': 'schedule', 'channel_id': 'synthetic-channel', 'chat': 'synthetic-chat',
                            'chat_kind': 'direct', 'outcome': 'sent', 'sent_at': sent})
        result = await store.summary(7)
        assert result['per_day'] == [{'date': (sent + timedelta(hours=14)).date().isoformat(),
                                     'by_source': {'schedule': 1}}]
    run_db(check, monkeypatch)


def test_unsent_decisions_allow_null_receipts_and_reject_partial_receipts(monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        columns = await pool.fetch("SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                                   "WHERE table_schema=current_schema() AND table_name='proactive_deliveries' "
                                   "AND column_name IN ('outcome', 'sent_at')")
        assert len(columns) == 2
        assert all(row["is_nullable"] == "YES" and row["column_default"] is None for row in columns)
        for decision in ("digest", "drop"):
            await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, decision, "
                               "decided_at) VALUES ('subscription', 'synthetic-channel', 'synthetic-chat', "
                               "'direct', $1, now())", decision)
        now = datetime.now(timezone.utc)
        await store.mark_replied("synthetic-channel", "synthetic-chat", None, now)
        assert await pool.fetchval("SELECT count(replied_at) FROM proactive_deliveries") == 0
        assert await store.summary(7) == {"per_day": [], "reply_rate": {"all": None, "by_source": {}}}
        for assignment in ("outcome='sent'", "sent_at=now()"):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE proactive_deliveries SET {assignment}")
    run_db(check, monkeypatch)


def test_delivery_schema_has_one_source_and_one_chat_kind(monkeypatch):
    async def check(pool):
        from looper.agents.turn_metrics import ProactiveSource
        columns = {row["column_name"]: row["data_type"] for row in await pool.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='proactive_deliveries'")}
        assert "kind" not in columns and "group_chat" not in columns
        assert columns["source"] == columns["chat_kind"] == "text"
        assert ProactiveSource("user_reminder").value == "user_reminder"
        for source in ProactiveSource:
            await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind) "
                               "VALUES ($1, 'synthetic-channel', 'synthetic-chat', 'direct')", source.value)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE proactive_deliveries SET source='completion_turn'")
    run_db(check, monkeypatch)


def test_receipt_retry_preserves_the_first_decision(monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        now = datetime.now(timezone.utc)
        row = await store.insert({"source": "gate", "source_ref": "gate-a", "channel_id": "synthetic-channel",
            "chat": "synthetic-chat", "chat_kind": "direct", "dedupe_key": "gate-message-a", "outcome": "unknown",
            "sent_at": now})
        await pool.execute("UPDATE proactive_deliveries SET dedupe_key='gate-message-a', decision='send', "
                           "reason='within_limits', counted=true, local_date=$2, decided_at=$3 WHERE id=$1",
                           row["id"], now.date(), now)
        final = await store.insert({"source": "subscription", "source_ref": "later-ref", "channel_id": "synthetic-channel",
            "chat": "synthetic-chat", "chat_kind": "direct", "dedupe_key": "gate-message-a", "outcome": "sent",
            "external_id": "message-a", "sent_at": now + timedelta(seconds=1)})
        assert final["id"] == row["id"] and await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1
        assert (final["source"], final["source_ref"], final["decision"], final["reason"], final["counted"],
                final["local_date"], final["decided_at"]) == (
                    "gate", "gate-a", "send", "within_limits", True, now.date(), now)
        assert (final["outcome"], final["external_id"], final["sent_at"]) == (
            "sent", "message-a", now + timedelta(seconds=1))
    run_db(check, monkeypatch)
