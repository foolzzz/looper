"""Status aggregates visit a source's event rows once, including the daily counters."""

import asyncio
import json
from datetime import timedelta

import pytest

from looper.db import message_stream as store
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_status_counts_share_one_event_scan(monkeypatch):
    async def check(pool):
        await store.save_source("src", {"name": "Test"}, db=pool)
        await store.save_source("other", {"name": "Other"}, db=pool)
        async with pool.acquire() as conn, conn.transaction():
            now = await conn.fetchval("SELECT now()")
            await conn.execute("INSERT INTO source_chats (source_id, chat_id, kind) "
                               "VALUES ('src', 'chat', 'group'), ('other', 'chat', 'group')")
            for index, (source, age, body, size, dropped) in enumerate((
                ("src", timedelta(minutes=30), "{}", 10, None),
                ("src", timedelta(hours=2), "{}", 20, None),
                ("src", timedelta(days=2), "{}", 30, None),
                ("src", timedelta(hours=1), None, 40, now),
                ("src", timedelta(days=1), "{}", 50, None),
                ("other", timedelta(), "{}", 100, None),
            )):
                await conn.execute(
                    "INSERT INTO source_events (source_id, external_id, chat_id, sender_kind, sent_at, msg_type, "
                    "mentions_me, text, body, body_bytes, body_dropped_at, origin, ingested_at) "
                    "VALUES ($1, $2, 'chat', 'user', $3, 'text', false, 'synthetic', $4::jsonb, $5, $6, 'import', $3)",
                    source, f"message-{index}", now - age, body, size, dropped)
            queries = []
            with conn.query_logger(queries.append):
                status = await store.status_row("src", db=conn)
                await asyncio.sleep(0)
            assert status == {
                "events": 5, "events_last_hour": 1, "newest_sent_at": now - timedelta(minutes=30),
                "body_bytes": 110, "events_last_day": 3, "body_bytes_last_day": 30, "bodies_dropped": 1,
                "chats": 1, "chats_excluded": 0, "chats_failing": 0, "now": now,
            }
            query = queries[0]
            plan = json.loads(await conn.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + query.query, *query.args))

            def event_scans(node):
                return int(node.get("Relation Name") == "source_events") + sum(
                    event_scans(child) for child in node.get("Plans", []))

            assert event_scans(plan[0]["Plan"]) == 1

    run_db(check, monkeypatch, timeout=20)
