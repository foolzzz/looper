"""Operator-only lead administration on the HTTP path and a disposable PostgreSQL schema."""

from datetime import datetime, timezone

import pytest

from looper.agents import leads_model
from looper.db import lead_admin
from tests import leads_support as leads, subscription_support as stream
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime.now(timezone.utc)


async def seed(pool):
    await stream.source(pool)
    await stream.person(pool, "ou_alice", '<at user_id="all">Alice</at>')
    event = await stream.add(pool, "synthetic source", sent_at=NOW, chat_name='<at user_id="all">Design</at>')
    lead = await leads.lead(pool, [event], summary='<at user_id="all">Ship</at> on Friday',
                            people=[("ou_alice", "sender")])
    return event, lead


def test_operator_search_mark_and_delete(monkeypatch, tmp_path):
    async def check(pool):
        _, number = await seed(pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("leads.settings_get")
            assert status == 200 and answer["result"] == {"enabled": False, "valid_days": 90, "subscriptions": []}
            status, answer = await call("leads.settings_put", {"enabled": True, "valid_days": 30})
            assert status == 200 and answer["result"]["valid_days"] == 30
            status, answer = await call("leads.search", {"query": "Ship Friday", "chat": "oc_ops",
                                                        "person": "ou_alice", "days": 1, "kind": "decision"})
            assert status == 200, answer
            result = answer["result"]
            assert set(result) == {"notice", "leads", "more"}
            assert result["notice"] == leads_model.NOTICE
            (lead,) = result["leads"]
            assert lead["id"] == f"lead-{number}" and lead["summary"] == "Ship on Friday"
            assert lead["sources"][0]["sender"] == "Alice" and lead["sources"][0]["chat"] == "Design"
            status, answer = await call("leads.mark", {"lead_id": lead["id"], "note": "  incorrect  "})
            assert status == 200 and answer["result"]["lead"]["state"] == "inaccurate"
            row = await pool.fetchrow("SELECT marked_via, marked_note FROM message_leads WHERE id=$1", number)
            assert tuple(row) == ("dashboard", "incorrect")
            status, answer = await call("leads.search", {"state": "inaccurate"})
            assert status == 200 and answer["result"]["leads"][0]["id"] == lead["id"]
            status, answer = await call("leads.delete", {"lead_id": lead["id"]})
            assert status == 200 and answer["result"] == {"deleted": lead["id"]}
            assert await pool.fetchval("SELECT count(*) FROM message_lead_sources") == 0
            assert await pool.fetchval("SELECT count(*) FROM message_lead_people") == 0
            for action in ("mark", "delete"):
                status, answer = await call("leads." + action, {"lead_id": lead["id"]})
                assert status == 404 and answer["error"]["code"] == "not_found"
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,body", [
    ("search", {}), ("mark", {"lead_id": "lead-1"}), ("delete", {"lead_id": "lead-1"}),
    ("chats", {}), ("set_chat", {"chat_id": "oc_ops", "extraction": "excluded"}),
])
def test_disabled_operations_conflict(monkeypatch, tmp_path, action, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("leads." + action, body)
            assert status == 409 and answer["error"]["message"] == "message leads are not enabled"
    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("action,body", [
    ("search", {}), ("mark", {"lead_id": "lead-1"}), ("delete", {"lead_id": "lead-1"}),
    ("settings_get", {}), ("settings_put", {"enabled": True}), ("chats", {}),
    ("set_chat", {"chat_id": "oc_ops", "extraction": "excluded"}),
])
def test_client_cannot_use_any_leads_operation(monkeypatch, tmp_path, action, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("leads." + action, body, client=True)
            assert status == 403
    run_db(check, monkeypatch, timeout=20)


def test_disabling_leads_disables_only_its_subscriptions(monkeypatch, tmp_path):
    async def check(pool):
        await stream.source(pool)
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
        for output in ("push", "leads"):
            await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,kind,enabled,next_fire_at) "
                               "VALUES ($1,'p',$1,'0 * * * *','UTC','Synthetic instructions','subscription',true,now())",
                               output)
            await pool.execute("INSERT INTO subscriptions(schedule_id,source_id,chat_kinds,sender_kinds,max_age_minutes,"
                               "max_events,text_max_chars,output_kind) "
                               "VALUES ($1,$2,ARRAY['group'],ARRAY['user'],60,50,200,$1)", output, stream.SOURCE)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("leads.settings_put", {"enabled": False, "valid_days": 1})
            assert status == 200 and answer["result"]["subscriptions"] == [
                {"schedule_id": "leads", "name": "leads", "enabled": False, "project_id": "p"}]
            assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='push'")
            assert await pool.fetchval("SELECT next_fire_at FROM schedules WHERE id='leads'") is None
            await call("leads.settings_put", {"enabled": True})
            assert not await pool.fetchval("SELECT enabled FROM schedules WHERE id='leads'")
    run_db(check, monkeypatch, timeout=25)


def test_chat_extraction_and_deletion_preserve_tasked_and_other_chats(monkeypatch, tmp_path):
    async def check(pool):
        event, _ = await seed(pool)
        other = await stream.add(pool, "other synthetic source", chat_id="oc_other", sent_at=NOW)
        other_lead = await leads.lead(pool, [other])
        for state in ("pending", "rejected", "expired"):
            await leads.intent(pool, [event], state=state)
        tasked = await leads.intent(pool, [event], state="tasked")
        await lead_admin.put_settings(enabled=True, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("leads.chats")
            assert status == 200 and answer["result"]["total"] == 2
            row = next(c for c in answer["result"]["chats"] if c["chat_id"] == "oc_ops")
            assert row["name"] == "Design" and (row["leads"], row["open_intents"]) == (1, 1)
            status, answer = await call("leads.set_chat", {"chat_id": "oc_ops", "extraction": "excluded",
                                                          "delete_extracted": True})
            assert status == 200 and answer["result"] == {"chat_id": "oc_ops", "extraction": "excluded",
                                                         "deleted_leads": 1, "deleted_intents": 3}
            assert await pool.fetchval("SELECT id FROM message_leads") == other_lead
            assert await pool.fetchval("SELECT id FROM intents") == tasked
            status, answer = await call("leads.chats", {"extraction": "excluded"})
            (row,) = answer["result"]["chats"]
            assert row["extraction_excluded_at"] is not None and row["leads"] == row["open_intents"] == 0
            status, _ = await call("leads.set_chat", {"chat_id": "oc_ops", "extraction": "included"})
            assert status == 200
            assert await pool.fetchval("SELECT extraction_excluded_at FROM source_chats WHERE chat_id='oc_ops'") is None
            status, _ = await call("leads.set_chat", {"chat_id": "missing", "extraction": "excluded"})
            assert status == 404
    run_db(check, monkeypatch, timeout=30)


def test_chat_filters_literal_wildcards_counts_and_paging(monkeypatch, tmp_path):
    async def check(pool):
        event, _ = await seed(pool)
        await leads.lead(pool, [event, await stream.add(pool, "duplicate chat source", sent_at=NOW)])
        await pool.execute("UPDATE source_chats SET name='Room_100%',last_message_at=$1", NOW)
        await stream.chat(pool, "oc_new", name="RoomX100Y")
        await stream.chat(pool, "oc_hidden")
        await pool.execute("UPDATE source_chats SET inclusion='excluded',excluded_at=now(),exclusion_reason='operator' "
                           "WHERE chat_id='oc_hidden'")
        await lead_admin.put_settings(enabled=True, db=pool)
        async with v1_api(tmp_path, pool) as call:
            for query in ("_100%", "oc_ops"):
                status, answer = await call("leads.chats", {"query": query})
                assert status == 200 and answer["result"]["total"] == 1
                assert answer["result"]["chats"][0]["leads"] == 2
            status, answer = await call("leads.chats", {"limit": 1, "offset": 5})
            assert status == 200 and answer["result"]["chats"] == [] and answer["result"]["total"] == 2
    run_db(check, monkeypatch, timeout=25)


def test_missing_ambiguous_and_explicit_source(monkeypatch, tmp_path):
    async def check(pool):
        await lead_admin.put_settings(enabled=True, db=pool)
        async with v1_api(tmp_path, pool) as call:
            for action in ("search", "chats"):
                status, answer = await call("leads." + action)
                assert status == 409 and answer["error"]["message"] == "no message source is set up"
            await stream.source(pool)
            await stream.source(pool, "other")
            for action in ("search", "chats"):
                status, answer = await call("leads." + action)
                assert status == 400 and all(s in answer["error"]["message"] for s in (stream.SOURCE, "other"))
                status, _ = await call("leads." + action, {"source_id": "other"})
                assert status == 200
                status, _ = await call("leads." + action, {"source_id": "missing"})
                assert status == 404
    run_db(check, monkeypatch, timeout=25)


@pytest.mark.parametrize("action,body", [
    ("search", {"since": "2026-11-18", "days": 1}), ("search", {"since": "2026-02-30"}),
    ("search", {"query": "x" * 201}), ("search", {"limit": 51}), ("search", {"days": 0}),
    ("search", {"state": "unknown"}), ("search", {"kind": "unknown"}),
    ("settings_put", {}), ("settings_put", {"valid_days": 366}), ("settings_put", {"enabled": "true"}),
    ("mark", {"lead_id": "wrong"}), ("mark", {"lead_id": "lead-1", "note": "x" * 501}),
    ("set_chat", {"chat_id": "oc_ops", "extraction": "included", "delete_extracted": True}),
    ("chats", {"offset": -1}), ("chats", {"limit": 201}), ("chats", {"extra": True}),
])
def test_invalid_arguments(monkeypatch, tmp_path, action, body):
    async def check(pool):
        await stream.source(pool)
        await lead_admin.put_settings(enabled=True, db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("leads." + action, body)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    run_db(check, monkeypatch, timeout=20)


def test_marking_again_or_after_other_states(monkeypatch):
    async def check(pool):
        event, number = await seed(pool)
        replacement = await leads.lead(pool, [event])
        await pool.execute("UPDATE message_leads SET state='superseded',superseded_by=$2 WHERE id=$1", number, replacement)
        assert await lead_admin.mark_inaccurate(number, via="cli", note="old", db=pool)
        assert await lead_admin.mark_inaccurate(number, via="dashboard", note="  ", db=pool)
        row = await pool.fetchrow("SELECT state,superseded_by,marked_via,marked_note FROM message_leads WHERE id=$1", number)
        assert tuple(row) == ("inaccurate", None, "dashboard", None)
        await pool.execute("UPDATE message_leads SET state='promoted',promoted_kind='vault_proposal',promoted_ref='synthetic' "
                           "WHERE id=$1", replacement)
        assert await lead_admin.mark_inaccurate(replacement, via="cli", note=None, db=pool)
        assert not await lead_admin.mark_inaccurate(999999, via="cli", note=None, db=pool)
        for values in ({"via": "api", "note": None}, {"via": "cli", "note": "x" * 501}):
            with pytest.raises(ValueError):
                await lead_admin.mark_inaccurate(number, db=pool, **values)
        await lead_admin.put_settings(enabled=True, db=pool)
        with pytest.raises(ValueError):
            await lead_admin.put_settings(enabled=False, valid_days=0, db=pool)
        assert await leads_model.leads_enabled(pool) is True
    run_db(check, monkeypatch, timeout=20)
