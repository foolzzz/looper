"""Event triage's additive schema: upgrade, read-only admission, constraints and retention."""

import asyncio

import asyncpg
import pytest

from looper.db import connection, subscriptions
from looper.db.manager_events import EventKind, Reason
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, drop_tables, fresh_schema, run_db
from tests.subscription_support import SOURCE, source

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_triage_upgrade_matches_fresh_and_reopen_does_not_replay():
    async def check():
        async with fresh_schema() as pool, fresh_schema() as fresh:
            await connection.ensure_schema(pool)
            restore = await drop_tables(pool, "triage_event_sources", "triage_events")
            await pool.execute("""
                DROP FUNCTION triage_drop_event();
                ALTER TABLE subscription_runs DROP COLUMN window_seqs;
                ALTER TABLE subscriptions DROP CONSTRAINT subscriptions_output_kind_check;
                ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_output_kind_check
                    CHECK (output_kind IN ('push', 'leads', 'intents'));
                ALTER TABLE manager_events DROP CONSTRAINT manager_events_task;
                ALTER TABLE manager_events ALTER COLUMN task_id SET NOT NULL;
                ALTER TABLE manager_events DROP CONSTRAINT manager_events_kind_check;
                ALTER TABLE manager_events ADD CONSTRAINT manager_events_kind_check CHECK (kind IN ('task_end'));
                ALTER TABLE manager_events DROP CONSTRAINT manager_events_reason_check;
                ALTER TABLE manager_events ADD CONSTRAINT manager_events_reason_check
                    CHECK (reason IN ('off', 'group_chat', 'manager_off', 'turn_failed', 'turn_timeout', 'turn_stopped',
                                      'empty_reply', 'send_failed', 'queue_deadline', 'restart', 'no_chat', 'target_changed'));
            """)
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('synthetic-saved-choice', 'keep')")
            async with pool.acquire() as conn:
                before = await witness(conn)
                async with conn.transaction(readonly=True):
                    assert await connection.schema_change_pending(conn)
                assert await witness(conn) == before
            await connection.ensure_schema(pool)
            await restore()
            await connection.ensure_schema(fresh)
            async with pool.acquire() as conn, fresh.acquire() as other:
                assert await structure(conn) == await structure(other)
                assert await conn.fetchval("SELECT value FROM system_settings WHERE key = 'synthetic-saved-choice'") == 'keep'
                assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                           "('event_triage', 'event_triage_online')") == 2
                assert not await other.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                                "WHERE name LIKE 'event_triage%')")
                saved, catalog = await witness(conn), await structure(conn)
            await connection.ensure_schema(pool)
            async with pool.acquire() as conn:
                assert not await connection.schema_change_pending(conn)
                assert await witness(conn) == saved and await structure(conn) == catalog
    asyncio.run(check())


@pytest.mark.parametrize("column,value", [
    ("category", "unknown"), ("reason_kind", "unknown"), ("confidence", "unknown"),
    ("reason", " "), ("reason", "x" * 101), ("title", "x" * 61), ("summary", "x" * 401),
])
def test_triage_rejects_invalid_vocabulary_and_lengths(monkeypatch, column, value):
    async def check(pool):
        await source(pool)
        values = {"category": "digest", "reason_kind": "model", "confidence": "high", "reason": "A useful update",
                  "title": "Update", "summary": "A synthetic update", column: value}
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(f"INSERT INTO triage_events (source_id, {', '.join(values)}, first_sent_at, last_sent_at) "
                               f"VALUES ($1, {', '.join(f'${i}' for i in range(2, len(values) + 2))}, now(), now())",
                               SOURCE, *values.values())
    run_db(check, monkeypatch)


def test_uncited_window_membership_is_exact_and_old_run_shape_stays_unchanged(monkeypatch):
    async def check(pool):
        await source(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic project')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                           "VALUES ('s', 'p', 'Synthetic triage', '0 * * * *', 'UTC', 'subscription', 'Synthetic triage')")
        await pool.execute("INSERT INTO subscriptions (schedule_id, source_id, chat_kinds, sender_kinds, "
                           "max_age_minutes, max_events, text_max_chars, output_kind) "
                           "VALUES ('s', $1, ARRAY['p2p'], ARRAY['user'], 60, 500, 200, 'triage')", SOURCE)
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                           "VALUES ('r', 's', now(), 'manual', 'running')")
        async with pool.acquire() as conn:
            await subscriptions.insert_run(conn, 'r', from_seq=0, to_seq=20, in_window=20, selected=2,
                                           too_old=0, over_cap=0)
        assert "window_seqs" not in await subscriptions.get_run('r', db=pool)
        assert await subscriptions.runs_for_message(4, db=pool) == []
        await pool.execute("UPDATE subscription_runs SET window_seqs = ARRAY[4, 19]::bigint[], "
                           "extract_counts = '{\"ignore\": 2, \"uncited\": 2}' WHERE run_id = 'r'")
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            runs = await subscriptions.runs_for_message(4, db=conn)
            assert [r['run_id'] for r in runs] == ['r']
            assert runs[0]['window_seqs'] == [4, 19]
            assert await subscriptions.runs_for_message(5, db=conn) == []
        assert await pool.fetchval("SELECT count(*) FROM triage_events") == 0
        # Even a recovered/queued attempt cannot reach the legacy intents processor.
        from looper.agents.subscription_runs import SubscriptionRuns
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock
        from looper.agents import processor

        launch = AsyncMock(side_effect=AssertionError("triage must not start a model"))
        monkeypatch.setattr(processor, "run", launch)
        runner = SubscriptionRuns(poster=AsyncMock(), own_apps=AsyncMock(), runtime_root=None,
                                  clock=lambda: datetime.now(timezone.utc))
        assert await runner.execute('r') is False
        launch.assert_not_awaited()
        assert await pool.fetchval("SELECT status FROM schedule_runs WHERE id = 'r'") == 'cancelled'
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = 's'") == 0
    run_db(check, monkeypatch)


def test_manager_event_kind_requires_task_only_for_task_end_and_link_categories_are_checked(monkeypatch):
    async def check(pool):
        await source(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic project')")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('t', 'p', 'Synthetic task', 'completed')")
        manager = await pool.fetchval("SELECT id FROM managers ORDER BY id LIMIT 1")

        async def insert(kind, task_id, key):
            return await pool.fetchval(
                "INSERT INTO manager_events (manager_id, kind, task_id, origin, target_kind, channel_id, chat, "
                "dedupe_key, payload, fallback, state, reason) "
                "VALUES ($1, $2, $3, 'schedule', 'web', 'web', 'synthetic', $4, '{}', '[]', 'skipped', $5) RETURNING id",
                manager, kind, task_id, key, Reason.MANAGER_SILENT if kind == EventKind.TRIAGE else Reason.OFF)

        event = await insert(EventKind.TRIAGE, None, 'triage')
        assert await insert(EventKind.TASK_END, 't', 'task')
        for kind, task in (('task_end', None), ('triage', 't')):
            with pytest.raises(asyncpg.CheckViolationError):
                await insert(kind, task, f'{kind}-invalid')
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, kind, requirement) "
                           "VALUES ('s', 'p', 'Briefing', '0 8 * * *', 'UTC', 'agent', 'Synthetic briefing')")
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                           "VALUES ('b', 's', now(), 'manual', 'completed')")
        sql = ("INSERT INTO triage_events (source_id, category, reason, reason_kind, confidence, first_sent_at, "
               "last_sent_at, manager_event_id, briefed_run_id) "
               "VALUES ($1, $2, 'Synthetic reason', $3, 'high', now(), now(), $4, $5) RETURNING id")
        for category, reason_kind, manager_event, briefed in (
                ('digest', 'model', event, None), ('ignore', 'model', None, 'b'),
                ('important', 'uncited', None, None)):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.fetchval(sql, SOURCE, category, reason_kind, manager_event, briefed)
        assert await pool.fetchval(sql, SOURCE, 'important', 'raised', event, 'b')
        assert await pool.fetchval(sql, SOURCE, 'digest', 'model', None, 'b')
    run_db(check, monkeypatch)


def test_a_recorded_triage_upgrade_does_not_repair_a_missing_table(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO schema_migrations (name) VALUES ('event_triage'), ('event_triage_online')")
        await pool.execute("DROP TABLE triage_event_sources")
        async with pool.acquire() as conn:
            before = await structure(conn)
            assert not await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert await structure(conn) == before
    run_db(check, monkeypatch)
