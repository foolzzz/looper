"""The memory candidate tables (memory-write-path RFC, section 4; memory-tiers RFC, section 4.1): what the schema itself
guarantees, whatever code writes — a confirmation is the operator's and of the current content, a lesson's states stay
a lesson's, a deleted source takes what was made from it — and the upgrade of a database without the tables."""

import re

import asyncpg
import pytest

from looper.agents import memory_model as model
from looper.db import connection
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SHA = "a" * 64
OTHER_SHA = "b" * 64


def _values(definition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'::text", definition))


def test_the_checks_list_exactly_the_code_vocabulary(monkeypatch):
    async def check(pool):
        rows = await pool.fetch("SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def "
                                "FROM pg_constraint WHERE conrelid::regclass::text LIKE 'memory_%' AND contype = 'c'")
        found = {row["conname"]: _values(row["def"]) for row in rows}
        assert found["memory_candidates_kind_check"] == set(model.Kind)
        assert found["memory_candidates_state_check"] == set(model.State)
        assert found["memory_candidates_reason_check"] == set(model.Reason)
        assert found["memory_candidates_source_class_check"] == set(model.SourceClass)
        assert found["memory_candidates_source_kind_check"] == set(model.SourceKind)
        assert found["memory_candidates_destination_check"] == set(model.Destination)
        assert found["memory_candidate_events_action_check"] == set(model.Action)
        assert found["memory_candidate_events_actor_kind_check"] == set(model.Actor)
        assert found["memory_candidate_events_via_check"] == set(model.Via)
        assert found["memory_candidate_revisions_made_by_check"] == set(model.MadeBy)
        assert found["memory_candidate_sources_kind_check"] == set(model.EvidenceKind)
        assert found["memory_events_user"] - {"user"} == set(model.USER_ACTIONS)

    run_db(check, monkeypatch, timeout=20)


async def _row(pool, **columns):
    values = {"kind": "preference", "source_kind": "manager_turn", "source_class": "operator", "statement": "x",
              "fingerprint": SHA, **columns}
    names = list(values)
    return await pool.fetchval(
        f"INSERT INTO memory_candidates ({', '.join(names)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(names)))}) "
        "RETURNING id", *values.values())


async def _pending(pool):
    """A pending candidate with content SHA, made by raw SQL."""
    return await _row(pool, state="pending", revision=1, content_hash=SHA, expires_at=sup.NOW)


async def _event(pool, candidate_id, *, action="confirm", actor="user", via="dashboard", content_hash=SHA):
    return await pool.fetchval(
        "INSERT INTO memory_candidate_events (candidate_id, action, actor_kind, via, content_hash) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id", candidate_id, action, actor, via, content_hash)


_CONFIRM = ("UPDATE memory_candidates SET state = 'confirmed', expires_at = NULL, confirmed_event_id = $2, "
            "confirmed_actor = 'user', confirmed_hash = $3 WHERE id = $1")


def test_a_confirmed_candidate_points_at_the_operators_confirmation_of_its_current_content(monkeypatch):
    async def check(pool):
        candidate = await _pending(pool)
        good = await _event(pool, candidate)
        await pool.execute(_CONFIRM, candidate, good, SHA)  # the positive control
        await pool.execute("UPDATE memory_candidates SET state = 'pending', expires_at = now(), "
                           "confirmed_event_id = NULL, confirmed_actor = NULL, confirmed_hash = NULL WHERE id = $1",
                           candidate)
        # Another content's confirmation, or a hash the event did not confirm.
        other = await _event(pool, candidate, content_hash=OTHER_SHA)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(_CONFIRM, candidate, other, OTHER_SHA)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute(_CONFIRM, candidate, other, SHA)
        # A Manager's event cannot be a confirmation, nor be one at all, nor a confirmation come from the CLI.
        with pytest.raises(asyncpg.CheckViolationError):
            await _event(pool, candidate, actor="manager")
        with pytest.raises(asyncpg.CheckViolationError):
            await _event(pool, candidate, via="cli")
        with pytest.raises(asyncpg.CheckViolationError):
            await _event(pool, candidate, content_hash=None)
        drafted = await _event(pool, candidate, action="draft", actor="system", via="system")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute(_CONFIRM, candidate, drafted, SHA)
        # Confirmed with no confirmation, or a new content kept under the old confirmation.
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE memory_candidates SET state = 'confirmed', expires_at = NULL WHERE id = $1",
                               candidate)
        await pool.execute(_CONFIRM, candidate, good, SHA)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE memory_candidates SET content_hash = $2 WHERE id = $1", candidate, OTHER_SHA)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("columns", [
    {"state": "pending"},                                                    # pending with no content
    {"state": "pending", "revision": 1, "content_hash": SHA},               # pending with no expiry
    {"state": "dropped"},                                                    # dropped without a reason
    {"reason": "duplicate"},                                                 # a reason for a drafting one
    {"state": "dropped", "reason": "duplicate", "merged_into": None, "commit_sha": "c" * 40},  # a commit, unwritten
    {"state": "written", "revision": 1, "content_hash": SHA},               # written with no commit or confirmation
    {"state": "drafting", "purged_at": sup.NOW},                             # purged before it was written
    {"statement": " "},                                                      # no text, not purged
    {"statement": "x" * 301},
    {"keywords": ["a", "b", "c", "d", "e", "f", "g"]},
    {"fingerprint": "not-a-sha"},
    {"state": "in_effect"},                                                  # a lesson's state on a vault row
    {"valid_until": sup.NOW},                                                # a validity on a vault row
])
def test_a_vault_candidate_state_needs_what_goes_with_it(monkeypatch, columns):
    async def check(pool):
        await _row(pool)  # the positive control
        with pytest.raises(asyncpg.CheckViolationError):
            await _row(pool, **columns)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("columns", [
    {"state": "pending", "revision": 1, "content_hash": SHA, "expires_at": sup.NOW},  # a vault state
    {"state": "in_effect", "source_class": "external"},     # nothing from outside takes effect
    {"state": "in_effect", "source_class": "system"},
    {"state": "in_effect", "project_id": None},             # a lesson belongs to a project
    {"state": "in_effect", "valid_until": None},            # and has a validity
    {"state": "in_effect", "kind": "preference"},           # and is a lesson
    {"state": "in_effect", "note_path": "mem/a.md"},
    {"state": "held"},                                      # held without a reason
])
def test_a_lesson_row_keeps_to_its_own_lifecycle(monkeypatch, columns):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-mem', 'Synthetic project')")
        lesson = {"destination": "looper", "kind": "lesson", "source_kind": "task", "source_class": "agent",
                  "project_id": "proj-mem", "valid_until": sup.NOW, "state": "in_effect"}
        await _row(pool, **lesson)  # the positive control
        await _row(pool, **{**lesson, "state": "held", "reason": "external_source", "source_class": "external"})
        with pytest.raises(asyncpg.CheckViolationError):
            await _row(pool, **{**lesson, **columns})

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("event", [
    {"action": "edit", "actor": "manager", "via": "lark"},       # only the operator edits, rejects, revives, retries
    {"action": "retry", "actor": "system", "via": "system"},
    {"action": "revoke", "actor": "planner", "via": "system"},
    {"action": "hold", "actor": "user", "via": "dashboard"},     # only code holds a lesson or recovers a write
    {"action": "recover", "actor": "user", "via": "dashboard"},
    {"action": "activate", "actor": "manager", "via": "lark"},   # activation is code's or the operator's, seen
    {"action": "activate", "actor": "user", "via": "cli"},
])
def test_who_may_record_an_event(monkeypatch, event):
    async def check(pool):
        candidate = await _row(pool)
        await _event(pool, candidate, action="activate", actor="system", via="system", content_hash=None)
        await _event(pool, candidate, action="retry", actor="user", via="cli", content_hash=None)
        with pytest.raises(asyncpg.CheckViolationError):
            await _event(pool, candidate, content_hash=None, **event)

    run_db(check, monkeypatch, timeout=20)


def test_a_source_names_exactly_its_reference_and_its_class_is_derived(monkeypatch):
    async def check(pool):
        candidate = await _row(pool)
        action = await sup.manager_action(pool)
        journal = await sup.journal_entry(pool)
        lead = await sup.lead(pool)
        insert = ("INSERT INTO memory_candidate_sources (candidate_id, kind, manager_action_id, journal_id, lead_id, "
                  "quote) VALUES ($1, $2, $3, $4, $5, $6)")
        await pool.execute(insert, candidate, "manager_turn", action, None, None, "said so")
        await pool.execute(insert, candidate, "journal", None, journal, None, None)
        await pool.execute(insert, candidate, "lead", None, None, lead, None)
        assert [r["source_class"] for r in await pool.fetch(
            "SELECT source_class FROM memory_candidate_sources ORDER BY id")] == ["operator", "agent", "external"]
        for kind, refs, quote in (("journal", (action, None, None), None), ("lead", (None, journal, None), None),
                                  ("journal", (None, journal, lead), None), ("lead", (None, None, lead), "q")):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(insert, candidate, kind, *refs, quote)

    run_db(check, monkeypatch, timeout=20)


def test_a_deleted_source_takes_an_unwritten_candidate_and_leaves_only_the_written_ones_metadata(monkeypatch):
    async def check(pool):
        action, other_action = await sup.manager_action(pool), await sup.manager_action(pool)
        pending = await sup.candidate(pool, evidence=[sup.operator_turn(action)])
        await sup.revise(pool, pending, note_path=None)
        kept = await sup.candidate(pool, statement="Kept", evidence=[sup.operator_turn(other_action)])
        written = await sup.candidate(pool, statement="Written", evidence=[sup.operator_turn(action)])
        row = await sup.confirmed(pool, written)
        await sup.move(pool, written, "writing", "claim")
        await sup.move(pool, written, "written", "write", commit_sha="c" * 40)
        await pool.execute("INSERT INTO memory_posts (candidate_id, kind, revision, content_hash, complete, "
                           "channel_id, chat, text) VALUES ($1, 'push', 1, $2, true, 'lark', 'ou_x', 'the text')",
                           written, row["content_hash"])
        await pool.execute("DELETE FROM manager_actions WHERE id = $1", action)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id = $1", pending) == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_revisions WHERE candidate_id = $1",
                                   pending) == 0
        assert dict(await pool.fetchrow(
            "SELECT state, statement, why, keywords, purged_at IS NOT NULL AS purged, content_hash, commit_sha "
            "FROM memory_candidates WHERE id = $1", written)) == {
            "state": "written", "statement": "", "why": "", "keywords": [], "purged": True,
            "content_hash": row["content_hash"], "commit_sha": "c" * 40}
        assert await pool.fetchval("SELECT changes FROM memory_candidate_revisions WHERE candidate_id = $1",
                                   written) == "[]"
        assert await pool.fetchval("SELECT text FROM memory_posts WHERE candidate_id = $1", written) == ""
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id = $1", kept) == 1
        await pool.execute("DELETE FROM memory_candidates WHERE id = $1", kept)  # a candidate deleted takes its sources
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources WHERE candidate_id = $1", kept) == 0

    run_db(check, monkeypatch, timeout=30)


def test_a_deleted_task_or_lead_takes_the_candidates_made_from_it(monkeypatch):
    async def check(pool):
        journal = await sup.journal_entry(pool)
        lesson = await sup.candidate(pool, kind="lesson", statement="Run make gen first",
                                     evidence=[sup.db_memory.Evidence(model.EvidenceKind.JOURNAL, journal_id=journal)])
        lead = await sup.lead(pool)
        decision = await sup.candidate(pool, kind="decision", statement="Ship on Friday",
                                       evidence=[sup.db_memory.Evidence(model.EvidenceKind.LEAD, lead_id=lead)])
        await pool.execute("DELETE FROM tasks WHERE id = 'task-mem'")
        await pool.execute("DELETE FROM message_leads WHERE id = $1", lead)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id = ANY($1::bigint[])",
                                   [lesson, decision]) == 0

    run_db(check, monkeypatch, timeout=30)


def test_a_deleted_project_takes_its_lessons_and_unlinks_the_rest(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-mem', 'Synthetic project')")
        lesson = await _row(pool, destination="looper", kind="lesson", source_kind="task", source_class="agent",
                            project_id="proj-mem", valid_until=sup.NOW, state="in_effect")
        staged = await _row(pool, project_id="proj-mem")
        await pool.execute("DELETE FROM projects WHERE id = 'proj-mem'")
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id = $1", lesson) == 0
        assert await pool.fetchval("SELECT project_id FROM memory_candidates WHERE id = $1", staged) is None

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_tables_gets_them_on_upgrade(monkeypatch):
    async def check(pool):
        await pool.execute(sup.DROP_MEMORY)
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'memory_candidates')")
        # Other tests' schemas in this database have the same triggers: count this schema's only.
        assert await pool.fetchval("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                                   "WHERE c.relnamespace = current_schema()::regnamespace AND t.tgname IN "
                                   "('projects_memory_lessons_drop', 'memory_candidate_sources_drop')") == 2
        candidate = await sup.candidate(pool)
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id = $1", candidate) == 1

    run_db(check, monkeypatch, timeout=30)


def test_a_fresh_database_records_only_its_baseline(monkeypatch):
    async def check(pool):
        assert not await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'memory_candidates')")
        assert await pool.fetchval("SELECT to_regclass('memory_tombstones') IS NOT NULL")

    run_db(check, monkeypatch, timeout=20)
