"""Unavailable execution counters stay unknown even when evaluations exist."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from looper.db import intent_rules as store
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize("reason", [None, "verification_unavailable"])
def test_execution_statistics_are_null_without_an_executor(reason):
    async def test(pool):
        rule_id = str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            await store.append_hit(conn, {
                "evaluation_key": "synthetic-evaluation", "intent_id": None, "discovery_id": 1,
                "generation": 0, "rule_id": rule_id, "rule_version": 1, "selection": "matched",
                "action": "create_task", "reason": reason, "verified_at": None, "mr_identity": None,
            })
        now = datetime.now(timezone.utc)
        stats = await store.hit_stats(pool, since=now - timedelta(days=1), until=now + timedelta(days=1))
        assert stats["available"] is False
        assert stats["rules"] == [{
            "rule_id": rule_id, "version": 1, "hits": 1, "verification_unavailable": None,
            "auto_created": None, "deduped": None, "daily_cap": None,
        }]

    run_db(test)
