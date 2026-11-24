"""Execution ledger atomicity, bounded recovery and independent evidence lifetime."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest

from looper.db import bridge_upgrade, connection, intent_rules as store, migrations
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
KEY = {"system_id": "review", "repository_id": "repo", "mr_id": "17", "revision": "head-a",
       "canonical_url": "https://review.example/repo/mr/17"}
TABLES = ("intent_rule_discoveries", "intent_rule_work", "intent_rule_mr_targets", "intent_rule_mr_claims",
          "intent_rule_project_days", "intent_rule_executions", "intent_rule_hit_executions",
          "intent_rule_task_starts", "intent_rule_notices", "intent_rule_verification_health",
          "intent_rule_migration_previews")


async def discovery(conn, ref="one"):
    return await store.create_discovery(conn, discovery_source_id="source", source_ref=ref,
                                        system_id="review", mr_url=KEY["canonical_url"])


async def task(conn, name="task-a"):
    await conn.execute("INSERT INTO projects(id,name) VALUES ('project','Review') ON CONFLICT DO NOTHING")
    await conn.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES ($1,'project','Review verified target','init')", name)
    return name


def test_execution_migration_fresh_upgrade_and_reentry(monkeypatch):
    from looper.db.migrations import intent_rule_execution as migration

    async def test():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            old = tuple(item for item in connection._MIGRATIONS if item.name != migration.name)
            with monkeypatch.context() as patch:
                patch.setattr(connection, "_MIGRATIONS", old)
                patch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(old))
                await connection.ensure_schema(upgraded)
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert migration.order == migration.pending_order == 1900
            assert sum(item.order == 1900 for item in migrations.discover()) == 1
            assert await structure(upgraded) == await structure(fresh)
            assert {row["name"] for row in await fresh.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}
            for conn in (upgraded, fresh):
                assert not await migration.pending(conn)
                assert not await conn.fetchval("SELECT count(*) FROM intent_rule_work")
                assert not await conn.fetchval("SELECT count(*) FROM intent_rule_config")
                await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", migration.name)
                await conn.execute("DROP INDEX idx_intent_rule_work_ready")
                assert await migration.pending(conn)
                await migration.apply(conn, sql="", state="current")
                assert not await migration.pending(conn)
                shape = await structure(conn)
                await conn.execute(migration.fresh_ddl)
                assert await structure(conn) == shape
    asyncio.run(test())


@pytest.mark.parametrize("damage", [
    "ALTER TABLE intent_rule_work DROP CONSTRAINT intent_rule_work_source_check",
    "ALTER TABLE intent_rule_hits DROP CONSTRAINT intent_rule_hits_discovery_id_fkey",
    "ALTER TABLE intent_rule_mr_claims DROP CONSTRAINT intent_rule_mr_claims_pkey",
    "DROP INDEX idx_intent_rule_notices_created",
    "DROP TABLE intent_rule_task_starts",
    "ALTER TABLE intent_rule_hits DROP COLUMN attempts",
    "ALTER TABLE intent_rule_hits DROP CONSTRAINT intent_rule_hits_selection_check; "
    "ALTER TABLE intent_rule_hits ADD CONSTRAINT intent_rule_hits_selection_check "
    "CHECK (selection IN ('matched','unmatched','fallback'))",
])
def test_execution_migration_checks_objects_even_with_marker(damage):
    from looper.db.migrations import intent_rule_execution as migration

    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", migration.name)
            await conn.execute(damage)
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="current")
            assert not await migration.pending(conn)
    run_db(test)


def test_work_is_enqueued_atomically_and_lease_recovered_without_backfill():
    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            work = await store.enqueue_work(conn, discovery_id=item["id"], generation=4,
                source_fingerprint="source-v1", first_reason="adapter_missing")
            duplicate = await store.enqueue_work(conn, discovery_id=item["id"], generation=9,
                source_fingerprint="different", first_reason="adapter_missing")
            assert duplicate["id"] == work["id"] and duplicate["generation"] == 4
        async with pool.acquire() as conn, conn.transaction():
            claimed = await store.claim_work(conn, now=NOW, lease_seconds=30)
            assert [row["id"] for row in claimed] == [work["id"]]
            token = claimed[0]["lease_token"]
            assert not await store.claim_work(conn, now=NOW + timedelta(seconds=29))
        async with pool.acquire() as conn, conn.transaction():
            recovered = await store.claim_work(conn, now=NOW + timedelta(seconds=31))
            assert recovered[0]["lease_token"] != token
            assert not await store.decide_work(conn, work["id"], decision="record_only", reason="matched",
                                               lease_token=token, now=NOW)
            assert await store.decide_work(conn, work["id"], decision="push_for_confirmation",
                reason="adapter_missing", lease_token=recovered[0]["lease_token"], now=NOW)
            assert not await store.claim_work(conn, now=NOW + timedelta(hours=1))
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_work") == 1
    run_db(test)


def test_shared_claim_and_daily_cap_have_transactional_lifetimes():
    async def test(pool):
        quota = dict(operator_key="operator", project_id="project", local_day=date(2026, 11, 24), timezone="UTC")
        async with pool.acquire() as conn, conn.transaction():
            await task(conn)
            assert (await store.lock_mr_target(conn, KEY))["blocked_reason"] is None
            assert await store.get_claim(conn, KEY) is None
            assert (await store.insert_claim(conn, KEY, "task-a"))["task_id"] == "task-a"
            assert (await store.insert_claim(conn, KEY, "another-task"))["task_id"] == "task-a"
            for _ in range(50):
                assert await store.consume_project_day(conn, **quota, cap=50)
            assert not await store.consume_project_day(conn, **quota, cap=50)
            try:
                async with conn.transaction():
                    assert await store.consume_project_day(conn, **{**quota, "local_day": date(2026, 11, 25)}, cap=50)
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_project_days") == 1
            await conn.execute("DELETE FROM tasks WHERE id='task-a'")
            assert (await store.get_claim(conn, KEY))["task_id"] == "task-a"
            assert (await store.lock_project_day(conn, **quota))["used"] == 50
    run_db(test)


def test_execution_and_claim_survive_evidence_deletion():
    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            await task(conn)
            execution = await store.insert_execution(conn, task_id="task-a", mode="auto", mr_key=KEY,
                quota_day=date(2026, 11, 24), quota_operator="operator", quota_project="project", quota_units=1)
            hit = await store.append_hit(conn, {"evaluation_key": "hit-a", "intent_id": None,
                "discovery_id": item["id"], "generation": 1, "rule_id": None, "rule_version": None,
                "selection": "fallback", "action": "push_for_confirmation", "reason": "no_match",
                "verified_at": None, "mr_identity": KEY})
            await store.link_hit_execution(conn, hit, execution["id"])
            await store.enqueue_start(conn, "task-a", execution["id"])
            await store.enqueue_notice(conn, kind="created", dedupe_key="rule-created:1", execution_id=execution["id"])
            assert (await store.get_hit(conn, hit))["execution"]["task_id"] == "task-a"
            await conn.execute("DELETE FROM intent_rule_discoveries WHERE id=$1", item["id"])
            assert await store.get_hit(conn, hit) is None
            assert (await store.execution_view(conn, execution["id"]))["quota"]["units"] == 1
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_task_starts") == 1
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_notices") == 1
    run_db(test)


def test_starts_acknowledge_only_claim_owner_and_back_off():
    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            await task(conn)
            execution = await store.insert_execution(conn, task_id="task-a", mode="manual", mr_key=KEY)
            await store.enqueue_start(conn, "task-a", execution["id"])
            await conn.execute("UPDATE intent_rule_task_starts SET next_attempt_at=$1", NOW)
            first = (await store.claim_starts(conn, now=NOW))[0]
            assert not await store.claim_starts(conn, now=NOW)
            assert await store.finish_start(conn, "task-a", lease_token=first["lease_token"],
                state="pending", error_code="start_failed", now=NOW)
            assert not await store.claim_starts(conn, now=NOW + timedelta(seconds=29))
            second = (await store.claim_starts(conn, now=NOW + timedelta(seconds=30)))[0]
            assert second["attempts"] == 2
            assert await store.finish_start(conn, "task-a", lease_token=second["lease_token"],
                state="acknowledged", now=NOW + timedelta(seconds=30))
            assert (await store.execution_view(conn, execution["id"]))["start_state"] == "started"
    run_db(test)


def test_verification_health_alert_is_one_per_unavailable_episode():
    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            for number in range(1, 5):
                notice = await store.record_verification(conn, "review", available=False, now=NOW)
                assert (notice is not None) == (number == 3)
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_notices") == 1
            await store.record_verification(conn, "review", available=True, now=NOW)
            for _ in range(3):
                await store.record_verification(conn, "review", available=False, now=NOW)
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_notices") == 2
    run_db(test)


def test_closed_states_and_exactly_one_source_are_enforced():
    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            for statement in (
                "INSERT INTO intent_rule_work(generation,source_fingerprint,first_verification_status) VALUES (0,'x','unavailable')",
                "INSERT INTO intent_rule_notices(kind,dedupe_key) VALUES ('created','missing-execution')",
                "INSERT INTO intent_rule_mr_targets(system_id,repository_id,mr_id,blocked_reason) VALUES ('s','r','m','guess')",
                "UPDATE intent_rule_discoveries SET state='unknown' WHERE id=" + str(item["id"]),
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with conn.transaction():
                        await conn.execute(statement)
    run_db(test)


def test_hit_statistics_count_unique_creations_and_dedup_hits():
    from datetime import timedelta
    from uuid import uuid4

    async def test(pool):
        rule_a, rule_b = str(uuid4()), str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            await task(conn)
            execution = await store.insert_execution(conn, task_id="task-a", mode="auto", mr_key=KEY,
                quota_day=NOW.date(), quota_operator="operator", quota_project="project", quota_units=1)
            for index, (rule, reason) in enumerate(((rule_a, "matched"), (rule_a, "deduped"), (rule_b, "deduped"))):
                item = await discovery(conn, f"stats-{index}")
                hit = await store.append_hit(conn, {"evaluation_key": f"stats-{index}", "intent_id": None,
                    "discovery_id": item["id"], "generation": 1, "rule_id": rule, "rule_version": 1,
                    "selection": "matched", "action": "create_task", "reason": reason,
                    "verified_at": None, "mr_identity": KEY})
                await store.link_hit_execution(conn, hit, execution["id"])
            now = datetime.now(timezone.utc)
            stats = await store.hit_stats(conn, since=now - timedelta(days=1), until=now + timedelta(days=1))
            rows = {row["rule_id"]: row for row in stats["rules"]}
            assert rows[rule_a]["hits"] == 2 and rows[rule_a]["auto_created"] == 1
            assert rows[rule_a]["deduped"] == 1
            assert rows[rule_b]["auto_created"] == 0 and rows[rule_b]["deduped"] == 1
            page = await store.list_hits(conn, limit=1)
            assert page["more"] and page["hits"][0]["task_id"] == "task-a"
            assert page["hits"][0]["execution_state"] == "pending"
    run_db(test)


def test_mr_target_lock_serializes_competing_revision_claims():
    async def test(pool):
        owned, contending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def owner():
            async with pool.acquire() as conn, conn.transaction():
                await store.lock_mr_target(conn, KEY)
                owned.set()
                await contending.wait()
                assert (await store.insert_claim(conn, KEY, "task-a"))["task_id"] == "task-a"
                release.set()

        async def contender():
            await owned.wait()
            async with pool.acquire() as conn, conn.transaction():
                contending.set()
                await store.lock_mr_target(conn, KEY)
                assert release.is_set()
                assert (await store.insert_claim(conn, KEY, "task-b"))["task_id"] == "task-a"

        await asyncio.wait_for(asyncio.gather(owner(), contender()), 10)
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_mr_claims") == 1
    run_db(test)


def test_work_persists_closed_verification_facts_only():
    from looper.agents.intent_rule_verifier import MRTarget, VerifiedFact

    async def test(pool):
        target = MRTarget("review", KEY["canonical_url"], "review.example", {"mr_id": "17"})
        fact = VerifiedFact(target=target, exists=True, state="open", reviewers=("operator",),
            repository="repo", mr_id="17", revision="head-a", canonical_url=KEY["canonical_url"], operator_id="operator")
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            with pytest.raises(ValueError):
                await store.enqueue_work(conn, discovery_id=item["id"], generation=1, source_fingerprint="fact",
                    first_fact={**fact.to_dict(), "body": "untrusted"}, first_verification_status="verified")
            saved = await store.enqueue_work(conn, discovery_id=item["id"], generation=1, source_fingerprint="fact",
                first_fact=fact.to_dict(), first_verification_status="verified")
            assert saved["first_fact"] == fact.to_dict()
    run_db(test)


def test_condition_result_is_a_scalar_in_hit_readback():
    from uuid import uuid4

    async def test(pool):
        rule_id = str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            hit = await store.append_hit(conn, {"evaluation_key": "condition-readback", "intent_id": None,
                "discovery_id": item["id"], "generation": 1, "rule_id": rule_id, "rule_version": 1,
                "selection": "matched", "action": "record_only", "reason": "matched",
                "verified_at": None, "mr_identity": KEY}, conditions=[{
                    "rule_position": 1, "condition_position": 1, "rule_id": rule_id, "rule_version": 1,
                    "kind": "internal_mr_open", "result": "passed", "reason": "passed", "verified_at": None}])
            result = await store.get_hit(conn, hit)
            assert result["conditions"] == [{"rule_position": 1, "condition_position": 1,
                "rule_id": rule_id, "rule_version": 1, "kind": "internal_mr_open", "result": "passed",
                "reason": "passed", "verified_at": None}]
    run_db(test)


def test_rule_attempts_are_closed_and_read_back_without_expanding_summaries():
    from uuid import uuid4

    async def test(pool):
        rule_id = str(uuid4())
        attempt = {"rule_id": rule_id, "rule_version": 1, "rule_position": 1,
                   "result": "failed", "reason": "scope_mismatch"}
        async with pool.acquire() as conn, conn.transaction():
            item = await discovery(conn)
            hit_value = {"evaluation_key": "attempt-readback", "intent_id": None,
                "discovery_id": item["id"], "generation": 1, "rule_id": None, "rule_version": None,
                "selection": "fallback", "action": "push_for_confirmation", "reason": "no_match",
                "verified_at": None, "mr_identity": KEY}
            with pytest.raises(ValueError):
                await store.append_hit(conn, hit_value, attempts=[{**attempt, "body": "untrusted"}])
            hit = await store.append_hit(conn, hit_value, attempts=[attempt])
            detail = await store.get_hit(conn, hit)
            assert detail["attempts"] == [attempt]
            assert "attempts" not in detail["hit"]
            assert "attempts" not in (await store.list_hits(conn))["hits"][0]
    run_db(test)
