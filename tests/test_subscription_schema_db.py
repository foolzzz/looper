"""Daily subscription upgrades match fresh schemas and respect recorded migrations."""

import pytest

from looper.db import connection
from tests.bridge_schema_support import structure
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

MISSING_DDL = (
    'ALTER TABLE subscriptions DROP COLUMN "window"',
    "ALTER TABLE subscriptions DROP COLUMN delivery_policy",
    "ALTER TABLE subscription_runs DROP COLUMN window_mode",
    "ALTER TABLE subscription_runs DROP COLUMN daily_date",
    "DROP TABLE subscription_daily_claims",
)


@pytest.mark.parametrize("ddl", [*MISSING_DDL, "; ".join(MISSING_DDL)],
                         ids=["window", "delivery-policy", "window-mode", "daily-date", "claims", "all"])
def test_unmarked_daily_upgrade_matches_fresh_even_with_some_objects_present(monkeypatch, ddl):
    async def check(pool):
        async with pool.acquire() as conn:
            expected = await structure(conn)
            assert not await connection.schema_change_pending(conn)
            await conn.execute(ddl)
            await conn.execute("DELETE FROM schema_migrations WHERE name = 'subscription_lookback'")
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations "
                                       "WHERE name = 'subscription_lookback'") == 1
            assert await structure(conn) == expected

    run_db(check, monkeypatch, timeout=20)


def test_recorded_daily_upgrade_does_not_repair_missing_objects(monkeypatch):
    async def check(pool):
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ('subscription_lookback') "
                               "ON CONFLICT DO NOTHING")
            await conn.execute("ALTER TABLE subscription_runs DROP COLUMN daily_date; "
                               "DROP TABLE subscription_daily_claims")
            expected = await structure(conn)
            assert not await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
            assert await structure(conn) == expected

    run_db(check, monkeypatch, timeout=20)
