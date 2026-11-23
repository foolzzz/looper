"""``dropped_tables``: the foreign keys a later table had into the dropped tables come back when the block ends."""
from __future__ import annotations

from typing import Any

import pytest

from tests.db_support import DSN, dropped_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_KEYS = ("SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND conrelid = to_regclass('later_notes') "
         "AND confrelid = to_regclass('base_items')")


async def _tables(pool: Any) -> None:
    await pool.execute("CREATE TABLE base_items (id BIGINT PRIMARY KEY); "
                       "CREATE TABLE later_notes (item_id BIGINT REFERENCES base_items(id))")


def test_keys_come_back_when_the_block_raises_after_the_tables_are_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _tables(pool)
        with pytest.raises(RuntimeError, match="the test body failed"):
            async with dropped_tables(pool, "base_items"):
                assert await pool.fetchval(_KEYS) == 0 and await pool.fetchval("SELECT to_regclass('base_items') IS NULL")
                await pool.execute("CREATE TABLE base_items (id BIGINT PRIMARY KEY)")  # the upgrade
                raise RuntimeError("the test body failed")
        assert await pool.fetchval(_KEYS) == 1

    run_db(check, monkeypatch)


def test_the_block_error_is_reported_when_the_tables_are_not_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _tables(pool)
        with pytest.raises(RuntimeError, match="the upgrade failed"):
            async with dropped_tables(pool, "base_items"):
                raise RuntimeError("the upgrade failed")
        assert await pool.fetchval(_KEYS) == 0

    run_db(check, monkeypatch)


def test_a_recreated_table_the_key_no_longer_fits_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(pool: Any) -> None:
        await _tables(pool)
        with pytest.raises(Exception, match="no unique constraint"):
            async with dropped_tables(pool, "base_items"):
                await pool.execute("CREATE TABLE base_items (id BIGINT)")  # an upgrade that lost the primary key

    run_db(check, monkeypatch)
