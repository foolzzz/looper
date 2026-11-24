"""Execution counters come from the persisted rule execution ledger."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from looper.db import intent_rules as store
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize("reason", [None, "mr_missing", "mr_not_open", "verification_unavailable"])
def test_execution_statistics_count_verification_failures_without_creations(reason):
    async def test(pool):
        rule_id = str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            discovery = await store.create_discovery(conn, discovery_source_id="source", source_ref="statistics",
                system_id="review", mr_url="https://review.example/repo/mr/1")
            await store.append_hit(conn, {
                "evaluation_key": "synthetic-evaluation", "intent_id": None, "discovery_id": discovery["id"],
                "generation": 0, "rule_id": rule_id, "rule_version": 1, "selection": "matched",
                "action": "create_task", "reason": reason, "verified_at": None, "mr_identity": None,
            })
        now = datetime.now(timezone.utc)
        stats = await store.hit_stats(pool, since=now - timedelta(days=1), until=now + timedelta(days=1))
        assert stats["available"] is True
        assert stats["rules"] == [{
            "rule_id": rule_id, "version": 1, "hits": 1, "verification_unavailable": int(reason == "verification_unavailable"),
            "auto_created": 0, "deduped": 0, "daily_cap": 0,
        }]

    run_db(test)


def test_unavailable_attempts_are_attributed_once_to_the_attempted_rule():
    async def test(pool):
        rule_id, other_rule = str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn, conn.transaction():
            for index, created_at in enumerate((now, now - timedelta(days=2))):
                discovery = await store.create_discovery(conn, discovery_source_id="source", source_ref=f"attempt-{index}",
                    system_id="review", mr_url="https://review.example/repo/mr/1")
                conditions = [{"rule_position": 1, "condition_position": position, "rule_id": rule_id,
                    "rule_version": 1, "kind": kind, "result": "unavailable",
                    "reason": "verification_unavailable", "verified_at": None}
                    for position, kind in ((1, "internal_mr_open"), (2, "operator_is_reviewer"))]
                hit_id = await store.append_hit(conn, {
                    "evaluation_key": f"attempt-{index}", "intent_id": None, "discovery_id": discovery["id"],
                    "generation": 0, "rule_id": None, "rule_version": None, "selection": "fallback",
                    "action": "push_for_confirmation", "reason": "verification_unavailable",
                    "verified_at": None, "mr_identity": None,
                }, conditions=conditions)
                await conn.execute("UPDATE intent_rule_hits SET created_at=$2 WHERE id=$1", hit_id, created_at)
        expected = {"rule_id": rule_id, "version": 1, "hits": 0, "verification_unavailable": 1,
                    "auto_created": 0, "deduped": 0, "daily_cap": 0}
        filters = dict(since=now - timedelta(days=1), until=now + timedelta(days=1))
        selected = await store.hit_stats(pool, rule_id=rule_id, **filters)
        assert selected["rules"] == [expected]
        assert (await store.hit_stats(pool, rule_id=other_rule, **filters))["rules"] == []
        all_rules = (await store.hit_stats(pool, **filters))["rules"]
        assert next(row for row in all_rules if row["rule_id"] == rule_id) == expected
        # The fallback remains an action record; it must not double-count its rule's failure.
        fallback = next(row for row in all_rules if row["rule_id"] is None)
        assert fallback["hits"] == 1 and fallback["verification_unavailable"] == 0
    run_db(test)
