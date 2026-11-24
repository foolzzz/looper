"""Exact fingerprints merge before the draft budget or a model call is needed."""

import json

import pytest

from looper.agents import memory_model as model
from looper.agents.memory_draft_service import MemoryDraftService
from looper.agents.model_call import CallFailure, Execution
from looper.db import memory as store
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class Backend:
    def __init__(self, *, fail=False):
        self.calls, self.fail = 0, fail

    async def run(self, request):
        self.calls += 1
        return Execution(None if self.fail else json.dumps({
            "decision": "duplicate", "of": "X1", "reason": "Already indexed", "instructions_seen": []}),
            cost_usd=.01, failure=CallFailure.TIMEOUT if self.fail else None)


@pytest.mark.parametrize("target_state", ["pending", "confirmed", "writing", "blocked", "conflict"])
def test_exact_fingerprint_duplicate_merges_without_a_model_run(monkeypatch, tmp_path, target_state):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        target = await sup.candidate(pool)
        row = await sup.revise_current(pool, target, settings)
        if target_state in {"confirmed", "writing", "blocked", "conflict"}:
            await sup.move(pool, target, "confirmed", "confirm", actor="user", via="dashboard",
                           content_hash=row["content_hash"])
        if target_state in {"writing", "blocked", "conflict"}:
            await sup.move(pool, target, "writing", "claim")
        if target_state == "blocked":
            await sup.move(pool, target, "blocked", "block", reason="dirty_target")
        if target_state == "conflict":
            await sup.move(pool, target, "conflict", "conflict", reason="base_changed")
        cid = await sup.candidate(pool, statement="  WEEKLY reports，conclusion first。")
        await pool.execute("UPDATE memory_candidates SET flagged=true WHERE id=$1", cid)
        # Exact duplicates do not need a lease or available model budget.
        await pool.execute("INSERT INTO llm_records(agent_id,run_kind,status,cost_usd) "
                           "VALUES('synthetic-draft','memory_draft','completed',1)")
        backend = Backend()
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        found = await store.get(cid, db=pool)
        assert (found["candidate"]["state"], found["candidate"]["reason"], found["candidate"]["merged_into"]) == (
            "dropped", "same_as", target)
        other = await store.get(target, db=pool)
        assert len(other["sources"]) == 2 and other["candidate"]["flagged"]
        assert other["candidate"]["state"] == target_state
        assert backend.calls == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_draft_lease") == 0

    run_db(check, monkeypatch)


def test_duplicate_whose_class_the_kind_refuses_is_dropped_as_duplicate(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        target = await sup.candidate(pool)
        await sup.revise_current(pool, target, settings)
        lead_id = await sup.lead(pool)
        cid = await sup.candidate(pool, kind="fact", evidence=[store.Evidence(model.EvidenceKind.LEAD, lead_id=lead_id)],
                                  untrusted=True)
        # Exercise the merge boundary even for a row imported without the seed API's class check.
        await pool.execute("UPDATE memory_candidates SET kind='preference',fingerprint=$2 WHERE id=$1", cid,
                           model.fingerprint("preference", "Weekly reports: conclusion first"))
        backend = Backend()
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert row["state"] == "dropped" and row["reason"] == "duplicate" and row["merged_into"] is None
        other = await store.get(target, db=pool)
        assert len(other["sources"]) == 1 and other["candidate"]["source_class"] == "operator"
        assert not other["candidate"]["untrusted"] and backend.calls == 0

    run_db(check, monkeypatch)


def test_same_statement_of_another_kind_is_drafted(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        raw = settings.to_json()
        raw["targets"]["feedback"] = raw["targets"]["preference"]
        await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
        target = await sup.candidate(pool)
        await sup.revise_current(pool, target, settings)
        cid = await sup.candidate(pool, kind="feedback")
        backend = Backend()
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert backend.calls == 1 and row["merged_into"] is None

    run_db(check, monkeypatch)


def test_newer_drafting_twin_merges_into_the_older(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await sup.configure_write(repo, pool)
        older = await sup.candidate(pool)
        newer = await sup.candidate(pool, statement="weekly reports conclusion first")
        # The newer candidate is claimed first, as can happen after a pending proposal is reseeded.
        await pool.execute("UPDATE memory_candidates SET updated_at=updated_at-interval '1 hour' WHERE id=$1", newer)
        backend = Backend()
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        row = (await store.get(newer, db=pool))["candidate"]
        assert row["merged_into"] == older and row["reason"] == "same_as"
        assert (await store.get(older, db=pool))["candidate"]["state"] == "drafting"
        assert len((await store.get(older, db=pool))["sources"]) == 2 and backend.calls == 0

    run_db(check, monkeypatch)


def test_reseeded_candidate_gets_fresh_draft_attempts(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        cid = await sup.candidate(pool)
        backend = Backend(fail=True)
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        for _ in range(2):
            assert await service.run_once()
        await sup.revise_current(pool, cid, settings)
        evidence = [sup.operator_turn(await sup.manager_action(pool))]
        async with store.transaction(pool) as conn:
            await store.reseed(conn, cid, statement="Weekly reports start with a summary", why="Easy to skim",
                               keywords=["weekly"], evidence=evidence, actor=model.Actor.MANAGER,
                               via=model.Via.DASHBOARD, now=sup.NOW)
        for expected in ("drafting", "drafting", "dropped"):
            assert await service.run_once()
            assert (await store.get(cid, db=pool))["candidate"]["state"] == expected
        assert backend.calls == 5
        assert (await store.get(cid, db=pool))["candidate"]["reason"] == "draft_failed"

    run_db(check, monkeypatch)
