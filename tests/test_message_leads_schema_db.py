"""The leads and intents tables: what the schema itself guarantees (a deleted message takes what was made from it),
the upgrade of a database without them, and the one search every surface uses."""

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from looper.agents import leads, leads_model
from looper.db import connection
from tests import leads_support as sup
from tests import subscription_support as stream
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _events(pool, *texts, chat_id="oc_release", chat_name="Release room", sender="ou_alice", kind="group"):
    await stream.source(pool)
    return [await stream.add(pool, text, chat_id=chat_id, chat_name=chat_name, kind=kind, sender=sender,
                             sent_at=NOW - timedelta(hours=len(texts) - i)) for i, text in enumerate(texts)]


def test_deleting_any_source_message_deletes_the_lead_and_the_intent_made_from_it(monkeypatch):
    async def check(pool):
        first, second, other = await _events(pool, "ship friday", "yes friday", "unrelated")
        both = await sup.lead(pool, [first, second], people=[("ou_alice", "sender")])
        kept = await sup.lead(pool, [other])
        target = await sup.intent(pool, [first])
        merged = await sup.intent(pool, [other], state="merged", merged_into=target)
        await pool.execute("INSERT INTO intent_posts (intent_id, kind, channel_id, chat, text) "
                           "VALUES ($1, 'push', 'lark', 'ou_operatorsynthetic', 'x')", target)
        await pool.execute("DELETE FROM source_events WHERE seq = $1", second)
        assert await pool.fetchval("SELECT count(*) FROM message_leads WHERE id = $1", both) == 0
        assert await pool.fetchval("SELECT count(*) FROM message_lead_sources WHERE lead_id = $1", both) == 0
        assert await pool.fetchval("SELECT count(*) FROM message_lead_people WHERE lead_id = $1", both) == 0
        assert await pool.fetchval("SELECT count(*) FROM message_leads WHERE id = $1", kept) == 1
        await pool.execute("DELETE FROM source_events WHERE seq = $1", first)
        # The intent goes, with its posts and the intent merged into it (the same matter).
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id = ANY($1::bigint[])", [target, merged]) == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 0
        await pool.execute("DELETE FROM message_leads WHERE id = $1", kept)  # a lead deleted itself takes its sources
        assert await pool.fetchval("SELECT count(*) FROM message_lead_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_events WHERE seq = $1", other) == 1

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("statement", [
    "UPDATE message_leads SET superseded_by = id WHERE id = $1",                  # linked but still active
    "UPDATE message_leads SET state = 'inaccurate' WHERE id = $1",                # inaccurate without who marked it
    "UPDATE message_leads SET state = 'promoted' WHERE id = $1",                  # promoted without where to
    "UPDATE message_leads SET last_sent_at = first_sent_at - interval '1 hour' WHERE id = $1",
])
def test_a_lead_state_needs_what_goes_with_it(monkeypatch, statement):
    async def check(pool):
        (seq,) = await _events(pool, "ship friday")
        lead_id = await sup.lead(pool, [seq])
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(statement, lead_id)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("columns", [
    {"state": "merged"},                                        # merged into nothing
    {"state": "tasked", "handled_at": None, "handled_via": None},  # handled without when
    {"reject_reason": "not mine"},                              # a reason for a pending intent
])
def test_an_intent_state_needs_what_goes_with_it(monkeypatch, columns):
    async def check(pool):
        (seq,) = await _events(pool, "please fix it")
        state = columns.get("state", "pending")
        extra = {key: value for key, value in columns.items() if key != "state"}
        with pytest.raises(asyncpg.CheckViolationError):
            await sup.intent(pool, [seq], state=state, **extra)

    run_db(check, monkeypatch, timeout=20)


def test_the_extraction_switch_the_output_kinds_and_the_builtin_types(monkeypatch):
    async def check(pool):
        await _events(pool, "hello")
        with pytest.raises(asyncpg.CheckViolationError):  # excluded without when
            await pool.execute("UPDATE source_chats SET extraction = 'excluded' WHERE chat_id = 'oc_release'")
        await pool.execute("UPDATE source_chats SET extraction = 'excluded', extraction_excluded_at = now() "
                           "WHERE chat_id = 'oc_release'")
        checks = {row["conname"]: row["def"] for row in await pool.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = ANY($1::text[])",
            ["subscriptions_output_kind_check", "subscription_runs_outcome_check"])}
        assert all(word in checks["subscriptions_output_kind_check"] for word in ("'push'", "'leads'", "'intents'"))
        assert "'extracted'" in checks["subscription_runs_outcome_check"]
        types = await pool.fetch("SELECT t.key, t.builtin, t.version, v.priority, v.edited_via FROM intent_types t "
                                 "JOIN intent_type_versions v ON v.type_key = t.key AND v.version = t.version "
                                 "ORDER BY t.key")
        assert [(r["key"], r["builtin"], r["version"], r["edited_via"]) for r in types] == [
            (key, True, 1, "builtin") for key in sorted(leads_model.BUILTIN_INTENT_TYPES)]
        assert {r["key"]: r["priority"] for r in types}[leads_model.ALERT_TYPE] == "urgent"
        with pytest.raises(asyncpg.CheckViolationError):  # a built-in type is never archived
            await pool.execute("UPDATE intent_types SET archived_at = now() WHERE key = 'dev_task'")

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_tables_gets_them_on_upgrade(monkeypatch):
    async def check(pool):
        await pool.execute(
            "DROP TABLE intent_posts, intent_sources, intents, intent_alert_senders, message_chat_projects, "
            "intent_type_projects, intent_type_versions, intent_types, message_lead_people, message_lead_sources, "
            "message_leads; ALTER TABLE source_chats DROP CONSTRAINT source_chats_extraction_check, "
            "DROP COLUMN extraction, DROP COLUMN extraction_excluded_at; "
            "ALTER TABLE subscription_runs DROP CONSTRAINT subscription_runs_extract_counts_check, "
            "DROP COLUMN extract_counts; DELETE FROM schema_migrations WHERE name = 'message_leads'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        (seq,) = await _events(pool, "ship friday")
        await sup.lead(pool, [seq])
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing
        assert await pool.fetchval("SELECT count(*) FROM message_leads") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_type_versions") == 3

    run_db(check, monkeypatch, timeout=30)


def test_search_matches_every_word_in_the_lead_its_chats_or_its_people(monkeypatch):
    async def check(pool):
        await stream.source(pool)
        await stream.person(pool, "ou_alice", "Alice Zhang")
        ship, chat = await _events(pool, "ship it", "moved", chat_id="oc_payments", chat_name="Payments <at>team</at>")
        a = await sup.lead(pool, [ship], summary="Release moves to Friday", people=[("ou_alice", "sender")])
        b = await sup.lead(pool, [chat], summary="Release owner is <at user_id=\"all\">所有人</at> Bob", kind="fact")

        async def ids(**query):
            found = await leads.search(now=NOW, db=pool, **query)
            return [lead["id"] for lead in found["leads"]]

        assert await ids(words=["release"]) == [f"lead-{b}", f"lead-{a}"]      # newest source first
        assert await ids(words=["release", "friday"]) == [f"lead-{a}"]          # every word
        assert await ids(words=["payments", "owner"]) == [f"lead-{b}"]         # a word in the chat's name
        assert await ids(words=["alice", "friday"]) == [f"lead-{a}"]           # a word in a person's name
        assert await ids(words=["100%"]) == []                                 # a wildcard is a character
        assert await ids(chat="payments") == [f"lead-{b}", f"lead-{a}"]
        assert await ids(chat="oc_payments") == [f"lead-{b}", f"lead-{a}"]
        assert await ids(person="Alice") == [f"lead-{a}"]
        assert await ids(person="ou_alice") == [f"lead-{a}"]
        assert await ids(kind="fact") == [f"lead-{b}"]
        found = await leads.search(words=["owner"], now=NOW, db=pool)
        assert found["notice"] == leads_model.NOTICE
        shown = found["leads"][0]
        assert set(shown) == set(leads.LEAD_FIELDS) and set(shown["sources"][0]) == set(leads.SOURCE_FIELDS)
        assert "<at" not in shown["summary"] and "所有人" in shown["summary"] and "@" not in shown["summary"]
        assert shown["sources"][0]["chat"] == "Payments team"
        assert shown["sources"][0]["link"].endswith("openChatId=oc_payments")

    run_db(check, monkeypatch, timeout=20)


def test_search_states_order_and_paging(monkeypatch):
    async def check(pool):
        seqs = await _events(pool, "a", "b", "c", "d", "e")
        active_low = await sup.lead(pool, [seqs[0]], summary="note one", confidence="low")
        active_high = await sup.lead(pool, [seqs[1]], summary="note two")
        expired = await sup.lead(pool, [seqs[2]], summary="note three", valid_until=NOW - timedelta(days=1))
        wrong = await sup.lead(pool, [seqs[3]], summary="note four", state="inaccurate")
        many = await sup.lead(pool, seqs, summary="note five")

        async def found(**query):
            return await leads.search(words=["note"], now=NOW, db=pool, **query)

        assert [lead["id"] for lead in (await found())["leads"]] == [
            f"lead-{many}", f"lead-{active_high}", f"lead-{active_low}"]  # active only; high first, then newest
        assert [lead["id"] for lead in (await found(state="expired"))["leads"]] == [f"lead-{expired}"]
        assert (await found(state="expired"))["leads"][0]["state"] == leads_model.EXPIRED
        assert [lead["id"] for lead in (await found(state="all"))["leads"]][3:] == [f"lead-{expired}", f"lead-{wrong}"]
        page = await found(state="all", limit=2)
        assert len(page["leads"]) == 2 and page["more"] == 3
        widest = next(lead for lead in (await found())["leads"] if lead["id"] == f"lead-{many}")
        assert len(widest["sources"]) == 3 and widest["sources_total"] == 5
        assert [s["sent_at"] for s in widest["sources"]] == sorted(s["sent_at"] for s in widest["sources"])
        with pytest.raises(ValueError):
            await found(state="stale")

    run_db(check, monkeypatch, timeout=20)
