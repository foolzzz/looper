"""``db.memory``: making a candidate from a seed, storing a revision, the transitions code may make, deleting with a
tombstone, and the reads (memory-write-path RFC, sections 3.1, 4.2, 5 and D23)."""

from datetime import timedelta

import pytest

from looper.agents import memory_model as model
from looper.agents.memory_model import EvidenceKind
from looper.db import memory as db_memory
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
Evidence = db_memory.Evidence


def test_a_seed_becomes_a_drafting_candidate_with_its_sources_and_a_propose_event(monkeypatch):
    async def check(pool):
        action = await sup.manager_action(pool)
        candidate = await sup.candidate(pool, evidence=[sup.operator_turn(action)], manager_id="manager")
        found = await db_memory.get(candidate, db=pool)
        row = found["candidate"]
        assert (row["state"], row["destination"], row["source_kind"], row["source_class"], row["revision"]) == (
            "drafting", "vault", "manager_turn", "operator", 0)
        assert row["fingerprint"] == model.fingerprint("preference", "Weekly reports: conclusion first")
        assert row["keywords"] == ["weekly", "reports"] and row["manager_id"] == "manager"
        assert [(s["kind"], s["source_class"], s["quote"], s["chat"]) for s in found["sources"]] == [
            ("manager_turn", "operator", "from now on, conclusion first", "ou_operator_synthetic")]
        assert [(e["action"], e["actor_kind"], e["via"]) for e in found["events"]] == [("propose", "manager", "lark")]
        assert found["revision"] is None

    run_db(check, monkeypatch, timeout=20)


def test_a_kind_refuses_sources_it_does_not_accept_judged_by_the_least_trusted_one(monkeypatch):
    async def check(pool):
        lead = Evidence(EvidenceKind.LEAD, lead_id=await sup.lead(pool))
        journal = Evidence(EvidenceKind.JOURNAL, journal_id=await sup.journal_entry(pool))
        turn = sup.operator_turn(await sup.manager_action(pool))
        for kind, evidence in (("preference", [lead]), ("feedback", [turn, journal]), ("decision", [journal]),
                               ("lesson", [lead])):
            with pytest.raises(db_memory.MemoryRefused) as refused:
                await sup.candidate(pool, kind=kind, evidence=evidence)
            assert refused.value.code == "class"
        mixed = await sup.candidate(pool, kind="decision", statement="Ship Friday", evidence=[turn, lead])
        assert (await db_memory.get(mixed, db=pool))["candidate"]["source_class"] == "external"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("fields", [
    {"statement": "  "}, {"statement": "x" * 301}, {"keywords": []}, {"keywords": ["a"] * 7}, {"keywords": [" "]},
    {"evidence": []},
])
def test_a_seed_that_does_not_hold_is_refused(monkeypatch, fields):
    async def check(pool):
        evidence = fields.pop("evidence", [sup.operator_turn(await sup.manager_action(pool))])
        async with db_memory.transaction(pool) as conn:
            with pytest.raises(db_memory.MemoryRefused) as refused:
                await db_memory.create(conn, **{"kind": "preference", "statement": "x", "keywords": ["k"],
                                                "evidence": evidence, "actor": "manager", "via": "lark", **fields})
        assert refused.value.code == "invalid"

    run_db(check, monkeypatch, timeout=20)


def test_only_an_operator_turn_carries_a_quote(monkeypatch):
    async def check(pool):
        journal = await sup.journal_entry(pool)
        with pytest.raises(db_memory.MemoryRefused):
            await sup.candidate(pool, kind="lesson", evidence=[Evidence(EvidenceKind.JOURNAL, journal_id=journal,
                                                                        quote="the operator said")])

    run_db(check, monkeypatch, timeout=20)


def test_a_revision_makes_the_candidate_pending_under_the_hash_of_exactly_its_changes(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        row = await sup.revise(pool, candidate)
        assert (row["state"], row["revision"], row["note_path"], row["title"]) == (
            "pending", 1, "mem/prefs/weekly.md", "Weekly reports: conclusion first")
        assert row["content_hash"] == model.content_hash(candidate, sup.SUBJECT, [sup.NOTE, sup.INDEX])
        assert row["expires_at"] == sup.NOW + timedelta(days=7)
        found = await db_memory.get(candidate, db=pool)
        assert found["revision"]["changes"] == [sup.NOTE, sup.INDEX]
        assert found["revision"]["diff_stats"] == {"mem/prefs/weekly.md": {"added": 4, "removed": 0}}
        assert [(e["action"], e["revision"], e["content_hash"]) for e in found["events"]][-1] == (
            "draft", 1, row["content_hash"])

    run_db(check, monkeypatch, timeout=20)


def test_a_new_revision_drops_the_old_confirmation_hash_and_waits_again(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        first = await sup.revise(pool, candidate)
        edited = await sup.revise(pool, candidate, changes=[{**sup.NOTE, "content": "Conclusion first, always.\n"}],
                                  action=model.Action.EDIT, actor=model.Actor.USER, via=model.Via.DASHBOARD,
                                  made_by=model.MadeBy.OPERATOR)
        assert edited["revision"] == 2 and edited["content_hash"] != first["content_hash"]
        with pytest.raises(db_memory.MemoryRefused) as refused:  # the operator saw the first one
            await sup.move(pool, candidate, "confirmed", "confirm", actor="user", via="dashboard",
                           content_hash=first["content_hash"])
        assert refused.value.code == "stale"
        await sup.move(pool, candidate, "confirmed", "confirm", actor="user", via="lark",
                       content_hash=edited["content_hash"])
        with pytest.raises(db_memory.MemoryRefused) as refused:  # a confirmed candidate is not revised in place
            await sup.revise(pool, candidate, action=model.Action.EDIT, actor=model.Actor.USER,
                             via=model.Via.DASHBOARD, made_by=model.MadeBy.OPERATOR)
        assert refused.value.code == "state"

    run_db(check, monkeypatch, timeout=20)


def test_who_may_store_a_revision_and_with_what(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        await sup.revise(pool, candidate)
        with pytest.raises(db_memory.MemoryRefused) as refused:  # only the operator edits
            await sup.revise(pool, candidate, action=model.Action.EDIT, actor=model.Actor.MANAGER,
                             via=model.Via.LARK, made_by=model.MadeBy.OPERATOR)
        assert refused.value.code == "invalid"
        with pytest.raises(db_memory.MemoryRefused) as refused:
            await sup.revise(pool, candidate, action=model.Action.REDRAFT,
                             changes=[{**sup.NOTE, "path": "../mine/a.md"}])
        assert refused.value.code == "invalid"
        other = await sup.candidate(pool, statement="Another note")
        with pytest.raises(db_memory.MemoryRefused) as refused:  # one open candidate per note
            await sup.revise(pool, other)
        assert refused.value.code == "busy"
        assert (await sup.revise(pool, other, note_path="mem/prefs/other.md"))["state"] == "pending"

    run_db(check, monkeypatch, timeout=20)


def test_a_confirmation_is_the_operators_from_the_dashboard_or_a_direct_chat(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        row = await sup.revise(pool, candidate)
        for actor, via in (("manager", "lark"), ("system", "system"), ("user", "cli")):
            with pytest.raises(db_memory.MemoryRefused) as refused:
                await sup.move(pool, candidate, "confirmed", "confirm", actor=actor, via=via,
                               content_hash=row["content_hash"])
            assert refused.value.code == "invalid"
        done = await sup.move(pool, candidate, "confirmed", "confirm", actor="user", via="telegram",
                              content_hash=row["content_hash"], channel_id="telegram", chat="42",
                              message_id="m-1")
        event = await pool.fetchrow("SELECT * FROM memory_candidate_events WHERE id = $1",
                                    done["confirmed_event_id"])
        assert (event["action"], event["actor_kind"], event["via"], event["content_hash"], event["message_id"]) == (
            "confirm", "user", "telegram", row["content_hash"], "m-1")
        assert (done["state"], done["confirmed_hash"], done["expires_at"]) == ("confirmed", row["content_hash"], None)

    run_db(check, monkeypatch, timeout=20)


def test_the_write_path_keeps_the_confirmation_and_counts_its_attempts(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        confirmed = await sup.confirmed(pool, candidate)
        claimed = await sup.move(pool, candidate, "writing", "claim")
        assert (claimed["attempts"], claimed["confirmed_event_id"]) == (1, confirmed["confirmed_event_id"])
        recovered = await sup.move(pool, candidate, "confirmed", "recover")  # cut off by a restart
        assert recovered["attempts"] == 1
        await sup.move(pool, candidate, "writing", "claim")
        blocked = await sup.move(pool, candidate, "blocked", "block", reason="dirty_target",
                                 note="the target note has uncommitted changes")
        assert (blocked["attempts"], blocked["reason"], blocked["confirmed_hash"]) == (
            2, "dirty_target", confirmed["content_hash"])
        retried = await sup.move(pool, candidate, "confirmed", "retry", actor="user", via="cli")
        assert (retried["attempts"], retried["reason"]) == (0, None)
        await sup.move(pool, candidate, "writing", "claim")
        written = await sup.move(pool, candidate, "written", "write", commit_sha="c" * 40)
        assert (written["state"], written["commit_sha"], written["written_at"]) == ("written", "c" * 40, sup.NOW)
        with pytest.raises(db_memory.MemoryRefused):  # written is final
            await sup.move(pool, candidate, "rejected", "reject", actor="user", via="dashboard")

    run_db(check, monkeypatch, timeout=20)


def test_leaving_the_confirmed_states_drops_the_confirmation(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        await sup.confirmed(pool, candidate)
        await sup.move(pool, candidate, "writing", "claim")
        conflict = await sup.move(pool, candidate, "conflict", "conflict", reason="base_changed")
        assert (conflict["confirmed_event_id"], conflict["confirmed_hash"], conflict["attempts"]) == (None, None, 0)
        redrafting = await sup.move(pool, candidate, "drafting", "redraft")
        assert (redrafting["state"], redrafting["reason"]) == ("drafting", None)
        assert (await sup.revise(pool, candidate, action=model.Action.DRAFT))["revision"] == 2

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("to,action,fields", [
    ("dropped", "drop", {}),                                         # a dropped one says why
    ("dropped", "drop", {"reason": "dirty_target"}),                 # with a reason of its own state
    ("drafting", "redraft", {"reason": "duplicate"}),                # and no other state has one
    ("expired", "expire", {"pending_days": 7}),                      # pending_days only with pending
    ("rejected", "reject", {"commit_sha": "c" * 40}),                # a commit only with written
    ("rejected", "reject", {"merged_into": 1}),                      # merged_into only with a same_as drop
])
def test_a_transition_carries_exactly_what_its_state_needs(monkeypatch, to, action, fields):
    async def check(pool):
        candidate = await sup.candidate(pool)
        await sup.revise(pool, candidate)
        if to == "dropped":
            await sup.move(pool, candidate, "drafting", "redraft")
        with pytest.raises(db_memory.MemoryRefused) as refused:
            await sup.move(pool, candidate, to, action, actor="user", via="dashboard", **fields)
        assert refused.value.code == "invalid"

    run_db(check, monkeypatch, timeout=20)


def test_a_seed_dropped_as_the_same_as_another_points_at_it(monkeypatch):
    async def check(pool):
        first = await sup.candidate(pool)
        second = await sup.candidate(pool, statement="Weekly reports, conclusion first!")
        row = await sup.move(pool, second, "dropped", "merge", reason="same_as", merged_into=first)
        assert (row["state"], row["reason"], row["merged_into"]) == ("dropped", "same_as", first)
        with pytest.raises(db_memory.MemoryRefused):  # only a drafting failure is drafted again
            await sup.move(pool, second, "drafting", "redraft", actor="user", via="dashboard")
        failed = await sup.candidate(pool, statement="Something else")
        await sup.move(pool, failed, "dropped", "drop", reason="draft_failed")
        assert (await sup.move(pool, failed, "drafting", "redraft", actor="user", via="dashboard"))["state"] == (
            "drafting")

    run_db(check, monkeypatch, timeout=20)


def test_pending_expires_and_is_revived(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        await sup.revise(pool, candidate)
        expired = await sup.move(pool, candidate, "expired", "expire")
        assert (expired["state"], expired["expires_at"]) == ("expired", None)
        revived = await sup.move(pool, candidate, "pending", "revive", actor="user", via="dashboard", pending_days=3)
        assert revived["expires_at"] == sup.NOW + timedelta(days=3)

    run_db(check, monkeypatch, timeout=20)


def test_a_lesson_row_has_no_vault_transitions(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-mem', 'Synthetic project')")
        lesson = await pool.fetchval(
            "INSERT INTO memory_candidates (destination, kind, source_kind, source_class, statement, fingerprint, "
            "project_id, valid_until, state) VALUES ('looper', 'lesson', 'task', 'agent', 'x', $1, 'proj-mem', now(), "
            "'in_effect') RETURNING id", "a" * 64)
        with pytest.raises(db_memory.MemoryRefused) as refused:
            await sup.move(pool, lesson, "rejected", "reject", actor="user", via="dashboard")
        assert refused.value.code == "state"
        async with db_memory.transaction(pool) as conn:
            with pytest.raises(db_memory.MemoryRefused):  # one in effect is revoked first, then deleted
                await db_memory.delete(conn, lesson, via="dashboard")

    run_db(check, monkeypatch, timeout=20)


def test_deleting_leaves_only_a_tombstone_that_stops_all_but_the_operator(monkeypatch):
    async def check(pool):
        journal = Evidence(EvidenceKind.JOURNAL, journal_id=await sup.journal_entry(pool))
        lesson = await sup.candidate(pool, kind="lesson", statement="Run make gen first", evidence=[journal])
        await sup.revise(pool, lesson, note_path=None)
        async with db_memory.transaction(pool) as conn:
            assert await db_memory.delete(conn, lesson, via="cli")
            assert not await db_memory.delete(conn, lesson, via="cli")
        for table in ("memory_candidates", "memory_candidate_revisions", "memory_candidate_events",
                      "memory_candidate_sources"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table
        assert dict(await pool.fetchrow("SELECT kind, destination, deleted_via FROM memory_tombstones")) == {
            "kind": "lesson", "destination": "vault", "deleted_via": "cli"}
        # The same lesson from a task again is refused; said by the operator, it is theirs and clears the tombstone.
        with pytest.raises(db_memory.MemoryRefused) as refused:
            await sup.candidate(pool, kind="lesson", statement="run  MAKE gen first.", evidence=[journal])
        assert refused.value.code == "tombstoned"
        assert await sup.candidate(pool, kind="lesson", statement="Run make gen first")
        assert await db_memory.tombstone_count(db=pool) == 0

    run_db(check, monkeypatch, timeout=20)


def test_a_candidate_being_written_cannot_be_deleted_and_tombstones_can_be_cleared(monkeypatch):
    async def check(pool):
        candidate = await sup.candidate(pool)
        await sup.confirmed(pool, candidate)
        await sup.move(pool, candidate, "writing", "claim")
        async with db_memory.transaction(pool) as conn:
            with pytest.raises(db_memory.MemoryRefused) as refused:
                await db_memory.delete(conn, candidate, via="dashboard")
            assert refused.value.code == "state"
            with pytest.raises(ValueError):  # deleting is the dashboard's or the CLI's
                await db_memory.delete(conn, candidate, via="lark")
        await sup.move(pool, candidate, "blocked", "block", reason="git_failed")
        async with db_memory.transaction(pool) as conn:
            assert await db_memory.delete(conn, candidate, via="dashboard")
        assert await db_memory.clear_tombstones(db=pool) == 1
        assert await db_memory.tombstone_count(db=pool) == 0

    run_db(check, monkeypatch, timeout=20)


def test_writes_run_in_the_callers_transaction(monkeypatch):
    async def check(pool):
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="transaction"):
                await db_memory.create(conn, kind="preference", statement="x", keywords=["k"], evidence=[],
                                       actor="manager", via="lark")

    run_db(check, monkeypatch, timeout=20)


def test_the_list_filters_by_state_and_kind_newest_first_and_pages(monkeypatch):
    async def check(pool):
        first = await sup.candidate(pool, statement="First")
        second = await sup.candidate(pool, kind="decision", statement="Second")
        third = await sup.candidate(pool, statement="Third")
        await sup.revise(pool, third)
        await sup.move(pool, third, "rejected", "reject", actor="user", via="dashboard", note="not mine")
        rows, more = await db_memory.list_candidates(db=pool)
        assert [row["id"] for row in rows] == [third, second, first] and not more
        assert rows[0]["sources_total"] == 1
        rows, more = await db_memory.list_candidates(states=["drafting"], limit=1, db=pool)
        assert [row["id"] for row in rows] == [second] and more
        rows, _ = await db_memory.list_candidates(states=["drafting"], before_id=second, db=pool)
        assert [row["id"] for row in rows] == [first]
        rows, _ = await db_memory.list_candidates(kind="decision", db=pool)
        assert [row["id"] for row in rows] == [second]
        assert await db_memory.get(999_999, db=pool) is None
        with pytest.raises(ValueError):
            await db_memory.list_candidates(limit=0, db=pool)

    run_db(check, monkeypatch, timeout=20)
