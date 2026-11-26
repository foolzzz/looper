"""One timing log line per migration or online step that actually runs."""

import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

from looper.db import connection, migrations
from looper.db.migrations import OnlineStep
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_DURATION = re.compile(r" in \d+\.\d{3}s$")
_TIMING_PREFIXES = ("migration applied:", "online schema step applied:")


def _timing_lines(records):
    return [r.message for r in records
            if r.levelname == "INFO" and r.message.startswith(_TIMING_PREFIXES)]


def _guarded_migration(name, order, *, online=()):
    """The common shape: apply performs work exactly while the marker is missing."""

    async def pending(conn):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)

    async def apply(conn, *, sql, state) -> bool:
        if not await pending(conn):
            return False
        # Plain CREATE TABLE: a replay would raise, proving the guard suppressed it.
        await conn.execute(f"CREATE TABLE {name}_witness (id BIGSERIAL PRIMARY KEY)")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
        return True

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=online, pending=pending, apply=apply)


def _repair_branch_migration(name, order):
    """The catalog probe stays false while apply still has a branch that does work.

    Mirrors the real migrations whose apply validates constraints or repairs markers
    after ``pending()`` comes back false (event_triage, leads_check, run_skips and the
    other constraint-repair migrations).
    """

    async def pending(conn):
        # The baseline catalog already satisfies the probe on every startup.
        return False

    async def apply(conn, *, sql, state) -> bool:
        # The pending=false repair branch: create the witness once and settle the marker.
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"{name}_witness"):
            return False
        await conn.execute(f"CREATE TABLE {name}_witness (id BIGSERIAL PRIMARY KEY)")
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        return True

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=(), pending=pending, apply=apply)


def _noop_migration(name, order):
    """A genuinely settled migration: pending false and apply never touches the database."""

    async def pending(conn):
        return False

    async def apply(conn, *, sql, state) -> bool:
        return False

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=(), pending=pending, apply=apply)


def _patch_registry(monkeypatch, registry, *, online_steps=(), online_orders=None):
    monkeypatch.setattr(connection, "_MIGRATIONS", registry)
    monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(registry))
    monkeypatch.setattr(connection, "_ONLINE_STEPS", online_steps)
    monkeypatch.setattr(connection, "_ONLINE_ORDERS", online_orders or {}, raising=False)


def test_ensure_schema_logs_one_timing_line_per_applied_item(caplog, monkeypatch):
    first = _guarded_migration("timing_fake_first", 910001)
    online_step = OnlineStep(migration="timing_fake_second", marker="timing_fake_second_online")
    second = _guarded_migration("timing_fake_second", 910002, online=(online_step,))
    _patch_registry(monkeypatch, (first, second), online_steps=(online_step,),
                    online_orders={"timing_fake_second": second.online_order})

    async def test():
        async with fresh_connection() as conn:
            caplog.set_level(logging.INFO, logger=connection.logger.name)

            await connection.ensure_schema(conn)
            timing = _timing_lines(caplog.records)
            # Exactly one line per applied item: two guarded migrations and one online step.
            assert len(timing) == 3
            assert [message.split(" in ")[0] for message in timing] == [
                "migration applied: timing_fake_first order=910001",
                "migration applied: timing_fake_second order=910002",
                "online schema step applied: timing_fake_second_online order=910002",
            ]
            assert all(_DURATION.search(message) for message in timing)
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name = ANY($1)",
                                       ["timing_fake_first", "timing_fake_second",
                                        "timing_fake_second_online"]) == 3
            assert any(r.message == "schema ensured" and r.levelname == "INFO" for r in caplog.records)

            # A second start applies nothing: no timing lines and no replay of the DDL.
            caplog.clear()
            await connection.ensure_schema(conn)
            assert not _timing_lines(caplog.records)
            assert any(r.message == "schema ensured" and r.levelname == "INFO" for r in caplog.records)

    asyncio.run(test())


def test_ensure_schema_logs_work_done_on_a_pending_false_branch(caplog, monkeypatch):
    # A no-work migration stays silent alongside one whose apply works while pending() is false.
    repair = _repair_branch_migration("timing_fake_repair", 910003)
    noop = _noop_migration("timing_fake_noop", 910004)
    _patch_registry(monkeypatch, (repair, noop))

    async def test():
        async with fresh_connection() as conn:
            caplog.set_level(logging.INFO, logger=connection.logger.name)
            # The probe is satisfied throughout, exactly as it is for the real repair branches.
            assert not await repair.pending(conn)

            await connection.ensure_schema(conn)
            timing = _timing_lines(caplog.records)
            # Exactly one line, for the migration that actually worked; the no-op stays silent.
            assert len(timing) == 1
            assert timing[0].split(" in ")[0] == "migration applied: timing_fake_repair order=910003"
            assert _DURATION.search(timing[0])
            assert await conn.fetchval("SELECT to_regclass('timing_fake_repair_witness') IS NOT NULL")
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", "timing_fake_repair")

            # The repair is now settled: a second start runs neither branch and logs nothing.
            caplog.clear()
            await connection.ensure_schema(conn)
            assert not _timing_lines(caplog.records)

    asyncio.run(test())


# apply's bool is what the startup timing line follows. These migrations gained that
# result after the first conversion; pin both values, not only the annotation.
_APPLY_RESULT_MIGRATIONS = (
    "task_origin_session",
    "task_drafts",
    "task_work_list",
    "task_select_reads",
    "task_select_directories",
    "task_manager_role",
    "proactive_bodies",
    "main_session_egress",
    "completion_session_allowlist",
    "network_pauses",
    "chat_message_entry_kind",
)


def _prefix_registry(migration):
    earlier = tuple(item for item in connection._MIGRATIONS if item.order < migration.order)
    return earlier, migrations.online_steps(earlier), {
        item.name: item.online_order for item in earlier if getattr(item, "online", ())}


async def _validate_online(conn, migration):
    """Settle NOT VALID checks the way the post-commit online phase does.

    A second ``apply`` still reports work while ``_unvalidated`` is true
    (``main_session_egress``, ``completion_session_allowlist``). Validation is
    what makes the next startup's ``apply`` return false.
    """
    for step in getattr(migration, "online", ()) or ():
        for table, constraint in step.validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")


@pytest.mark.parametrize("migration_name", _APPLY_RESULT_MIGRATIONS)
def test_apply_returns_true_only_while_pending(migration_name, monkeypatch):
    migration = next(item for item in connection._MIGRATIONS if item.name == migration_name)
    earlier, online_steps, online_orders = _prefix_registry(migration)

    async def test():
        async with fresh_connection(command_timeout=60) as conn:
            _patch_registry(monkeypatch, earlier, online_steps=online_steps, online_orders=online_orders)
            await connection.ensure_schema(conn)
            # apply reads the real online steps (the unvalidated repair looks them up).
            monkeypatch.undo()
            # The baseline CREATE TABLE already has this column, so a fresh database is
            # not pending. Dropping it is the upgrade pending() describes.
            if migration.name == "task_origin_session":
                await conn.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS origin_session")
                await conn.execute("DELETE FROM schema_migrations WHERE name = $1", migration.name)
            assert await migration.pending(conn) is True
            assert await migration.apply(conn, sql="", state="current") is True
            await _validate_online(conn, migration)
            assert await migration.apply(conn, sql="", state="current") is False

    asyncio.run(test())
