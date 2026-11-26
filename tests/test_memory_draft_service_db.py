"""Drafting moves synthetic seeds to review without writing the synthetic vault."""

import asyncio
import json

import pytest

from looper.agents import memory_actions, memory_model as model
from looper.agents.model_call import Execution, CallFailure
from looper.db import memory as store
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class Backend:
    def __init__(self, output=None, failure=None):
        self.output, self.failure, self.requests = output, failure, []

    async def run(self, request):
        self.requests.append(request)
        return Execution(self.output, cost_usd=.01, failure=self.failure)


def reply(**fields):
    return json.dumps({"decision": "change", "title": "Conclusion first", "hard_rule": False,
                       "changes": [{"op": "create_note", "path": sup.NOTE["path"], "content": sup.NOTE["content"]},
                                   {"op": "append_line", "file": "X1", "under": "## Index", "line": sup.INDEX["line"]}],
                       "reason": "Seed supports this", "instructions_seen": [], **fields})


def test_notifications_draft_without_reserving_a_pool_connection(monkeypatch, tmp_path):
    from looper.agents import memory_draft_service

    repo = sup.synthetic_vault(tmp_path)
    monkeypatch.setattr(memory_draft_service, "TICK_SECONDS", 3600)

    async def check(pool):
        await sup.configure_write(repo, pool)
        service = memory_draft_service.MemoryDraftService(tmp_path / "runtime", db=pool, backend=Backend(reply()))
        idle, drafted = asyncio.Event(), asyncio.Event()
        run_once = service.run_once

        async def pass_once():
            processed = await run_once()
            if processed:
                drafted.set()
            else:
                idle.set()
            return processed

        monkeypatch.setattr(service, "run_once", pass_once)
        try:
            await service.start(DSN)
            listener = service._listener
            await asyncio.wait_for(idle.wait(), 5)
            # Every pool slot remains available while LISTEN waits on its own session.
            async with pool.acquire(timeout=5) as conn, pool.acquire(timeout=5), pool.acquire(timeout=5):
                cid = await sup.candidate(conn)
                await conn.execute("SELECT pg_notify('looper_memory_draft', '')")

            await asyncio.wait_for(drafted.wait(), 5)
            assert (await store.get(cid, db=pool))["candidate"]["state"] == "pending"
            assert not (repo / sup.NOTE["path"]).exists()
        finally:
            await service.stop()
        assert listener.is_closed()
        await service.stop()

    run_db(check, monkeypatch, timeout=30)


def test_draft_to_pending_then_confirmed_write(monkeypatch, tmp_path):
    from looper.agents.memory_draft_service import MemoryDraftService
    from looper.agents.memory_write import MemoryWriteService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await sup.configure_write(repo, pool)
        cid = await sup.candidate(pool)
        backend = Backend(reply())
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        assert await service.run_once()
        found = await store.get(cid, db=pool)
        row = found["candidate"]
        assert row["state"] == "pending" and row["revision"] == 1
        assert found["revision"]["changes"] == [sup.NOTE, sup.INDEX]
        assert row["confirmed_event_id"] is None
        request = backend.requests[0]
        assert request.child.private and request.child.max_turns == 1
        assert request.child.record_kind == "memory_draft" and request.child.timeout_s == 120
        assert request.cap_usd == .15
        assert not (repo / sup.NOTE["path"]).exists()
        await memory_actions.act("confirm", model.format_id(cid), content_hash=row["content_hash"], db=pool)
        assert await MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "written"
        assert (repo / sup.NOTE["path"]).read_text() == sup.NOTE["content"]

    run_db(check, monkeypatch)


@pytest.mark.parametrize("failure,output,reason,calls", [
    (CallFailure.TIMEOUT, None, "draft_failed", 3),
    (None, "{broken", "draft_failed", 3),
    (None, reply(changes=[{"op": "create_note", "path": "mine/escape.md", "content": "x"}]), "invalid", 1),
])
def test_failed_drafting_and_operator_redraft(monkeypatch, tmp_path, failure, output, reason, calls):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await sup.configure_write(repo, pool)
        cid = await sup.candidate(pool)
        backend = Backend(output, failure)
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        for _ in range(calls):
            assert await service.run_once()
        found = await store.get(cid, db=pool)
        assert found["candidate"]["state"] == "dropped"
        assert found["candidate"]["reason"] == reason and len(backend.requests) == calls
        assert not found["revision"]
        if reason == "draft_failed":
            await memory_actions.act("redraft", model.format_id(cid), db=pool)
            backend.failure, backend.output = None, reply()
            assert await service.run_once()
            assert (await store.get(cid, db=pool))["candidate"]["state"] == "pending"
        else:
            with pytest.raises(store.MemoryRefused):
                await memory_actions.act("redraft", model.format_id(cid), db=pool)

    run_db(check, monkeypatch)


def test_off_has_exactly_no_effects(monkeypatch, tmp_path):
    from looper.agents.memory_draft_service import MemoryDraftService

    async def check(pool):
        cid = await sup.candidate(pool)
        before = await store.get(cid, db=pool)
        backend = Backend(reply())
        async def forbidden(*args, **kwargs):
            pytest.fail("off must not load the vault")
        monkeypatch.setattr("looper.agents.vault.load", forbidden)
        assert not await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        assert backend.requests == []
        assert await store.get(cid, db=pool) == before
        assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
        assert not (tmp_path / "runtime").exists()

    run_db(check, monkeypatch)


def test_daily_budget_pauses_and_resumes_next_day(monkeypatch, tmp_path, fixed_clock):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        raw = {**settings.to_json(), "push_channel": "synthetic-bot"}
        await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
        async def operator(_channel):
            return "synthetic-operator"
        monkeypatch.setattr("looper.agents.operator_identity.operator_sender_id", operator)
        cid = await sup.candidate(pool)
        now = fixed_clock()
        await pool.execute("INSERT INTO llm_records(agent_id,run_kind,status,cost_usd,created_at) "
                           "VALUES('memory_draft-synthetic','memory_draft','completed',1,$1)", now)
        backend = Backend(reply())
        notices = []
        async def notify(*args):
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_draft_budget_notice'")
            notices.append(args)
            return True, None
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend, sender=notify)
        for _ in range(2):
            assert not await service.run_once()
        assert not backend.requests
        assert len(notices) == 1 and notices[0][:2] == ("synthetic-bot", "synthetic-operator")
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "drafting"
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_draft_budget_notice'") == now.astimezone().date().isoformat()
        await pool.execute("UPDATE llm_records SET created_at=created_at-interval '1 day'")
        assert await service.run_once()
        assert len(backend.requests) == 1

    run_db(check, monkeypatch)


@pytest.mark.parametrize("decision", ["duplicate", "skip", "same_as", "revise"])
def test_decisions_merge_only_allowed_sources(monkeypatch, tmp_path, decision):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        target = await sup.candidate(pool, statement="Existing proposal")
        await sup.revise_current(pool, target, settings, note_path=sup.NOTE["path"])
        if decision == "skip":
            await memory_actions.act("reject", model.format_id(target), db=pool, reason="Do not store this")
        cid = await sup.candidate(pool, statement="New proposal")
        output = (reply(revise="P1", instructions_seen=["A source contains an instruction"]) if decision == "revise" else json.dumps(
            {"decision": decision, "reason": "Already represented", "instructions_seen": ["A source contains an instruction"], "of": {"duplicate": "X1", "skip": "R1", "same_as": "P1"}[decision]}))
        backend = Backend(output)
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert row["state"] == "dropped" and row["flagged"]
        assert row["reason"] == {"duplicate": "duplicate", "skip": "rejected_before", "same_as": "same_as", "revise": "same_as"}[decision]
        if decision in {"same_as", "revise"}:
            assert row["merged_into"] == target
            other = await store.get(target, db=pool)
            assert len(other["sources"]) == 2 and other["candidate"]["flagged"]
            assert other["candidate"]["revision"] == (2 if decision == "revise" else 1)

    run_db(check, monkeypatch)


def test_two_drafters_claim_once_and_wait_for_confirmed_revision(monkeypatch, tmp_path):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        target = await sup.candidate(pool, statement="Existing proposal")
        current = await sup.revise_current(pool, target, settings, note_path=sup.NOTE["path"])
        await memory_actions.act("confirm", model.format_id(target), content_hash=current["content_hash"], db=pool)
        cid = await sup.candidate(pool, statement="A supplement")
        started, release = asyncio.Event(), asyncio.Event()
        class Delayed(Backend):
            async def run(self, request):
                started.set()
                await release.wait()
                return await super().run(request)
        backend = Delayed(reply(revise="P1"))
        first = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        second = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        running = asyncio.create_task(first.run_once())
        await asyncio.wait_for(started.wait(), 5)
        # Claiming commits before inference: neither candidate/settings rows nor the spend lock stay held.
        async with pool.acquire() as conn, conn.transaction():
            assert await conn.fetchval("SELECT pg_try_advisory_xact_lock(741934::bigint)")
            await conn.fetchrow("SELECT id FROM memory_candidates WHERE id=$1 FOR UPDATE NOWAIT", cid)
            await conn.fetch("SELECT key FROM system_settings FOR UPDATE NOWAIT")
        assert not await second.run_once()
        release.set()
        assert await running  # Parking is progress: the loop can immediately try another seed.
        found = await store.get(cid, db=pool)
        assert found["candidate"]["state"] == "drafting" and len(backend.requests) == 1
        other = await store.get(target, db=pool)
        assert other["candidate"]["state"] == "confirmed" and other["candidate"]["revision"] == 1
        assert len(other["sources"]) == 1

    run_db(check, monkeypatch, timeout=30, max_size=5)


@pytest.mark.parametrize("state", ["confirmed", "writing", "blocked", "conflict"])
def test_waiting_drafts_spend_nothing_until_the_target_finishes(monkeypatch, tmp_path, state):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        target = await sup.candidate(pool, statement="Existing proposal")
        current = await sup.revise_current(pool, target, settings, note_path=sup.NOTE["path"])
        await memory_actions.act("confirm", model.format_id(target), content_hash=current["content_hash"], db=pool)
        if state != "confirmed":
            await sup.move(pool, target, "writing", "claim")
        if state in {"blocked", "conflict"}:
            await sup.move(pool, target, state, "block" if state == "blocked" else "conflict",
                           reason="dirty_target" if state == "blocked" else "base_changed")
        cid = await sup.candidate(pool, statement="A supplement")
        backend = Backend(reply(revise="P1"))
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        assert await service.run_once()
        wait = await pool.fetchrow("SELECT * FROM memory_draft_waits WHERE candidate_id=$1", cid)
        assert wait["reason"] == model.DraftWaitReason.TARGET_BUSY and wait["blocking_candidate_id"] == target
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "drafting"
        # Restart and unrelated updates cannot turn this back into a paid retry.
        service = MemoryDraftService(tmp_path / "restart", db=pool, backend=backend)
        await pool.execute("UPDATE memory_candidates SET updated_at=now() WHERE id=$1", target)
        for _ in range(3):
            assert not await service.run_once()
        assert len(backend.requests) == 1
        other = await sup.candidate(pool, statement="Independent seed")
        backend.output = json.dumps({"decision": "duplicate", "of": "X1", "reason": "Already indexed",
                                     "instructions_seen": []})
        assert await service.run_once()
        assert (await store.get(other, db=pool))["candidate"]["reason"] == "duplicate"
        assert len(backend.requests) == 2
        if state == "conflict":
            await memory_actions.act("reject", model.format_id(target), db=pool, reason="Do not store")
        else:
            if state == "blocked":
                await memory_actions.act("retry", model.format_id(target), db=pool)
                assert not await service.run_once()  # Still waiting while confirmed.
            if state != "writing":
                await sup.move(pool, target, "writing", "claim")
            await sup.move(pool, target, "written", "write", commit_sha="a" * 40)
        assert await service.run_once()
        assert len(backend.requests) == 3
        assert (await store.get(cid, db=pool))["candidate"]["reason"] == "duplicate"
        assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM memory_draft_waits WHERE candidate_id=$1)", cid)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("change", ["delete", "disable", "expire", "replace"])
def test_inflight_draft_is_fenced_after_claim_changes(monkeypatch, tmp_path, change):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await sup.configure_write(repo, pool)
        cid = await sup.candidate(pool)
        started, release = asyncio.Event(), asyncio.Event()
        class Delayed(Backend):
            async def run(self, request):
                started.set()
                await release.wait()
                return await super().run(request)
        backend = Delayed(reply())
        service = MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend)
        running = asyncio.create_task(service.run_once())
        await asyncio.wait_for(started.wait(), 5)
        if change == "delete":
            await asyncio.wait_for(memory_actions.act("delete", model.format_id(cid), db=pool), 2)
            await sup.candidate(pool, statement="Next seed")
            assert not await MemoryDraftService(tmp_path / "other", db=pool, backend=backend).run_once()
        elif change == "disable":
            await pool.execute("UPDATE system_settings SET value=jsonb_set(value::jsonb,'{enabled}','false')::text "
                               "WHERE key='memory_write'")
        elif change == "replace":
            from uuid import uuid4
            await pool.execute("UPDATE memory_draft_lease SET token=$1", uuid4())
        else:
            await pool.execute("UPDATE memory_draft_lease SET expires_at=now()-interval '1 second'")
        release.set()
        assert await running
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_revisions") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_draft_lease") == (1 if change == "replace" else 0)
        if change != "delete":
            assert (await store.get(cid, db=pool))["candidate"]["state"] == "drafting"

    run_db(check, monkeypatch, timeout=30, max_size=5)


def test_expired_spend_lease_is_reclaimed_and_deadline_releases_it(monkeypatch, tmp_path):
    from uuid import uuid4
    from looper.agents import memory_draft_service

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await sup.configure_write(repo, pool)
        cid = await sup.candidate(pool)
        await pool.execute("INSERT INTO memory_draft_lease(token,candidate_id,expires_at) "
                           "VALUES($1,$2,now()-interval '1 second')", uuid4(), cid)
        started = asyncio.Event()
        class Hanging(Backend):
            async def run(self, request):
                started.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(memory_draft_service, "_CALL_SECONDS", .5)
        assert await memory_draft_service.MemoryDraftService(tmp_path / "runtime", db=pool, backend=Hanging()).run_once()
        assert started.is_set()
        assert await pool.fetchval("SELECT count(*) FROM memory_draft_lease") == 0
        assert (await store.get(cid, db=pool))["events"][-1]["note"] == "draft_failed:timeout"
        monkeypatch.setattr(memory_draft_service, "_CALL_SECONDS", 150)
        backend = Backend(reply())
        assert await memory_draft_service.MemoryDraftService(tmp_path / "runtime", db=pool, backend=backend).run_once()
        assert len(backend.requests) == 1
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "pending"

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("decision", ["same_as", "revise"])
def test_external_evidence_cannot_merge_into_an_operator_preference(monkeypatch, tmp_path, decision):
    from looper.agents.memory_draft_service import MemoryDraftService

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        raw = settings.to_json()
        raw["targets"]["decision"] = raw["targets"]["preference"]
        await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
        target = await sup.candidate(pool, statement="Existing operator preference")
        await sup.revise_current(pool, target, settings, note_path=sup.NOTE["path"])
        evidence = [store.Evidence(model.EvidenceKind.LEAD, lead_id=await sup.lead(pool))]
        cid = await sup.candidate(pool, kind="decision", statement="External suggestion", evidence=evidence)
        output = reply(revise="P1") if decision == "revise" else json.dumps({
            "decision": "same_as", "of": "P1", "reason": "Same note", "instructions_seen": []})
        assert await MemoryDraftService(tmp_path / "runtime", db=pool, backend=Backend(output)).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert row["state"] == "dropped" and row["reason"] == "duplicate"
        other = await store.get(target, db=pool)
        assert other["candidate"]["source_class"] == "operator" and len(other["sources"]) == 1
        assert other["candidate"]["revision"] == 1

    run_db(check, monkeypatch)
