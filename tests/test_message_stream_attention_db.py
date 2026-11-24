"""Source alerts join existing delivery attention without running channel health checks."""

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import attention
from looper.db import message_stream_alerts as alerts
from tests import subscription_support as stream
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_source_alerts_share_deliveries_without_reading_channel_status(monkeypatch):
    async def check(pool):
        await stream.source(pool)
        await pool.execute("INSERT INTO source_collection_settings (source_id, alerts_enabled) VALUES ($1,true)", stream.SOURCE)
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            await alerts.open_episode(conn, stream.SOURCE, "auth", now=now - timedelta(days=5))
        monkeypatch.setattr(attention, "_catchup_checks", lambda: [{"id": "lark_catchup:synthetic", "label": "Lark",
            "detail": "A synthetic catch-up window was dropped", "fix": "Check catch-up", "status": "fail"}])

        async def forbidden():
            pytest.fail("delivery attention must not run full channel status")

        monkeypatch.setattr(attention, "_status_checks", forbidden)
        items, count = await attention._deliveries(None)
        assert count == 2 and {item["facts"]["source"] for item in items} == {"catchup", "message_stream"}
        source = next(item for item in items if item["facts"]["source"] == "message_stream")
        assert source["href"] == "#messages?collection=stream" and source["facts"]["kind"] == "auth"
        assert (await attention._earliest_deliveries(None))["key"] == source["key"]
        assert "消息流采集/对账需处理" in attention._line("deliveries", source, now)
        async with pool.acquire() as conn:
            await alerts.resolve_episode(conn, stream.SOURCE, "auth", now=now - timedelta(hours=73))
            await conn.execute("UPDATE source_stream_alerts SET recovery_state='unknown'")
        items, count = await attention._deliveries(None)
        assert count == 1 and items[0]["facts"]["source"] == "catchup"

    run_db(check, monkeypatch, timeout=30)


def test_disabled_alerts_do_not_read_source_event_tables(monkeypatch):
    async def check(pool):
        # A positive read and a deliberately unavailable event table prove the opt-in guard does not scan it.
        await stream.source(pool)
        await stream.add(pool, "Synthetic data", sent_at=datetime.now(timezone.utc))
        assert await pool.fetchval("SELECT count(*) FROM source_events") == 1
        await pool.execute("ALTER TABLE source_events RENAME TO source_events_unavailable")
        assert await attention._source_stream_items() == ([], 0)
        await pool.execute("ALTER TABLE source_events_unavailable RENAME TO source_events")

    run_db(check, monkeypatch, timeout=30)


def test_source_alert_read_failure_is_an_error_not_empty(monkeypatch):
    async def check(pool):
        await stream.source(pool)
        await pool.execute("INSERT INTO source_collection_settings (source_id, alerts_enabled) VALUES ($1,true)", stream.SOURCE)
        await pool.execute("ALTER TABLE source_stream_alerts RENAME TO source_alerts_unavailable")
        summary = await attention.collect()
        section = next(item for item in summary["sections"] if item["kind"] == "deliveries")
        assert section["state"] == "error" and section["items"] == []
        await pool.execute("ALTER TABLE source_alerts_unavailable RENAME TO source_stream_alerts")

    run_db(check, monkeypatch, timeout=30)
