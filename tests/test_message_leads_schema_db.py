"""The leads and intents tables: what the schema itself guarantees (a deleted message takes what was made from it),
the upgrade of a database without them, and the one search every surface uses."""

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from looper.agents import leads, leads_model
from looper.db import connection
from tests import leads_support as sup
from tests import memory_support as memory
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
        # The intent goes with its posts; the one merged into it was made from other messages and stays, pending.
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id = $1", target) == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_posts") == 0
        assert dict(await pool.fetchrow("SELECT state, merged_into FROM intents WHERE id = $1", merged)) == {
            "state": "pending", "merged_into": None}
        await pool.execute("DELETE FROM message_leads WHERE id = $1", kept)  # a lead deleted itself takes its sources
        assert await pool.fetchval("SELECT count(*) FROM message_lead_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM source_events WHERE seq = $1", other) == 1
        await pool.execute("DELETE FROM source_events WHERE seq = $1", other)  # and its own message takes it
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id = $1", merged) == 0

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("statement", [
    "UPDATE message_leads SET superseded_by = id WHERE id = $1",                  # linked but still active
    "UPDATE message_leads SET state = 'inaccurate' WHERE id = $1",                # inaccurate without who marked it
    "UPDATE message_leads SET state = 'promoted' WHERE id = $1",                  # promoted without where to
    "UPDATE message_leads SET last_sent_at = first_sent_at - interval '1 hour' WHERE id = $1",
    "UPDATE message_leads SET state = 'dropped' WHERE id = $1",                   # dropped without the check's verdict
    "UPDATE message_leads SET check_verdict = 'drop' WHERE id = $1",              # the check dropped it, yet active
    "UPDATE message_leads SET check_verdict = 'maybe' WHERE id = $1",
    "UPDATE message_leads SET check_verdict = 'keep', check_reason = repeat('x', 201) WHERE id = $1",
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
    {"state": "dropped", "handled_via": "system"},              # dropped without the check's verdict
    {"check_verdict": "drop"},                                  # the check dropped it, yet pending
    {"state": "dropped", "check_verdict": "drop", "handled_at": None, "handled_via": None},  # dropped, not handled
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
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = ANY($1::text[]) "
            "AND connamespace = current_schema()::regnamespace",
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
            memory.DROP_MEMORY +  # memory candidates point at leads: a database without leads predates them too
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
        assert await pool.fetchval("SELECT to_regclass('memory_candidate_sources') IS NOT NULL")

    run_db(check, monkeypatch, timeout=30)


_LEADS_CHECK_UNDONE = (
    "ALTER TABLE message_leads DROP CONSTRAINT message_leads_dropped, DROP COLUMN check_verdict, "
    "DROP COLUMN check_reason, DROP COLUMN check_record_id, DROP CONSTRAINT message_leads_state_check, "
    "ADD CONSTRAINT message_leads_state_check CHECK (state IN ('active', 'superseded', 'promoted', 'inaccurate')); "
    "ALTER TABLE intents DROP CONSTRAINT intents_dropped, DROP COLUMN check_verdict, DROP COLUMN check_reason, "
    "DROP COLUMN check_record_id, DROP CONSTRAINT intents_state_check, ADD CONSTRAINT intents_state_check "
    "CHECK (state IN ('pending', 'tasked', 'rejected', 'expired', 'merged')); "
    "ALTER TABLE subscriptions DROP COLUMN check_items, DROP COLUMN check_alert_at; "
    "ALTER TABLE subscription_runs DROP COLUMN check_status; "
    "ALTER TABLE schedule_notifications DROP CONSTRAINT schedule_notifications_kind_check, ADD CONSTRAINT "
    "schedule_notifications_kind_check CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert', "
    "'stale_recovered', 'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered')); "
    "ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check, ADD CONSTRAINT "
    "agent_setting_layers_role_check CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor', "
    "'advisor', 'advice_check')); "
    "DELETE FROM schema_migrations WHERE name IN ('leads_check', 'leads_check_online')")
_CHECK_CATALOG = (
    "SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
    "WHERE conrelid IN ('message_leads'::regclass, 'intents'::regclass, 'agent_setting_layers'::regclass, "
    "'subscriptions'::regclass, 'subscription_runs'::regclass, 'schedule_notifications'::regclass) ORDER BY 1, 2")


# What the leads check adds NOT VALID and validates after its migration commits (``connection._ONLINE_STEPS``).
_LEADS_CHECK_CONSTRAINTS = sorted([
    "agent_setting_layers_role_check", "message_leads_state_check", "message_leads_check_verdict_check",
    "message_leads_check_reason_check", "message_leads_check_record_id_fkey", "message_leads_dropped",
    "intents_state_check", "intents_check_verdict_check", "intents_check_reason_check", "intents_check_record_id_fkey",
    "intents_dropped", "subscription_runs_check_status_check", "schedule_notifications_kind_check"])


async def _validated(pool):
    return {row["conname"]: row["convalidated"] for row in await pool.fetch(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1::text[]) "
        "AND connamespace = current_schema()::regnamespace", _LEADS_CHECK_CONSTRAINTS)}


async def _markers(pool):
    return {row["name"] for row in await pool.fetch(
        "SELECT name FROM schema_migrations WHERE name IN ('leads_check', 'leads_check_online')")}


def test_a_fresh_database_ends_with_the_leads_check_constraints_validated(monkeypatch):
    async def check(pool):
        assert await _validated(pool) == dict.fromkeys(_LEADS_CHECK_CONSTRAINTS, True)
        assert await _markers(pool) == set()  # a fresh database records only its baseline

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_leads_check_gets_it_unvalidated_then_validated_online_once(monkeypatch):
    # Replay the released leads-check migration before explorer widened the role catalog.
    # Rolling back only that shared constraint does not recreate explorer's historical predecessor.
    for name in ("_MIGRATIONS", "_PENDING_MIGRATIONS"):
        monkeypatch.setattr(connection, name, tuple(m for m in getattr(connection, name)
                                                   if m.name != "platform_subagents"))
    monkeypatch.setattr(connection, "_ONLINE_STEPS", tuple(step for step in connection._ONLINE_STEPS
                                                         if step.migration != "platform_subagents"))

    async def stopped(conn):
        return None

    async def check(pool):
        fresh = [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)]
        (seq,) = await _events(pool, "ship friday")
        kept = await sup.lead(pool, [seq])
        await pool.execute(_LEADS_CHECK_UNDONE)
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        with monkeypatch.context() as patch:  # the start stops once the schema transaction committed
            patch.setattr(connection, "_finish_online", stopped)
            await connection.ensure_schema(pool)
        # Added without scanning the tables under the migration's lock: validated only after it commits.
        assert await _validated(pool) == dict.fromkeys(_LEADS_CHECK_CONSTRAINTS, False)
        assert await _markers(pool) == {"leads_check"}
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            await connection._finish_online(conn)
        assert await _validated(pool) == dict.fromkeys(_LEADS_CHECK_CONSTRAINTS, True)
        assert await _markers(pool) == {"leads_check", "leads_check_online"}
        assert [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)] == fresh
        assert await pool.fetchval("SELECT state FROM message_leads WHERE id = $1", kept) == "active"
        await pool.execute("UPDATE message_leads SET state = 'dropped', check_verdict = 'drop' WHERE id = $1", kept)
        await connection.ensure_schema(pool)  # and a second start changes nothing
        assert [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)] == fresh
        assert await pool.fetchval("SELECT state FROM message_leads WHERE id = $1", kept) == "dropped"

    run_db(check, monkeypatch, timeout=30)


_RUN_SKIPS_UNDONE = (
    "ALTER TABLE subscription_runs DROP CONSTRAINT subscription_runs_skipped, "
    "DROP CONSTRAINT subscription_runs_skip_reason_check, DROP COLUMN skip_reason, "
    "DROP CONSTRAINT subscription_runs_outcome_check, ADD CONSTRAINT subscription_runs_outcome_check "
    "CHECK (outcome IN ('pending', 'delivering', 'delivered', 'uncertain', 'quiet', 'dashboard_only', "
    "'output_invalid', 'failed', 'extracted')); "
    "ALTER TABLE subscriptions DROP COLUMN skip_alert_at; "
    "ALTER TABLE schedule_notifications DROP CONSTRAINT schedule_notifications_kind_check, ADD CONSTRAINT "
    "schedule_notifications_kind_check CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert', "
    "'stale_recovered', 'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered', 'check_alert', "
    "'check_recovered')); "
    "DELETE FROM schema_migrations WHERE name IN ('run_skips', 'run_skips_online')")
_RUN_SKIPS_CONSTRAINTS = ["subscription_runs_outcome_check", "subscription_runs_skip_reason_check",
                          "subscription_runs_skipped", "schedule_notifications_kind_check"]


async def _skips_validated(pool):
    return {row["conname"]: row["convalidated"] for row in await pool.fetch(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1::text[]) "
        "AND connamespace = current_schema()::regnamespace", _RUN_SKIPS_CONSTRAINTS)}


async def _skips_markers(pool):
    return {row["name"] for row in await pool.fetch(
        "SELECT name FROM schema_migrations WHERE name IN ('run_skips', 'run_skips_online')")}


@pytest.mark.parametrize("outcome, reason", [("skipped", None), ("failed", "blocked_by_hook"),
                                             ("skipped", "too_long")])
def test_a_skipped_run_has_a_typed_reason_and_only_a_skipped_one_has_one(monkeypatch, outcome, reason):
    async def check(pool):
        fresh = await _skips_validated(pool)
        assert fresh == dict.fromkeys(_RUN_SKIPS_CONSTRAINTS, True) and await _skips_markers(pool) == set()
        await pool.execute("ALTER TABLE subscription_runs DROP CONSTRAINT subscription_runs_run_id_fkey")
        await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, outcome, skip_reason) "
                           "VALUES ('r-ok', 1, 2, 'skipped', 'blocked_by_hook')")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, outcome, skip_reason) "
                               "VALUES ('r-bad', 1, 2, $1, $2)", outcome, reason)

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_run_skips_gets_them_unvalidated_then_validated_online_once(monkeypatch):
    async def stopped(conn):
        return None

    async def check(pool):
        fresh = [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)]
        await pool.execute(_RUN_SKIPS_UNDONE)
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        with monkeypatch.context() as patch:  # the start stops once the schema transaction committed
            patch.setattr(connection, "_finish_online", stopped)
            await connection.ensure_schema(pool)
        # Added without scanning the runs under the migration's lock: validated only after it commits.
        assert await _skips_validated(pool) == dict.fromkeys(_RUN_SKIPS_CONSTRAINTS, False)
        assert await _skips_markers(pool) == {"run_skips"}
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            await connection._finish_online(conn)
        assert await _skips_validated(pool) == dict.fromkeys(_RUN_SKIPS_CONSTRAINTS, True)
        assert await _skips_markers(pool) == {"run_skips", "run_skips_online"}
        assert [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)] == fresh
        await connection.ensure_schema(pool)  # and a second start changes nothing
        assert [tuple(row) for row in await pool.fetch(_CHECK_CATALOG)] == fresh
        assert await _skips_markers(pool) == {"run_skips", "run_skips_online"}

    run_db(check, monkeypatch, timeout=30)


def test_a_dropped_lead_is_out_of_the_default_search_and_last_of_all(monkeypatch):
    async def check(pool):
        seqs = await _events(pool, "a", "b", "c")
        active = await sup.lead(pool, [seqs[0]], summary="note one")
        dropped = await sup.lead(pool, [seqs[1]], summary="note two")
        wrong = await sup.lead(pool, [seqs[2]], summary="note three", state="inaccurate")
        await pool.execute("UPDATE message_leads SET state = 'dropped', check_verdict = 'drop' WHERE id = $1", dropped)

        async def found(**query):
            return [lead["id"] for lead in (await leads.search(words=["note"], now=NOW, db=pool, **query))["leads"]]

        assert await found() == [f"lead-{active}"]
        assert await found(state="all") == [f"lead-{active}", f"lead-{wrong}", f"lead-{dropped}"]
        assert await found(state="dropped") == [f"lead-{dropped}"]

    run_db(check, monkeypatch, timeout=20)


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


def test_an_intent_merged_into_a_deleted_one_is_pending_again_whatever_deletes_it(monkeypatch):
    async def check(pool):
        ours, theirs, shared = await _events(pool, "please fix the login page", "same login issue here", "both")
        target = await sup.intent(pool, [ours])
        elsewhere = await sup.intent(pool, [theirs], state="merged", merged_into=target)
        same_message = await sup.intent(pool, [shared], state="merged", merged_into=target)
        await pool.execute("INSERT INTO intent_sources (intent_id, event_seq) VALUES ($1, $2)", target, shared)
        await pool.execute("DELETE FROM intents WHERE id = $1", target)  # a code path deleting the target itself
        assert dict(await pool.fetchrow("SELECT state, merged_into, handled_at FROM intents WHERE id = $1",
                                        elsewhere)) == {"state": "pending", "merged_into": None, "handled_at": None}
        target = await sup.intent(pool, [ours])
        await pool.execute("UPDATE intents SET state = 'merged', merged_into = $2 WHERE id = ANY($1::bigint[])",
                           [elsewhere, same_message], target)
        await pool.execute("INSERT INTO intent_sources (intent_id, event_seq) VALUES ($1, $2)", target, shared)
        await pool.execute("DELETE FROM source_events WHERE seq = $1", shared)  # a message both were made from
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id = ANY($1::bigint[])",
                                   [target, same_message]) == 0
        assert await pool.fetchval("SELECT state FROM intents WHERE id = $1", elsewhere) == "pending"
        # The key itself never cascades: without the trigger, deleting a merge target fails instead.
        assert await pool.fetchval("SELECT confdeltype FROM pg_constraint WHERE conname = 'intents_merged_into_fkey' "
                                   "AND connamespace = current_schema()::regnamespace") in ("a", b"a")

    run_db(check, monkeypatch, timeout=20)


def test_a_database_whose_merge_key_cascades_is_upgraded(monkeypatch):
    async def check(pool):
        await pool.execute(
            "DROP TRIGGER \"A_intents_detach_merged\" ON intents; ALTER TABLE intents DROP CONSTRAINT intents_merged_into_fkey; "
            "ALTER TABLE intents ADD CONSTRAINT intents_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES intents(id) "
            "ON DELETE CASCADE; DELETE FROM schema_migrations WHERE name = 'intent_merges'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        ours, theirs = await _events(pool, "please fix it", "same here")
        target = await sup.intent(pool, [ours])
        merged = await sup.intent(pool, [theirs], state="merged", merged_into=target)
        await pool.execute("DELETE FROM source_events WHERE seq = $1", ours)
        assert await pool.fetchval("SELECT state FROM intents WHERE id = $1", merged) == "pending"
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing

    run_db(check, monkeypatch, timeout=30)


def test_a_chain_of_merges_deleted_in_one_statement_detaches_what_stays(monkeypatch):
    async def check(pool):
        a_seq, b_seq, c_seq = await _events(pool, "first report", "second report", "third report")
        a = await sup.intent(pool, [a_seq])
        b = await sup.intent(pool, [b_seq], state="merged", merged_into=a)
        c = await sup.intent(pool, [c_seq], state="merged", merged_into=b)
        await pool.execute("DELETE FROM intents WHERE id = ANY($1::bigint[])", [a, b])  # C -> B -> A, A and B at once
        assert await pool.fetchval("SELECT count(*) FROM intents WHERE id = ANY($1::bigint[])", [a, b]) == 0
        assert dict(await pool.fetchrow("SELECT state, merged_into FROM intents WHERE id = $1", c)) == {
            "state": "pending", "merged_into": None}

    run_db(check, monkeypatch, timeout=20)


def test_a_database_with_the_earlier_before_trigger_is_upgraded(monkeypatch):
    async def check(pool):
        await pool.execute(
            "DROP TRIGGER \"A_intents_detach_merged\" ON intents; "
            "CREATE OR REPLACE FUNCTION intents_detach_merged() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "UPDATE intents SET state = 'pending', merged_into = NULL WHERE merged_into = OLD.id; RETURN OLD; END $$; "
            "CREATE TRIGGER intents_detach_merged BEFORE DELETE ON intents FOR EACH ROW "
            "EXECUTE FUNCTION intents_detach_merged(); "
            "INSERT INTO schema_migrations (name) VALUES ('intent_merges') ON CONFLICT DO NOTHING")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)  # the marker alone does not hide it
        await connection.ensure_schema(pool)
        names = {row["tgname"] for row in await pool.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'intents'::regclass AND NOT tgisinternal")}
        assert "A_intents_detach_merged" in names and "intents_detach_merged" not in names
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)

    run_db(check, monkeypatch, timeout=30)
