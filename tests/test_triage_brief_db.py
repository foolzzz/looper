"""Brief candidates keep one owner per message and wait for an important event's settled fallback."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from looper.agents.triage_model import Category
from looper.db import triage_events
from tests.db_support import DSN, run_db
from tests.subscription_support import add, source

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 23, 9, 15, tzinfo=timezone.utc)


async def seed(pool):
    await source(pool)
    await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic')")
    await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                       "VALUES ('s', 'p', 'Triage', '0 * * * *', 'UTC', 'subscription', 'r')")


async def run(pool, name, messages=1):
    await pool.execute("INSERT INTO schedule_runs (id, schedule_id, status, trigger, scheduled_for) "
                       "VALUES ($1, 's', 'completed', 'manual', $2)", name, NOW)
    seqs = [await add(pool, f"Synthetic message {name}-{n}", sent_at=NOW) for n in range(messages)]
    await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, window_seqs) "
                       "VALUES ($1, 0, $2, $3)", name, seqs[-1], seqs)
    return seqs


async def event(pool, run_id, seqs, *, category="digest", state=None, reason=None, created=NOW, sent=NOW,
                flagged=False):
    event_id = await pool.fetchval(
        "INSERT INTO triage_events (source_id, schedule_id, run_id, category, title, summary, reason, reason_kind, "
        "confidence, flagged, first_sent_at, last_sent_at, created_at) VALUES ('stream', 's', $1, $2, "
        "CASE WHEN $2 = 'ignore' THEN '' ELSE 'Title' END, "
        "CASE WHEN $2 = 'ignore' THEN '' ELSE 'Summary' END, 'Reason', 'model', 'high', $3, $4, $4, $5) RETURNING id", run_id, category, flagged, sent, created)
    await pool.executemany("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)",
                           [(event_id, seq) for seq in seqs])
    if state:
        manager_id = await pool.fetchval(
            "INSERT INTO manager_events (manager_id, kind, origin, target_kind, channel_id, chat, dedupe_key, "
            "payload, fallback, state, reason, batch, due_at) VALUES ('manager', 'triage', 'chat', 'dm', "
            "'channel-lark', 'ou_synthetic', $1, '{}', '[]', $2, $3, $4, $5) RETURNING id",
            f"triage:{event_id}", state, reason, uuid4() if state in ("running", "delivered") else None,
            NOW if state == "pending" else None)
        await pool.execute("UPDATE triage_events SET manager_event_id = $2 WHERE id = $1", event_id, manager_id)
    return event_id


def test_only_the_highest_category_then_earliest_owner_is_in_the_brief(monkeypatch):
    async def check(pool):
        await seed(pool)
        seqs = await run(pool, "one", 3)
        covered = await event(pool, "one", seqs[:1])
        important = await event(pool, "one", seqs[:1], category="important", state="fallback", reason="turn_timeout")
        digest = await event(pool, "one", seqs[1:])
        duplicate = await event(pool, "one", seqs[1:])
        # A cited context message outside the selected window does not make an event an owner.
        context = await add(pool, "Context outside the window", sent_at=NOW)
        unowned = await event(pool, "one", [context])
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            events, more = await triage_events.for_brief(conn, dates_start=NOW)
        assert [row.id for row in events] == [digest, important]
        assert not {covered, duplicate, unowned}.intersection(row.id for row in events)
        assert more == 0 and events[0].category is Category.DIGEST
        assert events[0].chat_name == "Ops room"
        assert (events[0].title, events[0].summary, events[0].last_sent_at) == ("Title", "Summary", NOW)

    run_db(check, monkeypatch)


def test_bulk_ownership_equals_each_run_including_shared_messages_and_context(monkeypatch):
    async def check(pool):
        await seed(pool)
        first = await run(pool, "first", 2)
        second = await run(pool, "second", 2)
        await pool.execute("UPDATE subscription_runs SET window_seqs = $1 WHERE run_id = 'second'",
                           first[:1] + second)
        await event(pool, "first", first)
        await event(pool, "first", first[:1], category="important", state="pending")
        await event(pool, "second", first + second, category="digest")
        await event(pool, "second", second[:1])
        await event(pool, "second", second[1:], category="ignore")
        before = await triage_events.owned_counts("first", db=pool)
        after = await triage_events.owned_counts("second", db=pool)
        assert before and after  # the comparison includes actual ownership, not only empty results
        assert await triage_events.owned_counts_many(["first", "second", "missing"], db=pool) == before | after
        assert await triage_events.owned_counts_many(["first"], db=pool) == before
        assert await triage_events.owned_counts_many([], db=pool) == {}
        assert await triage_events.owned_counts_many(["missing"], db=pool) == {}

    run_db(check, monkeypatch)


@pytest.mark.parametrize("state,reason,listed", [
    ("pending", None, False), ("running", None, False), ("delivered", None, False),
    ("skipped", "manager_silent", False), (None, None, False),
    *[("fallback", reason, True) for reason in (
        "turn_failed", "turn_timeout", "turn_stopped", "empty_reply", "send_failed", "delivery_deferred",
        "daily_cap", "no_chat", "queue_deadline", "restart")],
])
def test_important_events_only_enter_after_fallback_regardless_of_reason(monkeypatch, state, reason, listed):
    async def check(pool):
        await seed(pool)
        seqs = await run(pool, "one", 2)
        important = await event(pool, "one", seqs[:1], category="important", state=state, reason=reason)
        digest = await event(pool, "one", seqs[1:])
        async with pool.acquire() as conn:
            events, more = await triage_events.for_brief(conn, dates_start=NOW)
        assert [row.id for row in events] == ([digest, important] if listed else [digest])
        assert more == 0

    run_db(check, monkeypatch)


def test_window_order_limit_and_consumed_events(monkeypatch):
    async def check(pool):
        await seed(pool)
        seqs = await run(pool, "one", 14)
        old = await event(pool, "one", seqs[:1], created=NOW - timedelta(microseconds=1))
        consumed = await event(pool, "one", seqs[1:2])
        ids = [await event(pool, "one", [seq], sent=NOW - timedelta(minutes=n // 2), flagged=n == 0)
               for n, seq in enumerate(seqs[2:])]
        await pool.execute("INSERT INTO morning_brief_triage (event_id, local_date, channel_id, chat) "
                           "VALUES ($1, $2, 'channel-lark', 'ou_synthetic')", consumed, NOW.date())
        async with pool.acquire() as conn:
            events, more = await triage_events.for_brief(conn, dates_start=NOW)
            all_events, all_more = await triage_events.for_brief(conn, dates_start=NOW, limit=20)
        expected = [value for pair in zip(ids[1::2], ids[::2]) for value in pair]
        assert [row.id for row in events] == expected[:10] and more == 2
        assert [row.id for row in all_events] == expected and all_more == 0
        assert next(row for row in all_events if row.id == ids[0]).flagged is True
        assert {old, consumed}.isdisjoint(row.id for row in all_events)
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_triage") == 1  # reads consume nothing

    run_db(check, monkeypatch)
