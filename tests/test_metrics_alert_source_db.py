"""The metrics alert source composes with independently shipped source widenings."""

import asyncio
import re

import asyncpg
import pytest

from looper.db import bridge_upgrade, connection
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


async def _source_check(conn):
    return await conn.fetchrow(
        "SELECT oid, convalidated, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid='proactive_deliveries'::regclass AND conname='proactive_deliveries_source_check'")


def test_fresh_schema_admits_metrics_alert_with_only_the_baseline_marker():
    async def test():
        from looper.db.migrations import metrics_alert_source as migration

        assert migration.order == 1420
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}
            assert not await migration.pending(conn)
            assert not await connection.schema_change_pending(conn)
            assert (await _source_check(conn))["convalidated"]
            await conn.execute(
                "INSERT INTO proactive_deliveries(source,channel_id,chat,chat_kind) "
                "VALUES ('metrics_alert','synthetic-channel','synthetic-chat','direct')")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE proactive_deliveries SET source='unknown_source'")

    asyncio.run(test())


@pytest.mark.parametrize("recorded", [False, True])
def test_widening_preserves_current_sources_and_validates_after_commit(recorded):
    async def test():
        from looper.db.migrations import metrics_alert_source as migration

        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            current = await _source_check(conn)
            sources = re.findall(r"'([a-z_]+)'", current["definition"])
            sources = [source for source in sources if source != "metrics_alert"]
            # These source widenings may have been installed in another branch/order.
            for source in ("morning_brief", "idle_check", "triage"):
                if source not in sources:
                    sources.append(source)
            values = ",".join(f"'{source}'" for source in sources)
            await conn.execute("ALTER TABLE proactive_deliveries DROP CONSTRAINT "
                               "proactive_deliveries_source_check")
            await conn.execute("ALTER TABLE proactive_deliveries ADD CONSTRAINT prior_source_check "
                               f"CHECK(source IN ({values}))")
            await conn.execute("ALTER TABLE proactive_deliveries ADD CONSTRAINT source_decoy "
                               "CHECK(source_ref IS NULL OR source_ref <> 'metrics_alert')")
            if recorded:
                await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1),($2)",
                                   migration.name, migration.online[0].marker)
            for source in sources:
                await conn.execute("INSERT INTO proactive_deliveries(source,channel_id,chat,chat_kind) "
                                   "VALUES ($1,'synthetic-channel','synthetic-chat','direct')", source)
            before = await conn.fetch("SELECT * FROM proactive_deliveries ORDER BY id")
            assert await migration.pending(conn)
            assert await connection.schema_change_pending(conn)
            async with conn.transaction():
                await migration.apply(conn, sql="", state="current")
                constraint = await _source_check(conn)
                assert not constraint["convalidated"]
            assert not await migration.pending(conn)
            assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name=$1)",
                                           migration.online[0].marker)
            await connection.ensure_schema(conn)
            validated = await _source_check(conn)
            assert validated["convalidated"] and validated["oid"] == constraint["oid"]
            assert set(re.findall(r"'([a-z_]+)'", validated["definition"])) == {*sources, "metrics_alert"}
            assert await conn.fetch("SELECT * FROM proactive_deliveries ORDER BY id") == before
            assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint "
                                       "WHERE conrelid='proactive_deliveries'::regclass AND conname='source_decoy')")
            await conn.execute("INSERT INTO proactive_deliveries(source,channel_id,chat,chat_kind) "
                               "VALUES ('metrics_alert','synthetic-channel','synthetic-chat','direct')")
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert (await _source_check(conn))["oid"] == validated["oid"]

    asyncio.run(test())
