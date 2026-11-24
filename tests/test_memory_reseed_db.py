"""A user's pending seed may be corrected without granting the redraft operation new states."""

import pytest

from looper.agents import memory_model as model
from looper.db import memory as store
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_reseed_keeps_history_adds_evidence_and_clears_the_new_fingerprints_tombstone(monkeypatch):
    async def check(pool):
        cid = await sup.candidate(pool)
        before = await sup.revise(pool, cid)
        statement = "  Weekly reports: summary first  "
        fingerprint = model.fingerprint("preference", statement)
        await pool.execute("INSERT INTO memory_tombstones(fingerprint,kind,destination,deleted_via) "
                           "VALUES($1,'preference','vault','dashboard')", fingerprint)
        aid = await sup.manager_action(pool)
        async with store.transaction(pool) as conn:
            row = await store.reseed(conn, cid, statement=statement, why="  easier to skim  ", keywords=[" weekly "],
                                     evidence=[sup.operator_turn(aid, "summary first")], actor=model.Actor.MANAGER,
                                     via=model.Via.DASHBOARD, now=sup.NOW)
        found = await store.get(cid, db=pool)
        assert row["statement"] == statement.strip() and row["why"] == "easier to skim"
        assert row["keywords"] == ["weekly"] and row["fingerprint"] == fingerprint
        assert row["state"] == "drafting" and row["expires_at"] is None
        assert row["revision"] == before["revision"] and found["revision"]["content_hash"] == before["content_hash"]
        assert len(found["sources"]) == 2 and found["sources"][-1]["manager_action_id"] == aid
        assert found["events"][-1]["action"] == "propose" and found["events"][-1]["actor_kind"] == "manager"
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 0
        with pytest.raises(model.TransitionRefused):
            model.check_transition("pending", "drafting", "redraft")

    run_db(check, monkeypatch)


def test_operator_reseed_clears_tombstone_even_when_existing_project_sources_include_a_task(monkeypatch):
    async def check(pool):
        cid = await sup.candidate(pool, kind="project")
        await sup.revise(pool, cid)
        journal_id = await sup.journal_entry(pool)
        await pool.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,journal_id) "
                           "VALUES($1,'journal',$2)", cid, journal_id)
        await pool.execute("UPDATE memory_candidates SET source_class='agent' WHERE id=$1", cid)
        fingerprint = model.fingerprint("project", "Build the project with make gen")
        await pool.execute("INSERT INTO memory_tombstones(fingerprint,kind,destination,deleted_via) "
                           "VALUES($1,'project','vault','dashboard')", fingerprint)
        evidence = [sup.operator_turn(await sup.manager_action(pool), "Build the project with make gen")]
        async with store.transaction(pool) as conn:
            row = await store.reseed(conn, cid, statement="Build the project with make gen", why="", keywords=["build"],
                                     evidence=evidence, actor=model.Actor.MANAGER, via=model.Via.DASHBOARD, now=sup.NOW)
        assert row["state"] == "drafting" and row["source_class"] == "agent"
        assert len((await store.get(cid, db=pool))["sources"]) == 3
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones WHERE fingerprint=$1", fingerprint) == 0

    run_db(check, monkeypatch)


@pytest.mark.parametrize("variant,code", [
    ("drafting", "state"), ("confirmed", "state"), ("untrusted", "invalid"), ("lead", "invalid"),
    ("new_lead", "invalid"), ("mixed_class", "class"), ("empty", "invalid"), ("keywords", "invalid"),
    ("quote", "invalid"),
])
def test_reseed_refuses_invalid_state_sources_and_content_without_changes(monkeypatch, variant, code):
    async def check(pool):
        cid = await sup.candidate(pool)
        if variant != "drafting":
            await sup.revise(pool, cid)
        if variant == "confirmed":
            row = (await store.get(cid, db=pool))["candidate"]
            await sup.move(pool, cid, "confirmed", "confirm", actor="user", via="dashboard",
                           content_hash=row["content_hash"])
        if variant in {"untrusted", "lead"}:
            await pool.execute("UPDATE memory_candidates SET untrusted=$2,source_kind=$3 WHERE id=$1", cid,
                               variant == "untrusted", "lead" if variant == "lead" else "manager_turn")
        lead_id = await sup.lead(pool) if variant in {"new_lead", "mixed_class"} else None
        if variant == "mixed_class":
            await pool.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,lead_id) VALUES($1,'lead',$2)",
                               cid, lead_id)
        evidence = [store.Evidence(model.EvidenceKind.LEAD, lead_id=lead_id)] if variant == "new_lead" else [
            sup.operator_turn(await sup.manager_action(pool), "x" * 501 if variant == "quote" else "summary first")]
        before = await store.get(cid, db=pool)
        with pytest.raises(store.MemoryRefused) as caught:
            async with store.transaction(pool) as conn:
                await store.reseed(conn, cid, statement="" if variant == "empty" else "Summary first", why="",
                                   keywords=[] if variant == "keywords" else ["summary"], evidence=evidence,
                                   actor=model.Actor.MANAGER, via=model.Via.DASHBOARD, now=sup.NOW)
        assert caught.value.code == code
        assert await store.get(cid, db=pool) == before

    run_db(check, monkeypatch)
