"""The rename to the dashboard's names, on a v2.6.0 database: every row survives and every role runs as before.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.

The database is ``fixtures/schema_v2_6_0.sql`` (the v2.6.0 release's
schema) with ``profile_rename_parity.V2_6_0_ROWS``. This code's schema
renames its tables and columns in place; ``fixtures/profile_rename_parity_main.json``
is what the code before the rename (``main`` at 0c5d5ab8a) started each role
with on the same database (``python tests/profile_rename_parity.py <dsn>`` run
on that code), so it is not derived from the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import profile_rename_parity as parity

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

MAIN = json.loads((Path(__file__).parent / "fixtures" / "profile_rename_parity_main.json").read_text("utf-8"))

# Each renamed table before and after, with its key and the columns that were renamed (old -> new). A column the
# rename, or a later migration of main, drops is in DROPPED; every other column keeps its name and value.
RENAMED = [
    ("agent_configs", "role_configs", "agent_id", {"agent_id": "role"}),
    ("agents", "agent_profiles", "id", {}),
    ("task_agent_overrides", "task_profile_overrides", "task_id, role", {"agent_id": "profile_id"}),
    ("agent_setting_layers", "agent_setting_layers", "scope, role, project_id", {"agent_id": "profile_id"}),
    ("agent_sessions", "agent_sessions", "agent_id", {"session_agent_id": "profile_id"}),
]
DROPPED = {"agent_configs": {"role", "max_concurrency"}}
# What a later migration of main adds to a v2.6.0 database, not the rename's doing: columns, and a built-in row.
ADDED = {"agent_sessions": {"clean_start": False}, "agent_setting_layers": {"max_concurrency": None},
         "agents": {"agent_teams": False}}
ADDED_ROWS = {"llm_providers": {"prov-codex-login"}}
UNCHANGED = ["agent_runtimes", "llm_providers", "llm_provider_secrets", "projects", "tasks"]


@asynccontextmanager
async def _schema() -> AsyncIterator[Any]:
    import asyncpg

    name = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{name}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, server_settings={"search_path": name})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def _rows(pool: Any, table: str, key: str) -> list[dict[str, Any]]:
    return [json.loads(r["row"]) for r in await pool.fetch(f"SELECT to_jsonb(t) AS row FROM {table} t ORDER BY {key}")]


async def _structure(pool: Any) -> dict[str, list[tuple[Any, ...]]]:
    """The schema's tables and columns, constraints and indexes, by name: what a database of this schema is."""
    return {
        "columns": [tuple(r) for r in await pool.fetch(
            "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY 1, 2")],
        "constraints": [tuple(r) for r in await pool.fetch(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE connamespace = current_schema()::regnamespace ORDER BY 1, 2")],
        "indexes": [tuple(r) for r in await pool.fetch(
            "SELECT tablename, indexname, replace(indexdef, ' ' || current_schema() || '.', ' ') FROM pg_indexes "
            "WHERE schemaname = current_schema() ORDER BY 1, 2")],
    }


def test_a_v2_6_0_database_keeps_every_row_under_the_new_names_and_every_role_runs_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import connection

    async def _main() -> dict[str, Any]:
        async with _schema() as pool:
            await parity.seed_v2_6_0(pool)
            before = {old: await _rows(pool, old, key) for old, _, key, _ in RENAMED}
            unchanged = {table: await _rows(pool, table, "1") for table in UNCHANGED}

            await connection.ensure_schema(pool)

            for old, new, key, columns in RENAMED:
                keyed = ", ".join(columns.get(k.strip(), k.strip()) for k in key.split(","))
                expected = [{**{columns.get(k, k): v for k, v in row.items() if k not in DROPPED.get(old, ())},
                             **ADDED.get(old, {})} for row in before[old]]
                assert await _rows(pool, new, keyed) == expected, new
                assert expected, old  # the seed has rows in every renamed table
            # The dropped column only repeated the key.
            assert all(row["role"] == row["agent_id"] for row in before["agent_configs"])
            assert {table: [row for row in await _rows(pool, table, "1")
                            if row.get("id") not in ADDED_ROWS.get(table, ())] for table in UNCHANGED} == unchanged
            assert {row["id"] for row in await _rows(pool, "llm_providers", "1")} - {
                row["id"] for row in unchanged["llm_providers"]} == ADDED_ROWS["llm_providers"]
            for old in ("agent_configs", "agents", "task_agent_overrides"):
                assert await pool.fetchval("SELECT to_regclass($1)", old) is None
            assert await pool.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE name = 'profiles_roles_renamed'") == 1

            # Applying the schema again changes nothing.
            migrated = {new: await _rows(pool, new, "1") for _, new, _, _ in RENAMED}
            shape = await _structure(pool)
            await connection.ensure_schema(pool)
            assert {new: await _rows(pool, new, "1") for _, new, _, _ in RENAMED} == migrated
            assert await _structure(pool) == shape

            monkeypatch.setattr(connection, "_pool", pool)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                monkeypatch.setenv("HOME", str(root / "home"))
                monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
                return await parity.capture(root)

    captured = asyncio.run(_main())
    assert sorted(captured) == sorted(MAIN)
    for case in parity.CASES:
        assert captured[case] == MAIN[case], case


def test_a_migrated_v2_6_0_database_is_shaped_like_a_new_one() -> None:
    """Tables, columns, constraints and indexes carry the same names and definitions either way."""
    from looper.db.connection import ensure_schema

    async def _main() -> tuple[dict[str, Any], dict[str, Any]]:
        async with _schema() as old, _schema() as new:
            await parity.seed_v2_6_0(old)
            await ensure_schema(old)
            await ensure_schema(new)
            return await _structure(old), await _structure(new)

    migrated, fresh = asyncio.run(_main())
    assert migrated == fresh
    tables = {column[0] for column in fresh["columns"]}
    assert {"role_configs", "agent_profiles", "task_profile_overrides"} <= tables
    assert not {"agent_configs", "agents", "task_agent_overrides"} & tables
