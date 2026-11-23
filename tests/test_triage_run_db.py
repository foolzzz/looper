"""Synthetic triage windows on disposable PostgreSQL, with a scripted processor and no delivery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import extraction, processor, triage_run, triage_settings
from looper.db import schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.subscription_support import (
    DM, SOURCE, add, confirm, cursor, fire, minutes_ago, scenario, source,
)

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


async def triager(env, *, confirmed=True, **subscription):
    """Create and enable the public preset; the operator confirms the current configuration."""
    await source(env.pool)
    await triage_settings.update(True)
    fields = (await env.ok("POST", "/api/v1/schedule_admin/subscription_preset",
                           {"output_kind": "triage"}))["fields"]
    assert "include_chats" not in fields["subscription"]
    fields["subscription"].update(subscription)
    fields.update(cron="*/5 * * * *", timezone="America/Los_Angeles")
    schedule = await env.enabled(**fields)
    sid = schedule["schedule_id"]
    if confirmed:
        await confirm(env, sid)
    return sid


def event(*sources, category="digest", title="Synthetic event", **fields):
    return {"category": category, "title": title, "summary": "A synthetic message",
            "reason": "It can wait for the brief", "confidence": "high", "sources": list(sources), **fields}


def test_none_counts_the_exact_window_as_ignored_without_rows_and_advances(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        seqs = [await add(env.pool, "My synthetic update", sender=DM, sent_at=minutes_ago(env, n))
                for n in (9, 8)]
        await add(env.pool, "Unaddressed group chatter", sent_at=minutes_ago(env, 7))
        run = await fire(env, sid)
        assert (run["status"], run["stream"]["outcome"]) == ("completed", "quiet")
        counts = json.loads(run["stream"]["extract_counts"])
        assert counts == {"important": 0, "digest": 0, "ignore": 2, "uncited": 0, "raised": 0, "none": True, "dropped": {}}
        assert run["stream"]["window_seqs"] == seqs
        assert await cursor(env, sid) == await db_subs.committed_seq(env.pool, SOURCE)
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM manager_events") == 0
        [call] = fake.calls
        assert call["contract"] == triage_run.CONTRACT
        assert call["input_ref"]["kind"] == "triage_window"
        assert "Unaddressed group chatter" not in call["input_text"]
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply="NONE")


def test_two_unreadable_replies_skip_the_window_with_the_extraction_reason(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        top = await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))
        fake.outcomes = [("unreadable", None), ('{"events": "not a list"}', None)]
        run = await fire(env, sid)
        assert (run["status"], run["stream"]["outcome"], run["stream"]["skip_reason"]) == (
            "completed", "skipped", extraction.SkipReason.INVALID_REPLY.value)
        assert len(fake.calls) == 2
        assert extraction.REFORMAT_REMINDER in fake.calls[1]["instructions"]
        # RFC section 5.3 inherits extraction's skip: move past the unreadable window.
        assert await cursor(env, sid) == top
        assert run["stream"]["window_seqs"] == [top] and run["stream"]["extract_counts"] is None
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path)


def test_a_hook_blocked_triage_window_skips_without_retry(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        top = await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))
        fake.outcomes = [(None, processor.ProcessorFailure.BLOCKED)]
        run = await fire(env, sid)
        assert (run["stream"]["outcome"], run["stream"]["skip_reason"]) == (
            "skipped", extraction.SkipReason.BLOCKED_BY_HOOK.value)
        assert len(fake.calls) == 1 and await cursor(env, sid) == top
        assert run["stream"]["window_seqs"] == [top] and run["stream"]["extract_counts"] is None
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0

    scenario(check, monkeypatch, tmp_path)


def test_events_link_their_sources_mark_instructions_and_leave_uncited_messages_rowless(monkeypatch, tmp_path):
    reply = json.dumps({"events": [event("m3", category="important"), event("m1", "c1")],
                        "ignored": [{"sources": ["m2"], "reason": "Routine update"}],
                        "instructions_seen": ["m3"]})

    async def check(env, poster, fake):
        # A pre-cursor message is context, never counted as another window outcome.
        await source(env.pool)
        context = await add(env.pool, "Earlier synthetic context", sender=DM, sent_at=minutes_ago(env, 30))
        sid = await triager(env)
        seqs = [await add(env.pool, f"Synthetic update {n}", sender=DM, sent_at=minutes_ago(env, 10 - n))
                for n in range(4)]
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "extracted"
        counts = json.loads(run["stream"]["extract_counts"])
        assert counts == {"important": 1, "digest": 1, "ignore": 2, "uncited": 1, "raised": 0, "none": False, "dropped": {}}
        assert run["stream"]["window_seqs"] == seqs
        rows = await env.pool.fetch("SELECT * FROM triage_events ORDER BY id")
        assert len(rows) == 3
        important = next(row for row in rows if row["category"] == "important")
        digest = next(row for row in rows if row["category"] == "digest")
        ignored = next(row for row in rows if row["category"] == "ignore")
        assert important["flagged"] and not digest["flagged"]
        assert ignored["title"] == ignored["summary"] == ""
        assert all(row["run_id"] == run["id"] and row["schedule_id"] == sid for row in rows)
        # Only the important event is handed off; this scenario names no chat to tell, so it waits for the brief.
        [handoff] = await env.pool.fetch("SELECT id, kind, state, reason FROM manager_events")
        assert (handoff["kind"], handoff["state"], handoff["reason"]) == ("triage", "fallback", "no_chat")
        assert {row["id"]: row["manager_event_id"] for row in rows} == {
            important["id"]: handoff["id"], digest["id"]: None, ignored["id"]: None}
        links = await env.pool.fetch("SELECT event_id, event_seq FROM triage_event_sources")
        assert {(row["event_id"], row["event_seq"]) for row in links} == {
            (important["id"], seqs[2]), (digest["id"], seqs[0]), (digest["id"], context),
            (ignored["id"], seqs[1])}
        times = await env.pool.fetchrow("SELECT min(sent_at) AS first, max(sent_at) AS last "
                                       "FROM source_events WHERE seq = ANY($1::bigint[])", [seqs[0], context])
        assert (digest["first_sent_at"], digest["last_sent_at"]) == (times["first"], times["last"])
        # The migration's deletion trigger must see the actual provenance written by store.
        await env.pool.execute("DELETE FROM source_events WHERE seq = $1", context)
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events WHERE id = $1", digest["id"]) == 0
        assert await env.pool.fetchval("SELECT count(*) FROM triage_event_sources WHERE event_id = $1", digest["id"]) == 0
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 2
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply=reply)


def test_an_unanswered_ignored_dm_is_raised_but_an_answered_or_important_one_is_not(monkeypatch, tmp_path):
    reply = json.dumps({"events": [event("m4", category="important")],
                        "ignored": [{"sources": ["m1", "m2", "m3"], "reason": "Routine messages"}],
                        "instructions_seen": []})

    async def check(env, poster, fake):
        sid = await triager(env)
        unanswered = await add(env.pool, "Unanswered synthetic DM", chat_id="oc_unanswered", kind="p2p",
                               sent_at=minutes_ago(env, 9))
        answered = await add(env.pool, "Answered synthetic DM", chat_id="oc_answered", kind="p2p",
                             sent_at=minutes_ago(env, 8))
        important = await add(env.pool, "Urgent synthetic DM", chat_id="oc_important", kind="p2p",
                              sent_at=minutes_ago(env, 7))
        # A live operator reply counts by sent_at and remains eligible for the selected window.
        answer = await add(env.pool, "The operator already answered", chat_id="oc_answered", kind="p2p", sender=DM,
                  origin="live", sent_at=minutes_ago(env, 6))
        run = await fire(env, sid)
        from dataclasses import asdict
        from looper.db import triage_events
        by_seq = {seq: asdict((await triage_events.for_message(seq, db=env.pool))[0])
                  for seq in (unanswered, answered, important, answer)}
        raised = by_seq[unanswered]
        assert (raised["category"], raised["reason_kind"], raised["title"], raised["summary"], raised["reason"],
                raised["confidence"]) == ("digest", "raised", "未回复的私聊", "",
                                          "操作者还没在这个会话里回复（代码保底）", "high")
        assert (by_seq[answered]["category"], by_seq[answered]["reason_kind"]) == ("ignore", "model")
        assert (by_seq[important]["category"], by_seq[important]["reason_kind"]) == ("important", "model")
        counts = json.loads(run["stream"]["extract_counts"])
        assert (counts["important"], counts["digest"], counts["ignore"], counts["raised"]) == (1, 1, 2, 1)
        assert run["stream"]["window_seqs"] == [unanswered, answered, important, answer]

    scenario(check, monkeypatch, tmp_path, reply=reply)


def test_raise_floor_queries_replies_outside_the_supplied_window_rows(monkeypatch):
    async def check(pool):
        await source(pool)
        now = datetime.now(timezone.utc)
        first = await add(pool, "Synthetic request", chat_id="oc_dm", kind="p2p", sent_at=now)
        answer = await add(pool, "Synthetic answer", chat_id="oc_dm", kind="p2p", sender=DM,
                           sent_at=now + timedelta(seconds=1))
        rows = [dict(await pool.fetchrow("SELECT e.*, 'p2p' AS chat_kind FROM source_events e WHERE seq = $1", first))]
        src = dict(await pool.fetchrow("SELECT * FROM event_sources WHERE id = $1", SOURCE))
        parsed = triage_run.parse(json.dumps({"events": [], "ignored": [
            {"sources": ["m1"], "reason": "Routine"}], "instructions_seen": []}), {"m1": first})
        held = await triage_run.raise_floor(pool, parsed, rows, source=src, window_end_seq=answer)
        assert len(held.items) == 1 and held.items[0]["category"] == "ignore"
        before_answer = await triage_run.raise_floor(pool, parsed, rows, source=src, window_end_seq=first)
        assert [item["reason_kind"] for item in before_answer.items if item["window_sources"]] == ["raised"]

    run_db(check, monkeypatch)


def test_a_changed_confirmation_skips_before_reading_and_keeps_the_cursor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        before = await cursor(env, sid)
        await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))
        await env.ok("POST", "/api/v1/schedule_admin/update",
                     {"schedule_id": sid, "fields": {"subscription": {"exclude_chats": ["oc_other"]}}})
        run = await fire(env, sid)
        assert (run["status"], run["error"]) == ("skipped_gate", extraction.NOT_CONFIRMED)
        assert fake.calls == [] and await cursor(env, sid) == before
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0

    scenario(check, monkeypatch, tmp_path, reply="NONE")


def test_a_confirmation_changed_during_processing_rolls_back_the_window_and_events(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        before = await cursor(env, sid)
        await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))

        async def change():
            await env.ok("POST", "/api/v1/schedule_admin/update",
                         {"schedule_id": sid, "fields": {"cron": "*/10 * * * *"}})

        fake.during = change
        run = await fire(env, sid)
        assert run["status"] == "cancelled" and "nothing was stored" in run["error"]
        assert await cursor(env, sid) == before
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert run["stream"]["window_seqs"] is None and run["stream"]["extract_counts"] is None

    scenario(check, monkeypatch, tmp_path, reply=json.dumps({"events": [event("m1")], "ignored": []}))


@pytest.mark.parametrize("change,reason", [("delete", "source_deleted"), ("exclude", "chat_excluded")])
def test_a_source_deleted_or_excluded_during_processing_leaves_no_derived_event(monkeypatch, tmp_path, change, reason):
    async def check(env, poster, fake):
        sid = await triager(env)
        seq = await add(env.pool, "My synthetic update", sender=DM, sent_at=minutes_ago(env, 5))

        async def revoke():
            if change == "delete":
                await env.pool.execute("DELETE FROM source_events WHERE seq = $1", seq)
            else:
                await env.pool.execute("UPDATE source_chats SET extraction = 'excluded', extraction_excluded_at = now() "
                                       "WHERE source_id = $1 AND chat_id = 'oc_ops'", SOURCE)

        fake.during = revoke
        run = await fire(env, sid)
        assert run["status"] == "completed" and await cursor(env, sid) == seq
        assert json.loads(run["stream"]["extract_counts"])["dropped"] == {reason: 1}
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM triage_event_sources") == 0
        assert run["stream"]["window_seqs"] == [seq]
        assert len(fake.calls) == 1 and poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply=json.dumps({"events": [event("m1")], "ignored": []}))


def test_a_context_source_deleted_during_processing_drops_the_entire_event(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        context = await add(env.pool, "Synthetic context to delete", sender=DM, sent_at=minutes_ago(env, 30))
        sid = await triager(env)
        message = await add(env.pool, "My live update", sender=DM, sent_at=minutes_ago(env, 5))

        async def delete_context():
            await env.pool.execute("DELETE FROM source_events WHERE seq = $1", context)

        fake.during = delete_context
        run = await fire(env, sid)
        counts = json.loads(run["stream"]["extract_counts"])
        assert run["status"] == "completed" and await cursor(env, sid) == message
        assert counts["dropped"] == {"source_deleted": 1}
        assert (counts["digest"], counts["ignore"], counts["uncited"]) == (0, 1, 1)
        assert "Synthetic context to delete" in fake.calls[0]["input_text"]
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM triage_event_sources") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM source_events WHERE seq = $1", message) == 1
        assert run["stream"]["window_seqs"] == [message] and poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply=json.dumps({"events": [event("m1", "c1")], "ignored": []}))


def test_imported_preceding_and_parent_context_never_reach_the_triage_processor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        await add(env.pool, "Imported preceding context", origin="import", sender=DM, sent_at=minutes_ago(env, 30))
        parent = await add(env.pool, "Imported explicit parent", origin="import", sender=DM,
                           sent_at=minutes_ago(env, 25 * 60))
        sid = await triager(env)
        message = await add(env.pool, "My selected live update", sender=DM, sent_at=minutes_ago(env, 5))
        # Outside the preceding-context horizon, so only the explicit parent lookup can fetch this import.
        await env.pool.execute("UPDATE source_events SET parent_id = (SELECT external_id FROM source_events "
                               "WHERE seq = $1) WHERE seq = $2", parent, message)
        run = await fire(env, sid)
        [call] = fake.calls
        assert "My selected live update" in call["input_text"]
        assert "Imported preceding context" not in call["input_text"]
        assert "Imported explicit parent" not in call["input_text"]
        assert call["input_ref"]["context"] == 0
        assert run["status"] == "completed" and run["stream"]["window_seqs"] == [message]
        assert await cursor(env, sid) == message and poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply="NONE")


@pytest.mark.parametrize("extra,problem", [({"include_chats": ["oc_synthetic"]}, "include_chats"),
                                          ({"check_items": True}, "second model check")])
def test_triage_refuses_a_chat_allowlist_and_a_second_model_check(monkeypatch, tmp_path, extra, problem):
    async def check(env, poster, fake):
        sid = await triager(env)
        fields = (await env.ok("POST", "/api/v1/schedule_admin/subscription_preset",
                               {"output_kind": "triage"}))["fields"]
        fields["subscription"].update(extra)
        status, body = await env.call("POST", "/api/v1/schedule_admin/create",
                                      {"project_id": env.project_id, "fields": fields})
        assert status == 400 and problem in json.dumps(body)
        status, body = await env.call("POST", "/api/v1/schedule_admin/update",
                                      {"schedule_id": sid, "fields": {"subscription": extra}})
        assert status == 400 and problem in json.dumps(body)
        assert fake.calls == [] and poster.sent == []

    scenario(check, monkeypatch, tmp_path)


def test_switch_off_refuses_creation_but_allows_disabling_and_renaming(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        fields = (await env.ok("POST", "/api/v1/schedule_admin/subscription_preset",
                               {"output_kind": "triage"}))["fields"]
        status, body = await env.call("POST", "/api/v1/schedule_admin/create",
                                      {"project_id": env.project_id, "fields": fields})
        assert status == 400 and triage_settings.TURNED_OFF in json.dumps(body)
        sid = await triager(env)
        await triage_settings.update(False)
        before = await cursor(env, sid)
        run = await fire(env, sid)
        assert (run["status"], run["error"]) == ("skipped_gate", triage_settings.TURNED_OFF)
        await env.ok("POST", "/api/v1/schedule_admin/update",
                     {"schedule_id": sid, "fields": {"enabled": False}})
        await env.ok("POST", "/api/v1/schedule_admin/update",
                     {"schedule_id": sid, "fields": {"name": "Renamed while off"}})
        await env.ok("POST", "/api/v1/schedule_admin/update",
                     {"schedule_id": sid, "fields": {"subscription": {"max_events": 20}}})
        stored = await db_schedules.get_schedule(sid)
        assert stored["name"] == "Renamed while off" and not stored["enabled"]
        assert await cursor(env, sid) == before and fake.calls == []

    scenario(check, monkeypatch, tmp_path)


def test_switch_off_keeps_firing_byte_identical_and_never_reads_the_processor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        await add(env.pool, "Synthetic DM", kind="p2p", sent_at=minutes_ago(env, 5))
        before = await cursor(env, sid)
        await triage_settings.update(False)
        run = await fire(env, sid)
        assert (run["status"], run["error"], run["stream"]) == ("skipped_gate", triage_settings.TURNED_OFF, None)
        assert await cursor(env, sid) == before and fake.calls == []
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM manager_events") == 0
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path)


def test_a_triage_attempt_without_a_firing_time_is_cancelled_and_the_next_run_reads_its_window(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        before = await cursor(env, sid)
        top = await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))
        runner = env.scheduler._subscriptions
        now = env.clock.now
        async with env.pool.acquire() as conn, conn.transaction():
            schedule = await db_schedules.get_schedule(sid, pool=conn)
            launched = await env.scheduler._start_subscription(
                conn, schedule, now, scheduled_for=now, run_id=None,
                common={"schedule_id": sid, "scheduled_for": now, "trigger": "manual", "created_at": now}, timeout={})
            await conn.execute("UPDATE schedule_runs SET fired_at = NULL WHERE id = $1", launched.run_id)
        assert await runner.execute(launched.run_id) is False
        ended = await db_schedules.get_run(launched.run_id, pool=env.pool)
        assert ended["status"] == "cancelled" and "firing time" in ended["error"]
        assert await cursor(env, sid) == before and fake.calls == []
        assert await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0
        resumed = await fire(env, sid)
        assert resumed["status"] == "completed" and await cursor(env, sid) == top
        assert resumed["stream"]["window_seqs"] == [top]
        assert len(fake.calls) == 1 and poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply="NONE")


def test_an_orphaned_triage_run_is_interrupted_and_its_next_confirmed_run_reads_the_window(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        before = await cursor(env, sid)
        top = await add(env.pool, "My update", sender=DM, sent_at=minutes_ago(env, 5))
        runner = env.scheduler._subscriptions
        now = env.clock.now
        async with env.pool.acquire() as conn, conn.transaction():
            schedule = await db_schedules.get_schedule(sid, pool=conn)
            launched = await env.scheduler._start_subscription(
                conn, schedule, now, scheduled_for=now, run_id=None,
                common={"schedule_id": sid, "scheduled_for": now, "trigger": "manual", "created_at": now}, timeout={})
        run = await db_schedules.get_run(launched.run_id, pool=env.pool)
        # A previous server's token and an expired runtime are enough to establish an orphan without fake PIDs.
        run = {**run, "runner_started": "previous-synthetic-server", "runner_pid": None,
               "fired_at": now - timedelta(seconds=processor.PROCESSOR_TIMEOUT_SEC + 301)}
        assert await runner.settle(run, now)
        ended = await db_schedules.get_run(launched.run_id, pool=env.pool)
        assert ended["status"] == "interrupted" and await cursor(env, sid) == before
        assert fake.calls == []
        resumed = await fire(env, sid)
        assert resumed["status"] == "completed" and await cursor(env, sid) == top
        assert resumed["stream"]["window_seqs"] == [top]
        assert len(fake.calls) == 1 and await env.pool.fetchval("SELECT count(*) FROM triage_events") == 0

    scenario(check, monkeypatch, tmp_path, reply="NONE")


def test_shared_citations_keep_every_link_and_flag_but_count_only_the_highest_category(monkeypatch, tmp_path):
    from looper.db import triage_events

    reply = json.dumps({"events": [event("m1", "m2", "c1", title="Digest"),
                                   event("m1", "c1", category="important", title="Important"),
                                   event("m1", "c1", title="Shared only")],
                        "ignored": [], "instructions_seen": ["m1", "c1"]})

    async def check(env, poster, fake):
        await source(env.pool)
        context = await add(env.pool, "Earlier context", sender=DM, sent_at=minutes_ago(env, 30))
        sid = await triager(env)
        seqs = [await add(env.pool, "My synthetic update", sender=DM, sent_at=minutes_ago(env, n)) for n in (9, 8)]
        run = await fire(env, sid)
        counts = json.loads(run["stream"]["extract_counts"])
        assert (counts["important"], counts["digest"], counts["ignore"], counts["dropped"]) == (1, 1, 0, {})
        events = await triage_events.for_run(run["id"], db=env.pool)
        assert len(events) == 3 and all(item.flagged for item in events)
        links = await triage_events.source_rows([item.id for item in events], db=env.pool)
        assert {item.title: {row["seq"] for row in links[item.id]} for item in events} == {
            "Important": {seqs[0], context}, "Digest": {*seqs, context}, "Shared only": {seqs[0], context}}
        cited = await triage_events.for_message(seqs[0], db=env.pool)
        assert cited[0].category == "important" and len(cited) == 3
        await env.pool.execute("DELETE FROM source_events WHERE seq = $1", seqs[0])
        assert await triage_events.for_run(run["id"], db=env.pool) == []
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path, reply=reply)


@pytest.mark.parametrize("sender_kind,origin,reply_origin,reply_offset,expected", [
    ("app", "live", None, 1, "ignore"),
    ("user", "import", None, 1, "ignore"),
    ("user", "live", "import", 1, "digest"),
    ("user", "live", "live", -1, "digest"),
    ("user", "live", "live", 1, "ignore"),
])
def test_floor_uses_live_user_messages_and_reply_send_time(monkeypatch, sender_kind, origin,
                                                          reply_origin, reply_offset, expected):
    async def check(pool):
        await source(pool)
        now = datetime.now(timezone.utc)
        first = await add(pool, "Synthetic request", chat_id="oc_dm", kind="p2p", sent_at=now,
                          sender_kind=sender_kind, origin=origin)
        end = first
        if reply_origin:
            end = await add(pool, "Synthetic answer", chat_id="oc_dm", kind="p2p", sender=DM,
                            sent_at=now + timedelta(seconds=reply_offset), origin=reply_origin)
        rows = [dict(await pool.fetchrow("SELECT e.*, 'p2p' AS chat_kind FROM source_events e WHERE seq = $1", first))]
        src = dict(await pool.fetchrow("SELECT * FROM event_sources WHERE id = $1", SOURCE))
        parsed = triage_run.parse(json.dumps({"events": [], "ignored": [
            {"sources": ["m1"], "reason": "Routine"}]}), {"m1": first})
        held = await triage_run.raise_floor(pool, parsed, rows, source=src, window_end_seq=end)
        assert [item["category"] for item in held.items if item["window_sources"]] == [expected]

    run_db(check, monkeypatch)


def test_none_marker_survives_the_code_floor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await triager(env)
        await add(env.pool, "Unanswered synthetic DM", kind="p2p", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        counts = json.loads(run["stream"]["extract_counts"])
        assert counts["none"] is True and counts["raised"] == counts["digest"] == 1
        assert counts["uncited"] == counts["ignore"] == 0

    scenario(check, monkeypatch, tmp_path, reply="```text\nNONE\n```")
