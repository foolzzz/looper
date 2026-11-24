"""Concurrent admission through the real service and explicit manual entry points."""
from __future__ import annotations

import asyncio

from tests.db_support import run_db
from tests.intent_rule_execution_support import URL, configure, fact


def test_two_service_workers_share_one_revision_claim(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import intent_rule_execution as ledger
        await configure(pool, tmp_path)
        async def verifier(*_args, **_kwargs):
            return fact()
        first = engine.IntentRuleService(db=pool, verifier=verifier)
        second = engine.IntentRuleService(db=pool, verifier=verifier)
        for reference in ("first-request", "second-request"):
            await first.submit_discovery("discovery", reference, "review-system", URL, principal=OPERATOR)
        async with pool.acquire() as conn, conn.transaction():
            work = await ledger.claim_work(conn, now=engine._now())
        ready = asyncio.Event()
        arrived = 0
        original = engine._lock_sources
        async def simultaneous_admission(conn, candidate, work):
            nonlocal arrived
            result = await original(conn, candidate, work)
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return result
        monkeypatch.setattr(engine, "_lock_sources", simultaneous_admission)
        await asyncio.gather(first._process(work[0]), second._process(work[1]))
        assert arrived == 2
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_mr_claims") == 1
        assert await pool.fetchval("SELECT sum(used) FROM intent_rule_project_days") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_executions") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='created'") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hit_executions") == 2
        assert await pool.fetchval("SELECT count(DISTINCT execution_id) FROM intent_rule_hit_executions") == 1
    run_db(check, monkeypatch, max_size=5, timeout=20)


def test_manual_and_automatic_admission_compete_for_one_revision(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import intent_rule_execution as ledger
        await configure(pool, tmp_path)
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        monkeypatch.setattr(engine, "_INSTALLED", service)
        automatic = await service.submit_discovery("discovery", "automatic", "review-system", URL, principal=OPERATOR)
        manual = await service.submit_discovery("discovery", "manual", "review-system", URL, principal=OPERATOR)
        async with pool.acquire() as conn, conn.transaction():
            items = await ledger.claim_work(conn, now=engine._now())
        work = next(work for work in items if work["discovery_id"] == automatic["discovery_id"])
        ready = asyncio.Event()
        arrived = 0
        original = engine._lock_sources
        async def simultaneous_admission(conn, candidate, work):
            nonlocal arrived
            result = await original(conn, candidate, work)
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return result
        monkeypatch.setattr(engine, "_lock_sources", simultaneous_admission)
        _, built = await asyncio.gather(service._process(work), engine.discovery_build(
            manual["discovery_id"], "review", "review", principal=OPERATOR, db=pool))
        assert arrived == 2
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT task_id FROM intent_rule_mr_claims") == built["task_id"]
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hit_executions") == 2
        assert await pool.fetchval("SELECT count(DISTINCT execution_id) FROM intent_rule_hit_executions") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='created'") == 1
        mode = await pool.fetchval("SELECT mode FROM intent_rule_executions")
        used = await pool.fetchval("SELECT coalesce(sum(used),0) FROM intent_rule_project_days")
        assert used == (1 if mode == "auto" else 0)
    run_db(check, monkeypatch, max_size=5, timeout=20)


def test_reenabled_switch_never_reauthorizes_work_from_an_old_generation(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import intent_rules as store
        await configure(pool, tmp_path)
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        await service.submit_discovery("discovery", "old-generation", "review-system", URL, principal=OPERATOR)
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
        async with pool.acquire() as conn, conn.transaction():
            await store.lock_admission(conn)
            config = await store.lock_config(conn)
            await store.update_config(conn, generation=config["generation"] + 1, adapter=config["adapter"],
                discovery_sources=config["discovery_sources"], activation_acceptance=[], now=engine._now())
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("old work cannot trigger another external verification")
        service.verifier = forbidden
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 0
        await pool.execute("UPDATE system_settings SET value='true' WHERE key='intents.rules.enabled'")
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "config_changed"
        assert await pool.fetchval("SELECT state FROM intent_rule_discoveries") == "confirmation"
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_verification_health") == 0
    run_db(check, monkeypatch)


def test_project_snapshot_precedes_first_external_verification(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        async def verifier(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        submitted = asyncio.create_task(service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR))
        await asyncio.wait_for(entered.wait(), 5)
        await pool.execute("UPDATE projects SET work_dir=$1,updated_at=now() WHERE id='review'", str(tmp_path / "changed"))
        release.set()
        await submitted
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "project_changed"
    run_db(check, monkeypatch)


def test_non_mr_candidates_do_not_count_as_adapter_failures(monkeypatch, tmp_path):
    async def check(pool):
        from datetime import datetime, timezone
        from looper.agents import extraction, intent_rule_engine as engine
        from tests import subscription_support as stream
        await configure(pool, tmp_path)
        await stream.source(pool)
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("ordinary development requests do not invoke MR verification")
        service = engine.IntentRuleService(db=pool, verifier=forbidden)
        for number in range(3):
            seq = await stream.add(pool, f"Synthetic development request {number}", kind="p2p", sent_at=datetime.now(timezone.utc))
            parsed = extraction.Parsed("items", items=[{"type":"dev_task", "title":"Synthetic development request",
                "summary":"A development request", "confidence":"high", "sources":[seq], "window_sources":[seq], "same_as":None}])
            prepared = await engine.prepare_for_extraction(parsed, source_id=stream.SOURCE, db=pool)
            async with pool.acquire() as conn, conn.transaction():
                await extraction.store_intents(conn, parsed, source_id=stream.SOURCE, schedule_id=None,
                    run_id=None, record_id=None, now=datetime.now(timezone.utc), more=False, rules_on=True, prepared_rules=prepared)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 3
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_verification_health") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='verification_fault'") == 0
    run_db(check, monkeypatch)


def test_message_project_snapshot_precedes_first_external_verification(monkeypatch, tmp_path):
    async def check(pool):
        from datetime import datetime, timezone
        from looper.agents import extraction, intent_rule_engine as engine, intent_rule_evaluation as evaluation
        from tests import subscription_support as stream
        await configure(pool, tmp_path)
        await stream.source(pool)
        seq = await stream.add(pool, "Review " + URL, kind="p2p", sent_at=datetime.now(timezone.utc))
        parsed = extraction.Parsed("items", items=[{"type":"mr_review", "title":"Review the MR",
            "summary":"Synthetic review request", "confidence":"high", "sources":[seq],
            "window_sources":[seq], "same_as":None}])
        entered = asyncio.Event()
        release = asyncio.Event()
        async def verifier(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return fact()
        monkeypatch.setattr(evaluation, "verify", verifier)
        pending = asyncio.create_task(engine.prepare_for_extraction(parsed, source_id=stream.SOURCE, db=pool))
        await asyncio.wait_for(entered.wait(), 5)
        await pool.execute("UPDATE projects SET work_dir=$1,updated_at=now() WHERE id='review'", str(tmp_path / "changed"))
        release.set()
        prepared = await pending
        async with pool.acquire() as conn, conn.transaction():
            await extraction.store_intents(conn, parsed, source_id=stream.SOURCE, schedule_id=None,
                run_id=None, record_id=None, now=datetime.now(timezone.utc), more=False, rules_on=True, prepared_rules=prepared)
        await engine.IntentRuleService(db=pool, verifier=verifier).run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "project_changed"
    run_db(check, monkeypatch)


def test_hit_retains_ordered_type_and_scope_skip_explanations(monkeypatch, tmp_path):
    async def check(pool):
        from uuid import uuid4
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import intent_rules as store
        await configure(pool, tmp_path)
        first, second = str(uuid4()), str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            current = (await store.read_rules(conn))[0]
            await store.append_rule_version(conn, {**current, "version":2, "position":3}, edited_by="operator")
            for identity, position, intent_type, scope in [
                (first, 1, "bug_triage", {"kind":"all"}),
                (second, 2, "mr_review", {"kind":"named_chats", "chats":[{"source_id":"other-source","chat_id":"other-room"}]})]:
                await store.append_rule_version(conn, {"id":identity,"version":1,"enabled":True,"position":position,
                    "intent_type":intent_type,"scope":scope,"conditions":[],"action":"record_only", "project_id":None,
                    "template_key":None}, edited_by="operator")
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        detail = await store.get_hit(pool, await pool.fetchval("SELECT id FROM intent_rule_hits"))
        assert [(attempt["rule_id"], attempt["rule_version"], attempt["reason"]) for attempt in detail["attempts"][:2]] == [
            (first, 1, "type_mismatch"), (second, 1, "scope_mismatch")]
        assert detail["attempts"][-1]["result"] == "passed"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch)


def test_source_snapshot_uses_the_same_events_that_supplied_the_target(monkeypatch, tmp_path):
    async def check(pool):
        from datetime import datetime, timezone
        from looper.agents import extraction, intent_rule_engine as engine, intent_rule_evaluation as evaluation
        from tests import subscription_support as stream
        await configure(pool, tmp_path)
        await stream.source(pool)
        seq = await stream.add(pool, "Review " + URL, kind="p2p", sent_at=datetime.now(timezone.utc))
        parsed = extraction.Parsed("items", items=[{"type":"mr_review", "title":"Review the MR", "summary":"Synthetic request",
            "confidence":"high", "sources":[seq], "window_sources":[seq], "same_as":None}])
        original = engine._reference_snapshot
        changed = False
        async def change_after_target_read(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                await pool.execute("UPDATE source_events SET text='Request was withdrawn' WHERE seq=$1", seq)
            return await original(*args, **kwargs)
        monkeypatch.setattr(engine, "_reference_snapshot", change_after_target_read)
        async def verifier(*_args, **_kwargs):
            return fact()
        monkeypatch.setattr(evaluation, "verify", verifier)
        prepared = await engine.prepare_for_extraction(parsed, source_id=stream.SOURCE, db=pool)
        async with pool.acquire() as conn, conn.transaction():
            await extraction.store_intents(conn, parsed, source_id=stream.SOURCE, schedule_id=None,
                run_id=None, record_id=None, now=datetime.now(timezone.utc), more=False, rules_on=True, prepared_rules=prepared)
        await engine.IntentRuleService(db=pool, verifier=verifier).run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "source_revoked"
    run_db(check, monkeypatch)


def test_concurrent_manual_builds_and_repeated_click_share_the_task(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        monkeypatch.setattr(engine, "_INSTALLED", service)
        record = await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        entered = 0
        ready = asyncio.Event()
        async def simultaneous_verify(*_args, **_kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return fact()
        service.verifier = simultaneous_verify
        first, second = await asyncio.gather(*[engine.discovery_build(record["discovery_id"], "review", "review",
            principal=OPERATOR, db=pool) for _ in range(2)])
        assert first["task_id"] == second["task_id"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_mr_claims") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='created'") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_project_days") == 0
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("a repeated completed build is only a readback")
        service.verifier = forbidden
        again = await engine.discovery_build(record["discovery_id"], "review", "review", principal=OPERATOR, db=pool)
        assert again == first
    run_db(check, monkeypatch, max_size=5, timeout=20)


def test_schedule_revocation_blocks_queued_work_but_runtime_updates_do_not(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from looper.agents import extraction, intent_rule_engine as engine, intent_rule_evaluation as evaluation
    from tests import subscription_support as stream
    async def check(pool):
        await configure(pool, tmp_path)
        await stream.source(pool)
        await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,enabled,kind) "
            "VALUES('request-source','review','Synthetic source','* * * * *','UTC','Extract synthetic requests',true,'subscription')")
        await pool.execute("INSERT INTO subscriptions(schedule_id,source_id,chat_kinds,sender_kinds,max_age_minutes,max_events,render,text_max_chars,output_kind) "
            "VALUES('request-source',$1,ARRAY['p2p'],ARRAY['user'],180,100,'lines_v1',200,'intents')", stream.SOURCE)
        async def first_verify(*_args, **_kwargs):
            return fact()
        monkeypatch.setattr(evaluation, "verify", first_verify)
        async def enqueue():
            seq = await stream.add(pool, "Review " + URL, kind="p2p", sent_at=datetime.now(timezone.utc))
            parsed = extraction.Parsed("items", items=[{"type":"mr_review", "title":"Review the MR", "summary":"Synthetic request",
                "confidence":"high", "sources":[seq], "window_sources":[seq], "same_as":None}])
            prepared = await engine.prepare_for_extraction(parsed, source_id=stream.SOURCE, db=pool)
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT id FROM schedules WHERE id='request-source' FOR NO KEY UPDATE")
                await extraction.store_intents(conn, parsed, source_id=stream.SOURCE, schedule_id="request-source",
                    run_id=None, record_id=None, now=datetime.now(timezone.utc), more=False, rules_on=True, prepared_rules=prepared)
        # A scheduler completion changes runtime fields, not extraction authority.
        await enqueue()
        await pool.execute("UPDATE schedules SET updated_at=now(),last_fired_at=now(),consecutive_failures=1 WHERE id='request-source'")
        service = engine.IntentRuleService(db=pool, verifier=first_verify)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        await enqueue()
        entered, release = asyncio.Event(), asyncio.Event()
        async def final_verify(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return fact()
        service.verifier = final_verify
        running = asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(), 5)
        await pool.execute("UPDATE schedules SET enabled=false,next_fire_at=NULL WHERE id='request-source'")
        release.set()
        await running
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT reason FROM intent_rule_work ORDER BY id DESC LIMIT 1") == "source_revoked"
    run_db(check, monkeypatch)


def test_draining_preserves_work_and_start_outboxes_without_publishing(monkeypatch, tmp_path):
    async def check(pool):
        from types import SimpleNamespace
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        drain = SimpleNamespace(draining=False)
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier, drain=drain)
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        drain.draining = True
        await service.run_once()
        assert await pool.fetchval("SELECT state FROM intent_rule_work") == "pending"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        drain.draining = False
        async def drain_during_verify(*_args, **_kwargs):
            drain.draining = True
            return fact()
        service.verifier = drain_during_verify
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 0
        assert await pool.fetchval("SELECT state FROM intent_rule_work") == "evaluating"
        drain.draining = False
        await pool.execute("UPDATE intent_rule_work SET lease_until=now()-interval '1 second'")
        service.verifier = verifier
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        async def forbidden(_envelope):
            raise AssertionError("draining cannot publish a start request")
        service.platform = SimpleNamespace(bus=SimpleNamespace(publish=forbidden))
        drain.draining = True
        await service.run_once()
        assert await pool.fetchval("SELECT state FROM intent_rule_task_starts") == "pending"
        assert await pool.fetchval("SELECT start_state FROM intent_rule_executions") == "pending"
    run_db(check, monkeypatch)
