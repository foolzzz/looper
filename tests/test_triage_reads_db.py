"""Triage vocabulary and SELECT-only event reads over synthetic message sources."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.leads_model import Confidence
from looper.agents.triage_model import Category, ExtractCounts, ReasonKind, format_event_id, parse_event_id
from looper.db import triage_events
from tests.db_support import run_db
from tests.subscription_support import SOURCE, add, person, source


def test_event_ids_and_uncited_counts_are_typed():
    assert format_event_id(123) == "E-123"
    assert parse_event_id("e-123") == parse_event_id("123") == 123
    with pytest.raises(ValueError):
        parse_event_id("lead-123")
    counts = ExtractCounts(ignore=2, uncited=1, raised=1, dropped={"missing_source": 3})
    assert counts.as_dict() == {"important": 0, "digest": 0, "ignore": 2, "uncited": 1, "raised": 1,
                                "none": False, "dropped": {"missing_source": 3}}
    with pytest.raises(FrozenInstanceError):
        counts.ignore = 4


def test_event_reads_are_typed_stable_and_read_only(monkeypatch):
    async def check(pool):
        await source(pool)
        await person(pool, "ou_alice", "Alice")
        now = datetime.now(timezone.utc)
        seqs = [await add(pool, f"Message {n}", sent_at=now + timedelta(minutes=n)) for n in range(2)]
        await pool.execute("INSERT INTO projects (id, name) VALUES ('triage-project', 'Triage project')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind) "
                           "VALUES ('triage-schedule', 'triage-project', 'Triage', '0 * * * *', 'UTC', 'Triage', "
                           "'subscription')")
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                           "VALUES ('triage-run', 'triage-schedule', now(), 'manual', 'completed')")
        await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq) VALUES ('triage-run', 0, $1)",
                           seqs[-1])
        ids = []
        for category, reason_kind in (("ignore", "uncited"), ("digest", "raised"), ("important", "model")):
            event_id = await pool.fetchval(
                "INSERT INTO triage_events (source_id, schedule_id, run_id, category, title, summary, reason, "
                "reason_kind, confidence, first_sent_at, last_sent_at) "
                "VALUES ($1, 'triage-schedule', 'triage-run', $2, $3, $3, 'Synthetic reason', $4, 'high', $5, $5) "
                "RETURNING id", SOURCE, category, "" if category == "ignore" else "Synthetic event", reason_kind, now)
            ids.append(event_id)
            for seq in reversed(seqs):
                await pool.execute("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)",
                                   event_id, seq)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            row = await triage_events.get(ids[0], db=conn)
            assert row.category is Category.IGNORE and row.reason_kind is ReasonKind.UNCITED
            assert row.confidence is Confidence.HIGH and row.title == row.summary == ""
            assert row.manager_event_id is None and row.briefed_run_id is None
            assert (await triage_events.get(ids[1], db=conn)).reason_kind is ReasonKind.RAISED
            assert await triage_events.get(ids[-1] + 1, db=conn) is None
            assert [event.id for event in await triage_events.for_message(seqs[0], db=conn)] == list(reversed(ids))
            assert [event.id for event in await triage_events.for_run("triage-run", db=conn)] == ids
            assert await triage_events.for_message(seqs[-1] + 1, db=conn) == []
            assert await triage_events.for_run("missing", db=conn) == []
            sources = await triage_events.source_rows(ids, db=conn)
            assert list(sources) == ids
            assert [row["seq"] for row in sources[ids[0]]] == seqs
            assert sources[ids[0]][0]["sender_name"] == "Alice"
            assert sources[ids[0]][0]["chat_name"] == "Ops room"
            assert await triage_events.source_rows([], db=conn) == {}
            assert await triage_events.source_rows([ids[-1] + 1], db=conn) == {}
        # Privacy follows every source message: losing even one deletes the entire derived event.
        await pool.execute("DELETE FROM source_events WHERE seq = $1", seqs[0])
        assert await triage_events.for_run("triage-run", db=pool) == []
        assert await triage_events.source_rows(ids, db=pool) == {}

    run_db(check, monkeypatch, timeout=20)
