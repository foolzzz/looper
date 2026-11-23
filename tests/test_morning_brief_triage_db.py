"""The brief consumes only the triage events it read, and only for the operator DM."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from looper.agents import attention, chat_settings, morning_brief, triage_settings
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.db import morning_briefs as store, triage_events
from tests.db_support import DSN, run_db, run_with_channel_configs
from tests.subscription_support import DM, add, source

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 23, 9, 15, tzinfo=ZONE)
DAY = NOW.date()
CHANNEL = "channel-lark"
BASELINE = Path(__file__).parent / "fixtures" / "morning_brief_before_triage.json"


async def baseline_observation(pool):
    """Fixed synthetic day, recorded before the triage section was implemented."""
    await baseline_digest(pool)
    deferred = await store.digests(pool, channel_id=CHANNEL, chat=DM, until=NOW, dates=[DAY])
    text = morning_brief.render(day=DAY, needs=attention.NOTHING, reminders=[], digests=deferred, tz=ZONE)
    assert await store.claim(pool, DAY, CHANNEL, DM, NOW, attempts_limit=3, read_ids=[9001])
    await pool.execute("UPDATE morning_briefs SET created_at = $1", NOW)
    claimed = dict(await pool.fetchrow("SELECT * FROM morning_briefs"))
    async with pool.acquire() as conn:
        assert await store.settle(conn, DAY, CHANNEL, DM, "sent", now=NOW, consumed_until=NOW, read_ids=[9001])
    settled = dict(await pool.fetchrow("SELECT * FROM morning_briefs"))
    return await observation(pool, text, claimed, settled)


async def baseline_digest(pool):
    await pool.execute(
        "INSERT INTO proactive_deliveries (id, dedupe_key, source, urgency, channel_id, chat, chat_kind, "
        "decision, reason, counted, local_date, digest_date, decided_at) VALUES "
        "(9001, 'baseline', 'task_notice', 'normal', $1, $2, 'direct', 'digest', 'quiet_hours', "
        "false, $3, $3, $4)", CHANNEL, DM, DAY, NOW)


async def observation(pool, text, claimed, settled):
    for row in (claimed, settled):
        if "triage_ids" in row:
            assert row.pop("triage_ids") == []
    return json.loads(json.dumps({"text": text, "claimed": claimed, "settled": settled,
                                 "consumed": [dict(r) for r in await pool.fetch(
                                     "SELECT * FROM morning_brief_consumed ORDER BY delivery_id")]}, default=str))


@pytest.mark.parametrize("enabled", [False, True])
def test_empty_triage_preserves_the_recorded_brief_and_rows(monkeypatch, enabled):
    # Captured on b4ebf13f938ddc357a9ccc7c90963f40eca53316, before this feature.
    async def check(pool):
        await triage_settings.update(enabled)
        assert await baseline_observation(pool) == json.loads(BASELINE.read_text())["observation"]
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_triage") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("enabled,has_event", [(False, True), (True, False)])
def test_service_matches_recorded_text_claim_settle_and_consumed_rows(monkeypatch, enabled, has_event):
    async def check(pool):
        if has_event:
            await events(pool, 1)
        await configure(pool, monkeypatch, enabled)
        await baseline_digest(pool)
        posts = []
        class Channel:
            async def post(self, channel_id, target, text, **kwargs):
                await pool.execute("UPDATE morning_briefs SET created_at = $1", NOW)
                posts.append((text, dict(await pool.fetchrow("SELECT * FROM morning_briefs"))))
                return PostReceipt(True)
        await morning_brief.MorningBriefService(Channel(), clock=lambda: NOW).run_once()
        [(text, claimed)] = posts
        settled = dict(await pool.fetchrow("SELECT * FROM morning_briefs"))
        assert await observation(pool, text, claimed, settled) == json.loads(BASELINE.read_text())["observation"]
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_triage") == 0
    run_with_channel_configs(check, monkeypatch)


async def configure(pool, monkeypatch, enabled=True):
    await triage_settings.update(enabled)
    await pool.execute("UPDATE channel_configs SET enabled = true, config = $1::jsonb "
                       "WHERE channel_id = $2", json.dumps({"operator_sender_id": DM}), CHANNEL)
    await pool.execute("INSERT INTO system_settings (key, value) VALUES "
                       "('user.timezone', 'America/Los_Angeles')")
    await chat_settings.load()
    chat_settings.set_morning_brief_enabled(True)
    async def collect(**kwargs):
        return {"total": 0, "sections": []}
    monkeypatch.setattr(attention, "collect", collect)


async def events(pool, count=2):
    await source(pool)
    await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic')")
    await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                       "VALUES ('s', 'p', 'Synthetic', '0 * * * *', 'UTC', 'subscription', 'r')")
    await pool.execute("INSERT INTO schedule_runs (id, schedule_id, status, trigger, scheduled_for) "
                       "VALUES ('r', 's', 'completed', 'manual', $1)", NOW)
    seqs = [await add(pool, f"Synthetic {n}", sent_at=NOW) for n in range(count)]
    await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, window_seqs) "
                       "VALUES ('r', 0, 0, $1)", seqs)
    ids = []
    for n, seq in enumerate(seqs):
        eid = await pool.fetchval(
            "INSERT INTO triage_events (source_id, schedule_id, run_id, category, title, summary, reason, "
            "reason_kind, confidence, flagged, first_sent_at, last_sent_at, created_at) "
            "VALUES ('stream', 's', 'r', 'digest', $1, 'A synthetic summary', 'r', 'model', 'high', true, "
            "$2, $2, $2) RETURNING id", f"Synthetic event {n}", NOW)
        await pool.execute("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)", eid, seq)
        ids.append(eid)
    return ids


@pytest.mark.parametrize("status", ["failed", "sent", "uncertain", "recovery"])
def test_claim_outcomes_record_exactly_the_read_ids(monkeypatch, status):
    async def check(pool):
        [first] = await events(pool, 1)
        async with pool.acquire() as conn:
            assert await store.claim(conn, DAY, CHANNEL, DM, NOW, attempts_limit=3, triage_ids=[first])
            # A producer commits another event after the brief read and claimed its set.
            seq = await add(pool, "Committed after the read", sent_at=NOW)
            await pool.execute("UPDATE subscription_runs SET window_seqs = window_seqs || $1::bigint WHERE run_id='r'", seq)
            later = await pool.fetchval(
                "INSERT INTO triage_events (source_id, run_id, category, title, summary, reason, reason_kind, "
                "confidence, first_sent_at, last_sent_at, created_at) SELECT source_id, run_id, category, "
                "'Later event', summary, reason, reason_kind, confidence, first_sent_at, last_sent_at, created_at "
                "FROM triage_events WHERE id = $1 RETURNING id", first)
            await pool.execute("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)", later, seq)
            if status == "recovery":
                assert await store.orphans_uncertain(conn, NOW) == 1
            else:
                assert await store.settle(conn, DAY, CHANNEL, DM, status, now=NOW,
                                          triage_ids=[first])
            consumed = await conn.fetch("SELECT event_id, local_date, channel_id, chat FROM morning_brief_triage")
            assert [dict(r) for r in consumed] == ([] if status == "failed" else [
                {"event_id": first, "local_date": DAY, "channel_id": CHANNEL, "chat": DM}])
            remaining, _ = await triage_events.for_brief(conn, dates_start=NOW - timedelta(days=2))
            assert {e.id for e in remaining} == ({first, later} if status == "failed" else {later})
            if status == "failed":
                assert await conn.fetchval("SELECT triage_ids FROM morning_briefs") == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize("outcome", [PostOutcome.SENT, PostOutcome.REJECTED, PostOutcome.UNKNOWN])
def test_service_settlement_retries_refusals_and_never_repeats_consumed_events(monkeypatch, outcome):
    async def check(pool):
        ids = await events(pool)
        await configure(pool, monkeypatch)
        manager_id = await pool.fetchval(
            "INSERT INTO manager_events (manager_id, kind, origin, target_kind, channel_id, chat, dedupe_key, "
            "payload, fallback, state, reason) VALUES ('manager', 'triage', 'chat', 'dm', $1, $2, 'fallback', "
            "'{}', '[]', 'fallback', 'delivery_deferred') RETURNING id", CHANNEL, DM)
        await pool.execute("UPDATE triage_events SET category='important', manager_event_id=$2 WHERE id=$1",
                           ids[0], manager_id)
        await pool.execute(
            "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
            "decision, reason, counted, local_date, digest_date, decided_at) VALUES "
            "('triage', 'triage', 'normal', $1, $2, 'direct', 'digest', 'quiet_hours', false, $3, $3, $4)",
            CHANNEL, DM, DAY, NOW)
        posts = []
        class Channel:
            async def post(self, channel_id, target, text, **kwargs):
                posts.append(text)
                return PostReceipt(outcome is PostOutcome.SENT, outcome=outcome)
        await morning_brief.MorningBriefService(Channel(), clock=lambda: NOW).run_once()
        [text] = posts
        assert text.count("E-") == 2 and "（重要，未能及时告诉你）" in text
        assert "推迟到这份晨报的消息" not in text
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 0
        consumed = await pool.fetchval("SELECT count(*) FROM morning_brief_triage")
        assert consumed == (0 if outcome is PostOutcome.REJECTED else 2)
        # Restart on the following day: rejected events remain in the original window; consumed ones do not.
        await morning_brief.MorningBriefService(Channel(), clock=lambda: NOW + timedelta(days=1)).run_once()
        assert len(posts) == (2 if outcome is PostOutcome.REJECTED else 1)
        if outcome is PostOutcome.REJECTED:
            assert posts[1].count("E-") == 2
    run_with_channel_configs(check, monkeypatch)


def test_releasing_a_retry_restores_the_refusal_and_clears_triage_ids(monkeypatch):
    async def check(pool):
        [eid] = await events(pool, 1)
        async with pool.acquire() as conn:
            assert await store.claim(conn, DAY, CHANNEL, DM, NOW, attempts_limit=3, triage_ids=[eid])
            await store.release(conn, DAY, CHANNEL, DM)
            assert await store.get(conn, DAY, CHANNEL, DM) is None
            assert await store.claim(conn, DAY, CHANNEL, DM, NOW, attempts_limit=3, triage_ids=[eid])
            await store.settle(conn, DAY, CHANNEL, DM, "failed", now=NOW)
            assert await store.claim(conn, DAY, CHANNEL, DM, NOW, attempts_limit=3, triage_ids=[eid])
            await store.release(conn, DAY, CHANNEL, DM)
            row = await conn.fetchrow("SELECT status, attempts, triage_ids FROM morning_briefs")
            assert dict(row) == {"status": "failed", "attempts": 1, "triage_ids": []}
            assert await conn.fetchval("SELECT count(*) FROM morning_brief_triage") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("enabled,dm", [(True, True), (False, True), (True, False)])
def test_only_the_enabled_operator_dm_gets_and_consumes_events(monkeypatch, enabled, dm):
    async def check(pool):
        ids = await events(pool, 12)
        await triage_settings.update(enabled)
        await pool.execute("UPDATE channel_configs SET enabled = true, config = $1::jsonb "
                           "WHERE channel_id IN ('channel-lark', 'channel-telegram')",
                           json.dumps({"operator_sender_id": DM}))
        await pool.execute("INSERT INTO system_settings (key, value) VALUES "
                           "('user.timezone', 'America/Los_Angeles')")
        await chat_settings.load()
        chat_settings.set_morning_brief_enabled(True)
        async def collect(**kwargs):
            return {"total": 0, "sections": []}
        monkeypatch.setattr(attention, "collect", collect)
        if not dm:
            async def no_target():
                return None
            monkeypatch.setattr(morning_brief, "operator_dm_target", no_target)
        posts = []
        class Channel:
            async def post(self, channel_id, target, text, **kwargs):
                posts.append((channel_id, text))
                return PostReceipt(True)
        await morning_brief.MorningBriefService(Channel(), clock=lambda: NOW).run_once()
        if enabled and dm:
            assert len(posts) == 1 and posts[0][0] == CHANNEL
            assert posts[0][1].count("E-") == 10 and "另有 2 条，见消息流页" in posts[0][1]
            assert "（含要求助手做事的话）" in posts[0][1]
            assert await pool.fetchval("SELECT count(*) FROM morning_brief_triage") == 10
            await morning_brief.MorningBriefService(Channel(), clock=lambda: NOW).run_once()
            assert len(posts) == 1
        else:
            assert posts == [] and await pool.fetchval("SELECT count(*) FROM morning_brief_triage") == 0
        assert await pool.fetchval("SELECT count(*) FROM triage_events") == len(ids)
    run_with_channel_configs(check, monkeypatch)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("settlement", ["sent", "recovery"])
def test_deferred_triage_exclusion_requires_the_switch_for_read_and_consumption(monkeypatch, enabled, settlement):
    async def check(pool):
        await triage_settings.update(enabled)
        await pool.execute(
            "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
            "decision, reason, counted, local_date, digest_date, decided_at) VALUES "
            "('deferred-triage', 'triage', 'normal', $1, $2, 'direct', 'digest', 'quiet_hours', false, $3, $3, $4)",
            CHANNEL, DM, DAY, NOW)
        row_id = await pool.fetchval("SELECT id FROM proactive_deliveries")
        deferred = await store.digests(pool, channel_id=CHANNEL, chat=DM, until=NOW, dates=[DAY])
        assert [row["id"] for row in deferred] == ([] if enabled else [row_id])
        async with pool.acquire() as conn:
            assert await store.claim(conn, DAY, CHANNEL, DM, NOW, attempts_limit=3, read_ids=[row_id])
            if settlement == "recovery":
                assert await store.orphans_uncertain(conn, NOW) == 1
            else:
                await store.settle(conn, DAY, CHANNEL, DM, "sent", now=NOW, read_ids=[row_id])
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == (0 if enabled else 1)
        assert await store.digests(pool, channel_id=CHANNEL, chat=DM, until=NOW, dates=[DAY]) == []
    run_db(check, monkeypatch)


@pytest.mark.parametrize("object_sql", ["DROP TABLE morning_brief_triage",
                                       "ALTER TABLE morning_briefs DROP COLUMN triage_ids"])
def test_upgrade_repairs_each_object_even_with_a_marker(monkeypatch, object_sql):
    from looper.db import connection
    from looper.db.migrations import morning_brief_triage as migration
    async def check(pool):
        assert await pool.fetch("SELECT name FROM schema_migrations")
        assert not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", migration.name)
        await pool.execute(object_sql)
        await pool.execute("INSERT INTO schema_migrations (name) VALUES ($1)", migration.name)
        async with pool.acquire() as conn:
            assert await migration.pending(conn)
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert not await migration.pending(pool)
    run_db(check, monkeypatch)
