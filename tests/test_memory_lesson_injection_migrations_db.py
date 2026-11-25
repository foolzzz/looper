"""Independent lesson delivery migration upgrades, repairs and conservative history handling."""
import asyncio

import pytest

from looper.db import connection, migrations
from looper.db.bridge_upgrade import BASELINE
from looper.db.migrations import memory_lesson_injection as migration
from tests.bridge_schema_support import structure
from tests.db_support import DSN,fresh_connection,run_db

pytestmark = pytest.mark.skipif(not DSN,reason='requires disposable LOOPER_TEST_DATABASE_URL')


def test_snapshot_migration_is_independent_of_retro():
    async def check():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            old = tuple(m for m in connection._MIGRATIONS if m.name != migration.name)
            await upgraded.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(),old))
            await upgraded.execute('INSERT INTO registry_schema_version(id,version) VALUES(TRUE,$1)',connection.REGISTRY_SCHEMA_VERSION)
            await upgraded.execute('INSERT INTO schema_migrations(name) VALUES ($1)',BASELINE)
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert {row[0] for row in await fresh.fetch('SELECT name FROM schema_migrations')} == {BASELINE}
            assert not await migration.pending(upgraded)
            await upgraded.execute('ALTER TABLE task_lesson_snapshot_sets DROP COLUMN omitted')
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            assert not await migration.pending(upgraded)
    asyncio.run(check())


def test_delivery_schema_repairs_missing_objects_under_marker():
    async def check(pool):
        async with pool.acquire() as conn:
            await conn.execute('INSERT INTO schema_migrations(name) VALUES ($1),($2) ON CONFLICT DO NOTHING',migration.name,migration.online[0].marker)
            await conn.execute('DROP TABLE task_lesson_planner_state; DROP INDEX idx_memory_injections_delivery; '
                'ALTER TABLE memory_injection_items DROP CONSTRAINT memory_injection_items_position_check')
            assert await migration.pending(conn)
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
    run_db(check)


def test_legacy_nonempty_snapshots_are_not_given_invented_history():
    async def check():
        async with fresh_connection() as conn:
            old = tuple(m for m in connection._MIGRATIONS if m.name != migration.name)
            await conn.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(),old))
            await conn.execute("INSERT INTO managers(id,name,tools,task_scope,idle_timeout_seconds) "
                "VALUES ('manager','Synthetic manager','all','all',0); "
                "INSERT INTO projects(id,name) VALUES ('p','Synthetic'); "
                "INSERT INTO tasks(id,requirement,project_id,manager_id) VALUES ('t','Synthetic','p','manager'); "
                "INSERT INTO memory_candidates(kind,source_kind,source_class,destination,state,reason,statement,fingerprint,project_id,valid_until) "
                "VALUES ('lesson','task','agent','looper','held','shadow','Synthetic',repeat('a',64),'p',now()+interval '90 days'); "
                "INSERT INTO task_lesson_snapshots(task_id,candidate_id,position) SELECT 't',id,1 FROM memory_candidates")
            with pytest.raises(RuntimeError,match='legacy lesson snapshots'):
                await migration.pending(conn)
    asyncio.run(check())
