"""Followed-group reads on disposable PostgreSQL, with synthetic messages and a scripted processor."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import chat_settings, extraction, intents, subscription_runs, subscriptions
from looper.agents.serve import seed_role_configs
from looper.db import connection, schedules as db_schedules, subscriptions as db_subs
from tests import leads_support
from tests.db_support import DSN, run_db
from tests.subscription_support import DM, SOURCE, add, chat, processor_says, source

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)


async def followed(pool, chat_id="g"):
    await chat(pool, chat_id)
    await pool.execute("INSERT INTO intent_group_reads(source_id,chat_id,added_at,read_from) VALUES($1,$2,$3,$3)",
                       SOURCE, chat_id, START)


def sub(**fields):
    return {"source_id": SOURCE, "include_chats": [], "exclude_chats": [], "max_age_minutes": 10080,
            "max_events": 500, **fields}


async def window(pool, allowance=None, **fields):
    top = await db_subs.committed_seq(pool, SOURCE)
    return await db_subs.extraction_window(pool, sub(**fields), output_kind="intents", from_seq=0,
        to_seq=top, now=NOW, own_apps=(), group_read=allowance or db_subs.GroupRead(200, {"g": 500}))


@pytest.mark.parametrize("case", ["human_live_after", "mentions_me", "operator_own", "bot_app", "other_kind",
    "imported", "history", "sent_before_start", "ingested_before_start", "unlisted_group", "unbound_user"])
def test_followed_group_reads_only_live_human_messages_since_read_from(monkeypatch, case):
    async def check(pool):
        await source(pool)
        await followed(pool)
        fields = {"sent_at": NOW - timedelta(minutes=5), "ingested_at": NOW - timedelta(minutes=4), "chat_id": "g"}
        if case == "mentions_me":
            fields["mentions_me"] = True
        elif case == "operator_own":
            fields["sender"] = DM
        elif case in ("bot_app", "other_kind"):
            fields["sender_kind"] = "app" if case == "bot_app" else "other"
        elif case in ("imported", "history"):
            fields["origin"] = "import" if case == "imported" else "history"
        elif case == "sent_before_start":
            fields["sent_at"] = START - timedelta(seconds=1)
        elif case == "ingested_before_start":
            fields["ingested_at"] = START - timedelta(seconds=1)
        elif case == "unlisted_group":
            fields["chat_id"] = "h"
        elif case == "unbound_user":
            await pool.execute("UPDATE event_sources SET user_open_id=NULL WHERE id=$1", SOURCE)
        seq = await add(pool, "synthetic request", **fields)
        result = await window(pool)
        expected = case in ("human_live_after", "mentions_me", "operator_own", "unbound_user")
        assert [r["seq"] for r in result["rows"]] == ([seq] if expected else [])
        if expected:
            assert result["rows"][0]["via_group"] is (case not in ("mentions_me", "operator_own"))
        # A positive control proves that the selector remains live in every negative case.
        control = await add(pool, "direct control", chat_id="dm", kind="p2p", sent_at=NOW)
        assert control in [r["seq"] for r in (await window(pool))["rows"]]
    run_db(check, monkeypatch)


@pytest.mark.parametrize("how", ["extraction_excluded", "stream_excluded", "subscription_excludes",
                                     "subscription_includes_other"])
def test_exclusions_win_over_a_followed_group(monkeypatch, how):
    async def check(pool):
        await source(pool)
        await followed(pool)
        await add(pool, "synthetic request", chat_id="g", sent_at=NOW)
        fields = {}
        if how == "extraction_excluded":
            await pool.execute("UPDATE source_chats SET extraction='excluded',extraction_excluded_at=now() WHERE chat_id='g'")
        elif how == "stream_excluded":
            await pool.execute("UPDATE source_chats SET inclusion='excluded',exclusion_reason='operator' WHERE chat_id='g'")
        elif how == "subscription_excludes":
            fields["exclude_chats"] = ["g"]
        else:
            fields["include_chats"] = ["h"]
        result = await window(pool, **fields)
        assert result["rows"] == []
        seq = await add(pool, "control", chat_id="h", kind="p2p", sent_at=NOW)
        assert [r["seq"] for r in (await window(pool, **fields))["rows"]] == [seq]
    run_db(check, monkeypatch)


async def schedule(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES('p','Synthetic project')")
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true'),"
                       "('intents.group_read.enabled','true')")
    await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind,run_budget_usd,"
                       "run_timeout_mode,run_timeout_seconds) VALUES('s','p','Synthetic intents','*/10 * * * *','UTC',$1,"
                       "'subscription',0.3,'seconds',20)", extraction.INTENTS_INSTRUCTIONS)
    await db_subs.create(pool, "s", subscriptions.preset("intents", source_id=SOURCE, zone_name="UTC")["subscription"])
    row = await db_schedules.get_schedule("s")
    await db_subs.update(pool, "s", {"target_confirmed_at": NOW,
                                    "target_confirmed_version": await subscriptions.config_version(row)})
    return await db_schedules.get_schedule("s")


async def run_once(pool, tmp_path, row, run_id="r"):
    async def own_apps():
        return ()
    runner = subscription_runs.SubscriptionRuns(poster=None, own_apps=own_apps, runtime_root=tmp_path,
                                               clock=lambda: NOW, day_start=lambda _: NOW.replace(hour=0))
    async with pool.acquire() as conn, conn.transaction():
        picked = await runner.plan(conn, row, now=NOW, scheduled_for=NOW)
        if not picked:
            return None
        await conn.execute("INSERT INTO schedule_runs(id,schedule_id,scheduled_for,fired_at,trigger,status) "
                           "VALUES($1,'s',$2,$2,'manual','running')", run_id, NOW)
        await runner.record(conn, run_id, picked)
    await runner.execute(run_id)
    result = await db_subs.get_run(run_id)
    if result["extract_counts"] is not None:
        result["extract_counts"] = json.loads(result["extract_counts"])
    return result


def test_followed_rows_are_capped_per_run_and_per_group_per_day(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, "NONE")
    async def check(pool):
        await source(pool)
        row = await schedule(pool)
        await followed(pool)
        await followed(pool, "h")
        g = [await add(pool, "group request", chat_id="g", sent_at=NOW) for _ in range(4)]
        direct = await add(pool, "direct", chat_id="dm", kind="p2p", sent_at=NOW)
        h = await add(pool, "other group", chat_id="h", sent_at=NOW)
        capped = await window(pool, db_subs.GroupRead(3, {"g": 10, "h": 10}))
        assert [r["seq"] for r in capped["rows"]] == g[:3] and capped["more"] and capped["last_seq"] == g[2]
        assert capped["group_over_cap"] == {}
        capped = await window(pool, db_subs.GroupRead(3, {"g": 2, "h": 10}))
        assert [r["seq"] for r in capped["rows"]] == [*g[:2], direct, h]
        assert not capped["more"] and capped["group_over_cap"] == {"g": 2}
        zero = await window(pool, db_subs.GroupRead(3, {"g": 0, "h": 10}))
        assert [r["seq"] for r in zero["rows"]] == [direct, h] and zero["group_over_cap"] == {"g": 4}
        await pool.execute("INSERT INTO intent_group_read_days(source_id,chat_id,day,read) VALUES($1,'g',$2,498),"
                           "($1,'h',$3,1)", SOURCE, NOW.date(), NOW.date() - timedelta(days=31))
        result = await run_once(pool, tmp_path, row)
        assert result["extract_counts"]["group_read"] == 3 and result["extract_counts"]["group_over_cap"] == 2
        assert "名单上的群 3 条，超出额度跳过 2 条" in result["output_text"]
        assert [tuple(r) for r in await pool.fetch("SELECT chat_id,read,skipped FROM intent_group_read_days ORDER BY chat_id")] == [
            ("g", 500, 2), ("h", 1, 0)]
        assert extraction.GROUP_REQUESTS_NOTE in fake.calls[0]["instructions"]
        # An all-over-quota window has no processor call; its skipped count and cursor still commit together.
        tail = await add(pool, "over cap", chat_id="g", sent_at=NOW)
        assert await run_once(pool, tmp_path, await db_schedules.get_schedule("s"), "r2") is None
        assert await pool.fetchval("SELECT skipped FROM intent_group_read_days WHERE chat_id='g'") == 3
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'") == tail
    run_db(check, monkeypatch, setup=seed_role_configs)


@pytest.mark.parametrize("revoke", ["delete", "switch", "restart_range"])
def test_revoking_stops_the_next_read_and_drops_what_a_running_run_read(monkeypatch, tmp_path, revoke):
    fake = processor_says(monkeypatch, json.dumps({"intents": [
        {"type": "bug_triage", "title": "Group bug", "summary": "Synthetic", "confidence": "high", "sources": ["m1"]},
        {"type": "dev_task", "title": "Direct request", "summary": "Synthetic", "confidence": "high", "sources": ["m2"]}]}))
    async def check(pool):
        await source(pool)
        row = await schedule(pool)
        await followed(pool)
        g = await add(pool, "group request", chat_id="g", sent_at=NOW)
        await add(pool, "direct request", chat_id="dm", kind="p2p", sent_at=NOW)
        existing = await leads_support.intent(pool, [g], via="followed_group")
        async def during():
            if revoke == "delete":
                await intents.group_read_delete("g", db=pool)
            elif revoke == "switch":
                await intents.group_read_settings_put(False, db=pool)
            else:
                await pool.execute("UPDATE intent_group_reads SET read_from=$1", NOW + timedelta(seconds=1))
        fake.during = during
        result = await run_once(pool, tmp_path, row)
        assert result["extract_counts"]["dropped"] == {"group_revoked": 1}
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id=$1", existing) == 1
        assert [r["via"] for r in await pool.fetch("SELECT via FROM intents WHERE id<>$1", existing)] == ["subscription"]
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'") == 2
        allowance = await extraction.group_read(pool, row, day=NOW.date())
        reread = await db_subs.extraction_window(pool, row["subscription"], output_kind="intents", from_seq=0,
                                               to_seq=2, now=NOW, own_apps=(), group_read=allowance)
        assert [r["seq"] for r in reread["rows"]] == [2]
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_re_adding_or_switching_on_starts_again_from_now(monkeypatch):
    async def check(pool):
        await source(pool)
        await followed(pool)
        await add(pool, "before readd", chat_id="g", sent_at=NOW - timedelta(minutes=1), ingested_at=NOW)
        await intents.group_read_delete("g", db=pool)
        await intents.group_read_put("g", db=pool, now=NOW)
        assert (await window(pool))["rows"] == []
        monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
        await pool.execute("UPDATE intent_group_reads SET added_at=now()-interval '2 hours',read_from=now()-interval '1 hour'")
        before = await pool.fetchval("SELECT read_from FROM intent_group_reads")
        await intents.group_read_settings_put(True, db=pool)
        after = await pool.fetchval("SELECT read_from FROM intent_group_reads")
        assert after > before
        await intents.group_read_settings_put(True, db=pool)
        assert await pool.fetchval("SELECT read_from FROM intent_group_reads") == after
        await intents.group_read_settings_put(False, db=pool)
        await intents.group_read_settings_put(True, db=pool)
        assert await pool.fetchval("SELECT read_from FROM intent_group_reads") > after
    run_db(check, monkeypatch)


def test_triage_and_leads_ignore_followed_groups(monkeypatch):
    async def check(pool):
        await source(pool)
        await followed(pool)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.group_read.enabled','true')")
        await add(pool, "ordinary group message", chat_id="g", sent_at=NOW)
        for kind in ("triage", "leads"):
            row = {"subscription": {**sub(), "output_kind": kind}}
            assert await extraction.group_read(pool, row, day=NOW.date()) is None
            baseline = await db_subs.extraction_window(pool, sub(), output_kind=kind, from_seq=0, to_seq=1,
                                                       now=NOW, own_apps=())
            assert [r["seq"] for r in baseline["rows"]] == ([1] if kind == "leads" else [])
            with pytest.raises(ValueError, match="only an intents run"):
                await db_subs.extraction_window(pool, sub(), output_kind=kind, from_seq=0, to_seq=1,
                    now=NOW, own_apps=(), group_read=db_subs.GroupRead(200, {"g": 500}))
        with pytest.raises(ValueError, match="only an intents run"):
            await db_subs.extraction_window(pool, sub(), output_kind="intents", from_seq=0, to_seq=1, now=NOW,
                own_apps=(), include_import=True, group_read=db_subs.GroupRead(200, {"g": 500}))
    run_db(check, monkeypatch)


def test_a_database_without_intent_group_reads_gets_it_on_upgrade(monkeypatch):
    async def check(pool):
        await source(pool)
        event = await add(pool, "old intent", chat_id="g", sent_at=NOW)
        iid = await leads_support.intent(pool, [event])
        await pool.execute("DROP TABLE intent_group_reads, intent_group_read_days; ALTER TABLE intents DROP COLUMN via; "
                           "DELETE FROM schema_migrations WHERE name='intent_group_reads'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT via FROM intents WHERE id=$1", iid) == "subscription"
        await followed(pool)
        async with pool.acquire() as conn, conn.transaction():
            await db_subs.count_group_day(conn, SOURCE, NOW.date(), read={"g": 2}, skipped={"g": 1})
        await pool.execute("DELETE FROM source_chats WHERE source_id=$1 AND chat_id='g'", SOURCE)
        assert await pool.fetchval("SELECT count(*) FROM intent_group_reads") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_group_read_days") == 0
    run_db(check, monkeypatch)


def test_a_successful_group_run_marks_intents_and_preserves_confirmation_version(monkeypatch, tmp_path):
    processor_says(monkeypatch, json.dumps({"intents": [{"type": "bug_triage", "title": "Synthetic group bug",
        "summary": "Synthetic bug", "confidence": "high", "sources": ["m1"]}]}))
    async def check(pool):
        await source(pool)
        row = await schedule(pool)
        before = await subscriptions.config_version(row)
        await followed(pool)
        await add(pool, "group bug", chat_id="g", sent_at=NOW, ingested_at=NOW)
        confirmation = await subscriptions.confirmation_text(row, now=NOW, action="确认")
        assert "另读名单上的群：1 个" in confirmation and "每个群每天最多 500 条" in confirmation
        tally = await db_subs.extraction_tally(pool, sub(), output_kind="intents", since=START, to_seq=1,
                    own_apps=(), include_import=False, zone_name="UTC", group_read=True)
        assert tally == [{"chat_kind": "group", "day": NOW.date(), "events": 1}]
        result = await run_once(pool, tmp_path, row)
        assert result["extract_counts"]["new"] == 1 and result["extract_counts"]["group_read"] == 1
        assert tuple(await pool.fetchrow("SELECT via,type_key FROM intents")) == ("followed_group", "bug_triage")
        await intents.group_read_delete("g", db=pool)
        assert await subscriptions.config_version(await db_schedules.get_schedule("s")) == before
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_input_trimming_counts_only_the_quota_skips_the_cursor_passes(monkeypatch, tmp_path):
    processor_says(monkeypatch, "NONE")
    monkeypatch.setattr(extraction, "INPUT_MAX_CHARS", 1500)
    async def check(pool):
        await source(pool)
        row = await schedule(pool)
        await followed(pool)
        g1 = await add(pool, "first group request", chat_id="g", sent_at=NOW, ingested_at=NOW)
        for n in range(5):
            await add(pool, "x" * 500, chat_id="dm", kind="p2p", sent_at=NOW + timedelta(seconds=n), ingested_at=NOW)
        tail = await add(pool, "group over quota at the tail", chat_id="g", sent_at=NOW, ingested_at=NOW)
        await pool.execute("INSERT INTO intent_group_read_days(source_id,chat_id,day,read) VALUES($1,'g',$2,499)",
                           SOURCE, NOW.date())
        result = await run_once(pool, tmp_path, row)
        cursor = await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'")
        assert g1 <= cursor < tail
        assert result["extract_counts"]["group_over_cap"] == 0
        assert await pool.fetchval("SELECT skipped FROM intent_group_read_days") == 0
    run_db(check, monkeypatch, setup=seed_role_configs)


def test_an_empty_execution_window_commits_only_its_quota_skips(monkeypatch, tmp_path):
    fake = processor_says(monkeypatch, "NONE")
    async def check(pool):
        await source(pool)
        row = await schedule(pool)
        await followed(pool)
        original = await add(pool, "direct control", chat_id="dm", kind="p2p", sent_at=NOW, ingested_at=NOW)
        tail = await add(pool, "group over cap", chat_id="g", sent_at=NOW, ingested_at=NOW)
        await pool.execute("INSERT INTO intent_group_read_days(source_id,chat_id,day,read) VALUES($1,'g',$2,500)",
                           SOURCE, NOW.date())
        runner = subscription_runs.SubscriptionRuns(poster=None, own_apps=lambda: None, runtime_root=tmp_path,
            clock=lambda: NOW, day_start=lambda _: NOW.replace(hour=0))
        async def own_apps():
            return ()
        runner._own_apps = own_apps
        async with pool.acquire() as conn, conn.transaction():
            picked = await runner.plan(conn, row, now=NOW, scheduled_for=NOW)
            await conn.execute("INSERT INTO schedule_runs(id,schedule_id,scheduled_for,fired_at,trigger,status) "
                               "VALUES('r','s',$1,$1,'manual','running')", NOW)
            await runner.record(conn, "r", picked)
        await pool.execute("DELETE FROM source_events WHERE seq=$1", original)
        await runner.execute("r")
        assert fake.calls == []
        assert await pool.fetchval("SELECT skipped FROM intent_group_read_days") == 1
        assert await pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id='s'") == tail
    run_db(check, monkeypatch, setup=seed_role_configs)
