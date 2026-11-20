"""Delivery rows, reply matching, retention and upgrading an older schema."""
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_reply_window_constraint_and_retention(monkeypatch):
    async def check(pool):
        from looper.db import proactive_deliveries as store
        now = datetime.now(timezone.utc)
        for external, age in [("quoted", 5), ("other", 29), ("old", 31)]:
            await store.insert({"source": "gate", "channel_id": "synthetic-channel", "chat": "synthetic-chat",
                "group_chat": True, "outcome": "sent", "external_id": external,
                "sent_at": now - timedelta(minutes=age)})
        await store.mark_replied("synthetic-channel", "synthetic-chat", "quoted", now)
        rows = await pool.fetch("SELECT external_id, reply_match FROM proactive_deliveries ORDER BY id")
        assert [tuple(row) for row in rows] == [("quoted", "quoted"), ("other", "next_message"), ("old", None)]
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE proactive_deliveries SET reply_match='quoted' WHERE external_id='old'")
        assert await store.purge(now - timedelta(minutes=30)) == 1
    run_db(check, monkeypatch)


def test_older_database_gets_delivery_table_idempotently(monkeypatch):
    async def check(pool):
        from looper.db import connection
        await pool.execute("DROP TABLE proactive_deliveries; DELETE FROM schema_migrations WHERE name='proactive_deliveries'")
        async with pool.acquire() as conn:
            await connection.ensure_schema(conn)
            await connection.ensure_schema(conn)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='proactive_deliveries'") == 1
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
