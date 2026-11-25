"""A new migration file supplies fresh and upgrade paths without shared edits."""

import asyncio
import sys

import pytest

from looper.db import bridge_upgrade, connection, migrations
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_failure_handling_upgrades_after_higher_order_retrospectives(monkeypatch):
    from looper.db.migrations import step_failure_handling as failure

    async def test():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            previous = tuple(item for item in connection._MIGRATIONS if item.order <= 1910)
            await upgraded.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(), previous))
            await upgraded.execute("INSERT INTO registry_schema_version(id,version) VALUES(TRUE,$1)",
                                   connection.REGISTRY_SCHEMA_VERSION)
            await upgraded.execute("INSERT INTO schema_migrations(name) VALUES($1)", bridge_upgrade.BASELINE)
            # Apply the released retrospective migrations while 1920 is still absent.
            with monkeypatch.context() as patch:
                released = tuple(item for item in connection._MIGRATIONS if item.name != failure.name)
                patch.setattr(connection, "_MIGRATIONS", released)
                patch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(released))
                patch.setattr(connection, "_ONLINE_STEPS", migrations.online_steps(released))
                await connection.ensure_schema(upgraded)
                assert not await connection.schema_change_pending(upgraded)
            markers = {row["name"] for row in await upgraded.fetch("SELECT name FROM schema_migrations")}
            assert {"memory_retro_role", "memory_retro_role_online",
                    "memory_retro_queue", "memory_retro_queue_online"} <= markers
            assert failure.name not in markers
            assert await upgraded.fetchval("SELECT to_regclass('step_holds') IS NULL")
            await upgraded.execute("INSERT INTO projects(id,name) VALUES('upgrade-project','Kept project')")
            await upgraded.execute("INSERT INTO tasks(id,project_id,requirement) "
                                   "VALUES('upgrade-task','upgrade-project','Kept task')")
            await upgraded.execute("INSERT INTO memory_retro_cursors(task_id,last_to_id) VALUES('upgrade-task',17)")

            assert await connection.schema_change_pending(upgraded)
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert await upgraded.fetchval("SELECT requirement FROM tasks WHERE id='upgrade-task'") == "Kept task"
            assert await upgraded.fetchval("SELECT last_to_id FROM memory_retro_cursors "
                                           "WHERE task_id='upgrade-task'") == 17
            assert markers | {failure.name, failure.online[0].marker} == {
                row["name"] for row in await upgraded.fetch("SELECT name FROM schema_migrations")}
            assert not await connection.schema_change_pending(upgraded)
            saved = await witness(upgraded)
            await connection.ensure_schema(upgraded)
            assert await witness(upgraded) == saved

    asyncio.run(test())


_NEW_MIGRATION = '''\
name = "registry_example_items"
order = 10000
pending_order = 10000
fresh_ddl = "CREATE TABLE IF NOT EXISTS registry_example_items (id BIGSERIAL PRIMARY KEY, note TEXT NOT NULL DEFAULT '');"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('registry_example_items') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
'''


@pytest.mark.parametrize("with_retrospectives", [False, True], ids=["without-retrospectives", "released-retrospectives"])
def test_lesson_injection_upgrades_after_higher_order_retrospective_migrations(monkeypatch, with_retrospectives):
    from looper.db.migrations import memory_lesson_injection as injection
    from looper.db.migrations import memory_retro_queue, memory_retro_role

    registry = connection._MIGRATIONS
    retros = (memory_retro_role, memory_retro_queue)
    assert [m.name for m in registry if m.order == 1940] == [injection.name]
    assert [m.order for m in registry if m in (injection, *retros)] == [1940, 1980, 1990]

    def install(modules):
        monkeypatch.setattr(connection, "_MIGRATIONS", modules)
        monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(modules))
        monkeypatch.setattr(connection, "_ONLINE_STEPS", migrations.online_steps(modules))

    async def check():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            previous = tuple(m for m in registry if m.order <= 1910)
            await upgraded.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(), previous))
            await upgraded.execute("INSERT INTO registry_schema_version(id,version) VALUES(TRUE,$1)",
                                   connection.REGISTRY_SCHEMA_VERSION)
            await upgraded.execute("INSERT INTO schema_migrations(name) VALUES($1)", bridge_upgrade.BASELINE)
            # Apply 1920/1930 first; also cover a database predating the higher orders.
            install(tuple(m for m in registry if m not in (injection, *retros)))
            await connection.ensure_schema(upgraded)
            if with_retrospectives:
                install(tuple(m for m in registry if m is not injection))
                await connection.ensure_schema(upgraded)
            assert not await connection.schema_change_pending(upgraded)
            existing_markers = await upgraded.fetch("SELECT * FROM schema_migrations ORDER BY name")
            names = {row["name"] for row in existing_markers}
            assert {"step_failure_handling", "step_failure_handling_online", "morning_brief_send_started"} <= names
            retro_names = {m.name for m in retros} | {m.online[0].marker for m in retros}
            assert names & retro_names == (retro_names if with_retrospectives else set())
            await upgraded.execute("INSERT INTO system_settings(key,value) VALUES ('synthetic-upgrade','kept')")
            assert await upgraded.fetchval("SELECT to_regclass('task_lesson_snapshot_sets')") is None

            # Admission is read-only; a lower order addition must still run on a current DB.
            install(registry)
            before = await witness(upgraded)
            assert await connection.schema_change_pending(upgraded)
            assert await witness(upgraded) == before
            await connection.ensure_schema(upgraded)
            assert await upgraded.fetchval("SELECT count(*) FROM schema_migrations WHERE name = ANY($1)",
                [injection.name, injection.online[0].marker]) == 2
            assert await upgraded.fetch(
                "SELECT * FROM schema_migrations WHERE name = ANY($1) ORDER BY name",
                sorted(names)) == existing_markers
            assert await upgraded.fetchval(
                "SELECT value FROM system_settings WHERE key='synthetic-upgrade'") == "kept"
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert not await connection.schema_change_pending(upgraded)
            saved = await witness(upgraded)
            await connection.ensure_schema(upgraded)
            assert await witness(upgraded) == saved

    asyncio.run(check())


def test_new_migration_file_upgrades_and_initializes_the_same_schema(tmp_path, monkeypatch):
    module_name = "registry_test_example_items"
    (tmp_path / f"{module_name}.py").write_text(_NEW_MIGRATION)
    monkeypatch.setattr(migrations, "__path__", [*migrations.__path__, str(tmp_path)])

    async def test():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            # This installation was initialized before the new file was shipped.
            await connection.ensure_schema(upgraded)
            assert not await connection.schema_change_pending(upgraded)
            assert not await upgraded.fetchval("SELECT to_regclass('registry_example_items') IS NOT NULL")
            before, shape = await witness(upgraded), await structure(upgraded)

            discovered = migrations.discover()
            assert [migration.name for migration in discovered][-1] == "registry_example_items"
            monkeypatch.setattr(connection, "_MIGRATIONS", discovered)
            monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(discovered))
            monkeypatch.setattr(connection, "_ONLINE_STEPS", migrations.online_steps(discovered))

            # Backup admission discovers the addition but changes neither rows nor catalog.
            assert await connection.schema_change_pending(upgraded)
            assert await witness(upgraded) == before
            assert await structure(upgraded) == shape
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert {row["name"] for row in await upgraded.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE, "registry_example_items"}
            assert {row["name"] for row in await fresh.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}

            for conn in (upgraded, fresh):
                assert await conn.fetchval("INSERT INTO registry_example_items (note) VALUES ('kept') RETURNING id") == 1
                saved = await witness(conn)
                assert not await connection.schema_change_pending(conn)
                await connection.ensure_schema(conn)
                assert await witness(conn) == saved
                assert not await connection.schema_change_pending(conn)

    try:
        asyncio.run(asyncio.wait_for(test(), 30))
    finally:
        sys.modules.pop(f"{migrations.__name__}.{module_name}", None)


@pytest.mark.parametrize("migration_name,rollback", [
    ("skill_proposal_feedback", "ALTER TABLE skill_proposal_events DROP COLUMN told_at"),
    ("mcp_tool_levels", "DROP TABLE mcp_tool_levels CASCADE"),
    ("failure_typing", "ALTER TABLE llm_records DROP COLUMN failure_class, DROP COLUMN failure_cause"),
    ("memory_candidates", "DROP TABLE memory_candidates, memory_candidate_revisions, memory_candidate_events, "
                          "memory_candidate_sources, memory_posts, memory_tombstones CASCADE"),
    ("memory_draft_role", "ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check; "
                          "ALTER TABLE agent_setting_layers ADD CONSTRAINT agent_setting_layers_role_check "
                          "CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor', 'advisor', "
                          "'advice_check', 'leads_check', 'outbound_check', 'explorer'))"),
    ("memory_draft_queue", "DROP TABLE memory_draft_waits, memory_draft_lease"),
    ("channel_outbox", "DROP TABLE channel_outbox CASCADE"),
    ("manager_sessions", "DROP TABLE manager_sessions CASCADE"),
    ("scenario_session_routing", "ALTER TABLE managers DROP COLUMN session_routing"),
    ("provider_limit_windows", "DROP TABLE provider_limit_windows CASCADE"),
    ("provider_admission", "DROP TABLE provider_admission, provider_admission_events CASCADE"),
    ("cost_holds", "DROP TABLE cost_holds"),
    ("intent_recognitions", "ALTER TABLE intents DROP COLUMN recognition_id CASCADE; "
                            "DROP TABLE intent_recognitions CASCADE"),
    ("schedule_equivalent_budget", "ALTER TABLE schedules DROP COLUMN daily_budget_equivalent_usd"),
    ("intent_group_reads", "DROP TABLE intent_group_reads, intent_group_read_days; "
                           "ALTER TABLE intents DROP COLUMN via"),
])
def test_merged_migrations_remain_discoverable_and_upgrade(migration_name, rollback):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(rollback)
            await conn.execute("DELETE FROM schema_migrations WHERE name = ANY($1)",
                               [migration_name, f"{migration_name}_online"])
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)",
                                       migration_name)

    asyncio.run(test())


@pytest.mark.parametrize("missing_marker", [False, True])
def test_room_delivery_resumes_validation_without_replacing_constraint(missing_marker):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE room_messages DROP CONSTRAINT room_messages_kind_check, "
                "ADD CONSTRAINT room_messages_kind_check CHECK (kind IN "
                "('info','question','answer','interface','announce','notice','escalation'))")
            async with conn.transaction():
                await connection.ensure_schema(conn)
            constraint = await conn.fetchrow("SELECT oid,convalidated FROM pg_constraint "
                "WHERE conrelid='room_messages'::regclass AND conname='room_messages_kind_check'")
            assert not constraint["convalidated"]
            if missing_marker:
                await conn.execute("DELETE FROM schema_migrations WHERE name='subagent_room_delivery'")
            await connection.ensure_schema(conn)
            validated = await conn.fetchrow("SELECT oid,convalidated FROM pg_constraint "
                "WHERE conrelid='room_messages'::regclass AND conname='room_messages_kind_check'")
            assert validated["oid"] == constraint["oid"] and validated["convalidated"]
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                      "('subagent_room_delivery','subagent_room_delivery_online')") == 2
            assert not await connection.schema_change_pending(conn)

    asyncio.run(test())


@pytest.mark.parametrize("rollback", ["ALTER TABLE intents DROP CONSTRAINT intents_on_request",
                                     "ALTER TABLE intents DROP COLUMN recognition_id CASCADE"])
def test_recognition_repairs_incomplete_schema_with_its_marker_present(rollback):
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO schema_migrations(name) VALUES('intent_recognitions') "
                               "ON CONFLICT DO NOTHING")
            await conn.execute(rollback)
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='intents'::regclass AND conname='intents_on_request'")
            assert "on_request" in definition and "recognition_id IS NOT NULL" in definition
    asyncio.run(check())


def test_recognition_upgrade_preserves_existing_intent_via_values():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE intents DROP CONSTRAINT intents_via_check; "
                "ALTER TABLE intents ADD CONSTRAINT intents_via_check "
                "CHECK (via IN ('subscription', 'followed_group', 'future_source'))")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='intents'::regclass AND conname='intents_via_check'")
            for via in ("subscription", "followed_group", "future_source", "on_request"):
                assert f"'{via}'" in definition
            assert not await connection.schema_change_pending(conn)
    asyncio.run(check())


@pytest.mark.parametrize("replacement", [
    "",
    "CREATE INDEX idx_intent_recognitions_running ON intent_recognitions(source_id) WHERE state='running'",
    "CREATE UNIQUE INDEX idx_intent_recognitions_running ON intent_recognitions(chat_id) WHERE state='running'",
    "CREATE UNIQUE INDEX idx_intent_recognitions_running ON intent_recognitions(source_id) WHERE state='done'",
    "CREATE UNIQUE INDEX idx_intent_recognitions_running ON intent_recognitions(source_id)",
], ids=["missing", "not_unique", "wrong_column", "wrong_predicate", "not_partial"])
def test_recognition_repairs_running_index_with_its_marker_present(replacement):
    from looper.db.migrations import intent_recognitions

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO schema_migrations(name) VALUES('intent_recognitions') "
                               "ON CONFLICT DO NOTHING")
            assert not await intent_recognitions.pending(conn)
            expected = await conn.fetchval("SELECT pg_get_indexdef('idx_intent_recognitions_running'::regclass)")
            await conn.execute("DROP INDEX idx_intent_recognitions_running")
            if replacement:
                await conn.execute(replacement)
            assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                       "WHERE name='intent_recognitions')")
            assert await intent_recognitions.pending(conn)
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT pg_get_indexdef('idx_intent_recognitions_running'::regclass)") == expected
            assert not await intent_recognitions.pending(conn)
            assert not await connection.schema_change_pending(conn)
    asyncio.run(check())


def test_equivalent_budget_migration_repairs_column_even_with_marker():
    async def test():
        from looper.db.migrations import schedule_equivalent_budget as migration
        assert migration.order == migration.pending_order == 1600
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", migration.name)
            await conn.execute("ALTER TABLE schedules DROP COLUMN daily_budget_equivalent_usd")
            assert await migration.pending(conn)
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
    asyncio.run(test())
