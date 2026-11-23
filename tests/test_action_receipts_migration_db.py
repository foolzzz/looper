"""The ``action_receipts`` migration: a fresh database and an upgrade end up with the same two nullable columns and
validated checks, and an old row keeps NULLs."""

import asyncio

import asyncpg
import pytest

from looper.db import connection
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

CHECKS = ("manager_actions_category_check", "manager_actions_receipt_check")


async def _columns(conn):
    return {row["column_name"]: (row["is_nullable"], row["column_default"]) for row in await conn.fetch(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'manager_actions' AND column_name IN ('category', 'receipt')")}


async def _validated(conn):
    return {row["conname"]: row["convalidated"] for row in await conn.fetch(
        "SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'manager_actions'::regclass "
        "AND conname = ANY($1)", list(CHECKS))}


async def _insert(conn, **values):
    columns = ", ".join(["tool", "risk", "status", *values])
    marks = "".join(f", ${i}" for i in range(1, len(values) + 1))
    await conn.execute(f"INSERT INTO manager_actions ({columns}) VALUES ('looper_projects', 'write', 'ok'{marks})",
                       *values.values())


def test_a_fresh_database_has_both_columns_without_defaults_and_validated_checks():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert await _columns(conn) == {"category": ("YES", None), "receipt": ("YES", None)}
            assert await _validated(conn) == {name: True for name in CHECKS}
            await _insert(conn)
            await _insert(conn, category="remove", receipt="✅ 已删除项目「demo」")
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(conn, category="delete")
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert(conn, category="update", receipt="x" * 301)
            await _insert(conn, category="update", receipt="x" * 300)
            assert not await connection.schema_change_pending(conn)

    asyncio.run(test())


def test_an_upgrade_adds_the_columns_unvalidated_then_validates_them_online():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE manager_actions DROP COLUMN category, DROP COLUMN receipt")
            await conn.execute("DELETE FROM schema_migrations WHERE name IN ('action_receipts', 'action_receipts_online')")
            await _insert(conn)
            assert await connection.schema_change_pending(conn)
            async with conn.transaction():  # the DDL runs in the caller's transaction; online steps defer
                await connection.ensure_schema(conn)
            assert await _columns(conn) == {"category": ("YES", None), "receipt": ("YES", None)}
            assert await _validated(conn) == {name: False for name in CHECKS}
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await _validated(conn) == {name: True for name in CHECKS}
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                       "('action_receipts', 'action_receipts_online')") == 2
            assert await conn.fetchval("SELECT count(*) FROM manager_actions WHERE category IS NULL "
                                       "AND receipt IS NULL") == 1

    asyncio.run(test())
