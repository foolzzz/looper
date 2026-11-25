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


def _fake_migration(name, order, *, online=()):
    async def pending(conn):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)

    async def apply(conn, *, sql, state):
        if not await pending(conn):
            return
        # Plain CREATE TABLE: a replay would raise, proving the guard suppressed it.
        await conn.execute(f"CREATE TABLE {name}_witness (id BIGSERIAL PRIMARY KEY)")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=online, pending=pending, apply=apply)


def test_ensure_schema_logs_one_timing_line_per_applied_item(caplog, monkeypatch):
    first = _fake_migration("timing_fake_first", 910001)
    online_step = OnlineStep(migration="timing_fake_second", marker="timing_fake_second_online")
    second = _fake_migration("timing_fake_second", 910002, online=(online_step,))
    registry = (first, second)
    monkeypatch.setattr(connection, "_MIGRATIONS", registry)
    monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(registry))
    monkeypatch.setattr(connection, "_ONLINE_STEPS", (online_step,))
    monkeypatch.setattr(connection, "_ONLINE_ORDERS",
                        {"timing_fake_second": second.online_order}, raising=False)

    async def test():
        async with fresh_connection() as conn:
            caplog.set_level(logging.INFO, logger=connection.logger.name)

            await connection.ensure_schema(conn)
            timing = [r.message for r in caplog.records
                      if r.levelname == "INFO" and r.message.startswith(("migration applied:",
                                                                          "online schema step applied:"))]
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
            assert not [r for r in caplog.records
                        if r.levelname == "INFO" and r.message.startswith(("migration applied:",
                                                                            "online schema step applied:"))]
            assert any(r.message == "schema ensured" and r.levelname == "INFO" for r in caplog.records)

    asyncio.run(test())
