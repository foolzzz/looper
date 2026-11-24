"""Execution contracts over synthetic discoveries, tasks and a disposable database."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.db_support import run_db

from tests.intent_rule_execution_support import URL, configure, fact


def test_auto_task_requirement_contains_only_fixed_verified_input(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        async def verifier(*_args, **_kwargs):
            return fact()
        service = engine.IntentRuleService(db=pool, verifier=verifier)
        created = await service.submit_discovery("discovery", "request-7", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        row = await pool.fetchrow("SELECT * FROM tasks")
        assert row and row["requirement"] == f"按本项目评审流程评审此 MR：{URL}"
        assert row["status"] == "init"
        assert await pool.fetchval("SELECT state FROM intent_rule_discoveries WHERE id=$1", created["discovery_id"]) == "tasked"
        assert await pool.fetchval("SELECT used FROM intent_rule_project_days") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_task_starts") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_notices WHERE kind='created'") == 1
    run_db(check, monkeypatch)


def test_shared_revision_claim_wins_across_sources_and_manual_build(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = IntentRuleService(db=pool, verifier=lambda *_args, **_kw: asyncio.sleep(0, result=fact()))
        first, second = await asyncio.gather(*[service.submit_discovery("discovery", ref, "review-system", URL,
            principal=OPERATOR) for ref in ("request-a", "request-b")])
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_mr_claims") == 1
        assert await pool.fetchval("SELECT used FROM intent_rule_project_days") == 1
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hit_executions") == 2
        repeat = await service.submit_discovery("discovery", "request-a", "review-system", URL, principal=OPERATOR)
        assert repeat["discovery_id"] == first["discovery_id"]
    run_db(check, monkeypatch)


def test_project_daily_cap_is_shared_atomic_and_timezone_scoped(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        await pool.execute("INSERT INTO intent_rule_project_days(operator_key,project_id,local_day,used,timezone) "
            "VALUES('operator','review',CURRENT_DATE,49,'UTC')")
        service = IntentRuleService(db=pool, verifier=lambda *_args, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "fiftieth", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        service.verifier = lambda *_args, **_kw: asyncio.sleep(0, result=fact("head-b"))
        await service.submit_discovery("discovery", "fifty-first", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT used FROM intent_rule_project_days") == 50
        assert await pool.fetchval("SELECT reason FROM intent_rule_work ORDER BY id DESC LIMIT 1") == "daily_cap"
        from datetime import datetime, timedelta, timezone
        from looper.db import intent_rules as store
        rule_id = str(await pool.fetchval("SELECT id FROM intent_rules"))
        now = datetime.now(timezone.utc)
        stats = await store.hit_stats(pool, rule_id=rule_id, since=now-timedelta(days=1), until=now+timedelta(seconds=1))
        assert len(stats["rules"]) == 1
        assert stats["rules"][0]["daily_cap"] == 1
        assert stats["rules"][0]["auto_created"] == 1

    run_db(check, monkeypatch)


def test_first_revision_survives_restart_and_changed_revision_confirms(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = IntentRuleService(db=pool, verifier=lambda *_args, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        restarted = IntentRuleService(db=pool, verifier=lambda *_args, **_kw: asyncio.sleep(0, result=fact("head-b")))
        await restarted.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "revision_changed"
        assert await pool.fetchval("SELECT state FROM intent_rule_discoveries") == "confirmation"
    run_db(check, monkeypatch)


def test_audit_failure_rolls_back_task_claim_and_quota(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = IntentRuleService(db=pool, verifier=lambda *_args, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await pool.execute("CREATE FUNCTION reject_hit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'blocked'; END $$; "
            "CREATE TRIGGER reject_hit BEFORE INSERT ON intent_rule_hits FOR EACH ROW EXECUTE FUNCTION reject_hit()")
        await service.run_once()
        for table in ("tasks","intent_rule_mr_claims","intent_rule_executions","intent_rule_task_starts"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
        assert await pool.fetchval("SELECT coalesce(sum(used),0) FROM intent_rule_project_days") == 0
        assert tuple(await pool.fetchrow("SELECT decision,reason,hit_id FROM intent_rule_work")) == (
            "push_for_confirmation","audit_unavailable",None)
    run_db(check, monkeypatch)


def test_revoke_is_operator_only_and_serializes_with_start(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR, Principal
        from looper.agents.ops import OpError
        from looper.db import tasks
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        execution = await pool.fetchrow("SELECT id,task_id FROM intent_rule_executions")
        with pytest.raises(OpError):
            await engine.revoke(execution["id"], principal=Principal("client", "synthetic"), db=pool)
        result = await engine.revoke(execution["id"], principal=OPERATOR, db=pool)
        assert result["state"] == "stopped"
        assert await tasks.start_ready_task(execution["task_id"], pool=pool) is None
        assert await pool.fetchval("SELECT status FROM tasks") == "cancelled"
        assert await pool.fetchval("SELECT state FROM intent_rule_task_starts") == "cancelled"
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_mr_claims") == 1
        assert await pool.fetchval("SELECT used FROM intent_rule_project_days") == 1
        assert (await engine.revoke(execution["id"], principal=OPERATOR, db=pool))["state"] == "stopped"
    run_db(check, monkeypatch)


def test_cancelled_revision_is_not_recreated(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "first", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        execution_id = await pool.fetchval("SELECT id FROM intent_rule_executions")
        await engine.revoke(execution_id, principal=OPERATOR, db=pool)
        await service.submit_discovery("discovery", "same-revision", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        service.verifier = lambda *_a, **_kw: asyncio.sleep(0, result=fact("head-b"))
        await service.submit_discovery("discovery", "new-revision", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 2
        assert await pool.fetchval("SELECT used FROM intent_rule_project_days") == 2
    run_db(check, monkeypatch)


def test_rule_or_project_change_and_source_revocation_prevent_create(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "first", "review-system", URL, principal=OPERATOR)
        await pool.execute("UPDATE projects SET auto_task_config_version=auto_task_config_version+1 WHERE id='review'")
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "project_changed"
        await service.submit_discovery("discovery", "second", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch)


def test_start_and_notice_recovery_never_creates_again(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents.intent_rule_engine import IntentRuleService
        from looper.agents.api_v1 import OPERATOR
        from looper.db import tasks
        await configure(pool, tmp_path)
        service = IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        bus = SimpleNamespace(publish=lambda envelope: received.put(envelope))
        received = asyncio.Queue()
        service.platform = SimpleNamespace(bus=bus)
        await service.run_once()
        envelope = await asyncio.wait_for(received.get(), 1)
        task_id = envelope.payload["task_id"]
        assert await tasks.start_ready_task(task_id, pool=pool)
        assert await tasks.start_ready_task(task_id, pool=pool) is None
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT start_state FROM intent_rule_executions") == "started"
        assert await pool.fetchval("SELECT state FROM intent_rule_task_starts") == "acknowledged"
    run_db(check, monkeypatch)


def test_discovery_confirmation_can_be_manually_built_without_quota(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        record = await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        # Manual use of the installed verifier is the same read-only protocol.
        monkeypatch.setattr(engine, "_INSTALLED", service)
        await pool.execute("UPDATE projects SET auto_task_daily_cap=1 WHERE id='review'")
        await service.run_once()
        assert await pool.fetchval("SELECT state FROM intent_rule_discoveries") == "confirmation"
        result = await engine.discovery_build(record["discovery_id"], "review", "review", principal=OPERATOR, db=pool)
        assert result["task_id"]
        assert await pool.fetchval("SELECT mode FROM intent_rule_executions") == "manual"
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_project_days") == 0
        assert await pool.fetchval("SELECT decision FROM intent_rule_work") == "create_task"
    run_db(check, monkeypatch)


def test_discovery_migration_requires_shared_claims_and_blocks_unknown_revision(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        monkeypatch.setattr(engine, "_INSTALLED", service)
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES('task-existing','review','Synthetic existing review','completed')")
        preview = await engine.migration_preview("discovery", [{"task_id":"task-existing", "system_id":"review-system", "mr_url":URL}], principal=OPERATOR, db=pool)
        assert preview["blocked"] == 1
        result = await engine.migration_confirm(preview["preview_id"], principal=OPERATOR, db=pool)
        assert result == {"registered":0,"blocked":1,"conflicts":0}
        assert await engine.migration_confirm(preview["preview_id"], principal=OPERATOR, db=pool) == result
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "unknown_legacy_revision"
    run_db(check, monkeypatch)


def test_stale_work_lease_cannot_create_or_append_audit(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import intent_rule_execution as ledger
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        async with pool.acquire() as conn, conn.transaction():
            stale = (await ledger.claim_work(conn, now=engine._now()))[0]
        await pool.execute("UPDATE intent_rule_work SET lease_until=now()-interval '1 second'")
        async with pool.acquire() as conn, conn.transaction():
            current = (await ledger.claim_work(conn, now=engine._now()))[0]
        assert stale["lease_token"] != current["lease_token"]
        await service._process(stale)
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hits") == 0
        assert await pool.fetchval("SELECT state FROM intent_rule_work") == "evaluating"
        await service._process(current)
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch)


def test_switch_off_during_final_verify_leaves_work_without_rule_writes(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        async def turn_off(*_args, **_kwargs):
            await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.rules.enabled'")
            return fact()
        service.verifier = turn_off
        await service.run_once()
        for table in ("tasks", "intent_rule_hits", "intent_rule_verification_health"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
        assert await pool.fetchval("SELECT state FROM intent_rule_work") == "evaluating"
    run_db(check, monkeypatch)


def test_claim_revision_records_manual_execution_for_later_dedup(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        monkeypatch.setattr(engine, "_INSTALLED", service)
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES('task-existing','review','Synthetic existing review','completed')")
        await engine.claim_revision("task-existing", "review-system", URL, "head-a", principal=OPERATOR, db=pool)
        assert await pool.fetchval("SELECT mode FROM intent_rule_executions") == "manual"
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert await pool.fetchval("SELECT reason FROM intent_rule_work") == "deduped"
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_project_days") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_task_starts") == 0
    run_db(check, monkeypatch)


def test_migration_confirm_reports_deleted_task_as_conflict(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        monkeypatch.setattr(engine, "_INSTALLED", service)
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES('task-existing','review','Synthetic existing review','completed')")
        preview = await engine.migration_preview("discovery", [{"task_id":"task-existing", "system_id":"review-system", "mr_url":URL, "revision":"head-a"}], principal=OPERATOR, db=pool)
        await pool.execute("DELETE FROM tasks WHERE id='task-existing'")
        result = await engine.migration_confirm(preview["preview_id"], principal=OPERATOR, db=pool)
        assert result == {"registered":0,"blocked":0,"conflicts":1}
    run_db(check, monkeypatch)


def test_creation_notice_keeps_operator_target_after_source_deletion(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        notice = await pool.fetchrow("SELECT channel_id,chat FROM intent_rule_notices WHERE kind='created'")
        assert tuple(notice) == ("telegram:synthetic", "operator-chat")
        await pool.execute("DELETE FROM intent_rule_discoveries")
        assert tuple(await pool.fetchrow("SELECT channel_id,chat FROM intent_rule_notices WHERE kind='created'")) == tuple(notice)
    run_db(check, monkeypatch)


def test_publish_is_requested_until_task_router_acknowledges_start(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        from looper.db import tasks
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0, result=fact()))
        await service.submit_discovery("discovery", "request", "review-system", URL, principal=OPERATOR)
        await service.run_once()
        received = asyncio.Queue()
        service.platform = SimpleNamespace(bus=SimpleNamespace(publish=received.put))
        await service.run_once()
        envelope = await asyncio.wait_for(received.get(), 1)
        assert await pool.fetchval("SELECT status FROM tasks") == "init"
        assert await pool.fetchval("SELECT start_state FROM intent_rule_executions") == "requested"
        assert await tasks.start_ready_task(envelope.payload["task_id"], pool=pool)
        assert await pool.fetchval("SELECT start_state FROM intent_rule_executions") == "started"
    run_db(check, monkeypatch)
