"""One-off recognition on disposable PostgreSQL, with synthetic messages and a scripted processor."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from looper.agents import extraction, intent_push, intent_recognition, processor, subscriptions
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.serve import seed_role_configs
from looper.db import intents as db_intents, schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db
from tests.leads_support import intent
from tests.subscription_support import DM, SOURCE, add, chat, processor_says, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)
REPLY = json.dumps({"intents": [{"type": "bug_triage", "title": "Synthetic upload bug",
    "summary": "Synthetic problem report", "confidence": "high", "sources": ["m1"]}]})


async def configured(pool):
    await source(pool)
    await chat(pool, "g", name="Synthetic group")
    await pool.execute("INSERT INTO projects(id,name) VALUES('p','Synthetic project')")
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true'),"
                       "('intents.on_request.enabled','true')")
    await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind,run_budget_usd,"
                       "run_timeout_mode,run_timeout_seconds) VALUES('s','p','Synthetic intents','*/10 * * * *',"
                       "'UTC',$1,'subscription',0.3,'seconds',20)", extraction.INTENTS_INSTRUCTIONS)
    await db_subs.create(pool, "s", subscriptions.preset("intents", source_id=SOURCE, zone_name="UTC")["subscription"])
    row = await db_schedules.get_schedule("s")
    await db_subs.update(pool, "s", {"target_confirmed_at": NOW,
                                    "target_confirmed_version": await subscriptions.config_version(row)})
    return await db_schedules.get_schedule("s")


async def many(pool, count, *, same_time=False):
    await pool.execute("INSERT INTO source_events(source_id,external_id,chat_id,sender_id,sender_kind,sent_at,"
        "msg_type,mentions_me,text,body_bytes,origin) SELECT $1,'synthetic-' || n,'g','ou_synthetic','user',"
        "$3::timestamptz + CASE WHEN $4 THEN interval '0 seconds' ELSE n * interval '1 second' END,"
        "'text',false,'Synthetic request ' || n,1,'live' FROM generate_series(1,$2::integer) n", SOURCE, count,
        NOW - timedelta(hours=2), same_time)
    await pool.execute("UPDATE event_sources SET committed_seq=(SELECT max(seq) FROM source_events) WHERE id=$1", SOURCE)


async def finished(service, pool):
    # Join the actual background work, including result delivery, instead of assuming a green start is completion.
    await asyncio.wait_for(asyncio.gather(*tuple(service._tasks)), timeout=15)
    return dict(await pool.fetchrow("SELECT * FROM intent_recognitions ORDER BY id DESC LIMIT 1"))


def test_recognize_once_reads_only_the_range_and_moves_nothing(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, REPLY)
    async def check(pool):
        await configured(pool)
        await pool.execute("INSERT INTO intent_group_reads(source_id,chat_id,added_at,read_from) VALUES($1,'g',$2,$2)",
                           SOURCE, NOW - timedelta(hours=1))
        before = dict(await pool.fetchrow("SELECT * FROM intent_group_reads"))
        cursor = await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'")
        await add(pool, "Outside earlier context", chat_id="g", sent_at=SINCE - timedelta(seconds=1))
        live = await add(pool, "Live request before followed start", chat_id="g", sent_at=SINCE)
        history = await add(pool, "History request", chat_id="g", sent_at=SINCE + timedelta(minutes=1), origin="history")
        await add(pool, "Imported forbidden", chat_id="g", sent_at=SINCE + timedelta(minutes=2), origin="import")
        await add(pool, "Bot forbidden", chat_id="g", sent_at=SINCE + timedelta(minutes=3), sender_kind="app")
        await add(pool, "Other sender forbidden", chat_id="g", sent_at=SINCE + timedelta(minutes=4), sender_kind="other")
        await add(pool, "Outside upper boundary", chat_id="g", sent_at=NOW)
        await add(pool, "Another group forbidden", chat_id="h", sent_at=SINCE + timedelta(minutes=5))
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            preview = await intent_recognition.preview(source_id=SOURCE, chat_id="g", db=pool, now=NOW)
            assert preview["events"] == 2 and preview["truncated"] is None
            assert datetime.fromisoformat(preview["range_from"]) == SINCE
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            record = await finished(service, pool)
            assert record["state"] == "done" and record["events_read"] == 2
            stored = await pool.fetchrow("SELECT * FROM intents")
            assert stored["via"] == "on_request" and stored["recognition_id"] == record["id"]
            assert stored["schedule_id"] == "s" and stored["run_id"] is None
            assert [r["event_seq"] for r in await pool.fetch("SELECT event_seq FROM intent_sources")] == [live]
            assert "Live request before followed start" in fake.calls[0]["input_text"]
            assert "History request" in fake.calls[0]["input_text"]
            assert all(text not in fake.calls[0]["input_text"] for text in (
                "Outside earlier context", "Imported forbidden", "Bot forbidden", "Other sender forbidden",
                "Outside upper boundary", "Another group forbidden"))
            assert extraction.GROUP_REQUESTS_NOTE in fake.calls[0]["instructions"]
            assert dict(await pool.fetchrow("SELECT * FROM intent_group_reads")) == before
            assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'") == cursor
            assert await pool.fetchval("SELECT count(*) FROM intent_group_read_days") == 0
            # Force a usable private push target, then prove the normal sender still sends a control intent.
            async def target(_):
                return "channel-lark", DM
            monkeypatch.setattr(intent_push, "target_for", target)
            posts = []
            async def post(channel, target, text, **kwargs):
                posts.append(text)
                return PostReceipt(PostOutcome.SENT, ("om_synthetic",))
            sender = intent_push.IntentPushService(SimpleNamespace(post=post), clock=lambda: NOW)
            await sender.run_once()
            assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 0 and posts == []
            await intent(pool, [history], schedule_id="s", created_at=NOW)
            await sender.run_once()
            assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 1 and len(posts) == 1
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


@pytest.mark.parametrize("same_time", [False, True])
def test_recognize_once_caps_at_the_latest_two_thousand_messages(monkeypatch, tmp_path, same_time):
    fake = processor_says(monkeypatch, "NONE")
    async def check(pool):
        await configured(pool)
        await many(pool, 2001, same_time=same_time)
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            preview = await intent_recognition.preview(source_id=SOURCE, chat_id="g", db=pool, now=NOW)
            assert preview["events"] == 2001 and preview["truncated"] == "messages"
            assert max(preview["estimate_usd"]) <= 1
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            record = await finished(service, pool)
            assert record["state"] == "done" and record["events_read"] == 2000
            assert record["truncated"] == "messages" and len(fake.calls) == 4
            text = "\n".join(call["input_text"] for call in fake.calls)
            assert "Synthetic request 2001" in text
            assert not any(line.endswith("Synthetic request 1") for line in text.splitlines())
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_recognize_once_stops_at_the_remaining_budget(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, REPLY)
    fake.cost = 0.6
    async def check(pool):
        await configured(pool)
        await many(pool, 1500)
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            record = await finished(service, pool)
            assert record["state"] == "done" and record["truncated"] == "budget"
            assert record["events_read"] == 1000 and len(fake.calls) == 2
            assert [round(call["budget_usd"], 4) for call in fake.calls] == [1.0, 0.4]
            assert await pool.fetchval("SELECT count(*) FROM intents WHERE recognition_id=$1", record["id"]) == 2
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


@pytest.mark.parametrize("reason", ["long_range", "future", "reversed", "unconfirmed", "intents_off", "recognition_off"])
def test_recognize_once_refuses_invalid_or_unconfirmed_requests(monkeypatch, reason):
    async def check(pool):
        await configured(pool)
        values = {"source_id": SOURCE, "chat_id": "g", "db": pool, "now": NOW}
        if reason == "long_range":
            values["since"] = NOW - timedelta(days=31)
        elif reason == "future":
            values["until"] = NOW + timedelta(seconds=1)
        elif reason == "reversed":
            values.update(since=NOW, until=SINCE)
        elif reason == "unconfirmed":
            await pool.execute("UPDATE subscriptions SET target_confirmed_version=NULL")
        else:
            key = "intents.enabled" if reason == "intents_off" else "intents.on_request.enabled"
            await pool.execute("UPDATE system_settings SET value='false' WHERE key=$1", key)
        with pytest.raises((ValueError, db_intents.IntentRefused)):
            await intent_recognition.preview(**values)
        assert await pool.fetchval("SELECT count(*) FROM intent_recognitions") == 0
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_recognize_once_refuses_overlap_and_marks_interrupted_work_failed(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, REPLY)
    entered, release = asyncio.Event(), asyncio.Event()
    async def during():
        entered.set()
        await release.wait()
    fake.during = during
    async def check(pool):
        await configured(pool)
        await add(pool, "Synthetic request", chat_id="g", sent_at=NOW - timedelta(hours=1))
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(db_intents.IntentRefused, match="already running"):
                await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            await service.stop()
            row = await pool.fetchrow("SELECT * FROM intent_recognitions")
            assert row["state"] == "failed" and row["finished_at"] is not None
            # Simulate a persisted running record after a process crash and recover it on startup.
            await pool.execute("UPDATE intent_recognitions SET state='running',finished_at=NULL,error=NULL")
            await service.start()
            row = await pool.fetchrow("SELECT * FROM intent_recognitions")
            assert row["state"] == "failed" and "restart" in row["error"]
        finally:
            release.set()
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


@pytest.mark.parametrize("requested_via,reply", [("manager", ("channel-lark", DM)), ("dashboard", None)])
def test_recognize_once_posts_one_result_message_to_the_asking_dm(monkeypatch, tmp_path, requested_via, reply):
    processor_says(monkeypatch, REPLY)
    async def check(pool):
        await configured(pool)
        await add(pool, "Synthetic request", chat_id="g", sent_at=NOW - timedelta(hours=1))
        sent = []
        async def post(channel, target, text, **kwargs):
            sent.append((channel, target, text))
            return PostReceipt(PostOutcome.SENT, ("om_synthetic_result",))
        service = intent_recognition.IntentRecognitions(SimpleNamespace(post=post), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            await service.begin(source_id=SOURCE, chat_id="g", requested_via=requested_via, reply=reply)
            record = await finished(service, pool)
            iid = await pool.fetchval("SELECT id FROM intents WHERE recognition_id=$1", record["id"])
            assert record["state"] == "done" and iid is not None
            assert len(sent) == (1 if reply else 0)
            if reply:
                channel, target, text = sent[0]
                assert channel == reply[0] and target.id == reply[1]
                assert f"R-{record['id']}" in text and f"I-{iid:04}" in text
                assert "Synthetic upload bug" in text and "识别完成" in text
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


@pytest.mark.parametrize("failure", ["runtime", "blocked", "invalid_reply"])
def test_recognize_once_keeps_committed_windows_when_a_later_window_fails(monkeypatch, tmp_path, failure):
    fake = processor_says(monkeypatch, REPLY)
    later = ((None, processor.ProcessorFailure.FAILED) if failure == "runtime" else
             (None, processor.ProcessorFailure.BLOCKED) if failure == "blocked" else ("unreadable", None))
    fake.outcomes = [(REPLY, None), later, later]
    async def check(pool):
        await configured(pool)
        await many(pool, 1001)
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            record = await finished(service, pool)
            assert record["state"] == "failed" and record["error"] and record["finished_at"] is not None
            assert record["events_read"] == 500
            assert await pool.fetchval("SELECT count(*) FROM intents WHERE recognition_id=$1", record["id"]) == 1
            assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'") == 0
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_recognize_once_records_spend_but_stores_nothing_when_disabled_during_a_call(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, REPLY)
    async def check(pool):
        await configured(pool)
        await add(pool, "Synthetic request", chat_id="g", sent_at=NOW - timedelta(hours=1))
        async def disable():
            await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.on_request.enabled'")
        fake.during = disable
        service = intent_recognition.IntentRecognitions(SimpleNamespace(), runtime_root=tmp_path,
                                                        claude_bin="unused", clock=lambda: NOW)
        await service.start()
        try:
            await service.begin(source_id=SOURCE, chat_id="g", requested_via="dashboard", reply=None)
            record = await finished(service, pool)
            assert record["state"] == "failed" and record["events_read"] == 0
            assert record["cost_usd"] == fake.cost
            assert await pool.fetchval("SELECT count(*) FROM intents") == 0
        finally:
            await service.stop()
    run_db(check, monkeypatch, setup=seed_role_configs)
