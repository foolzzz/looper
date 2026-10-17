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

import agent_launch_parity
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
DROPPED = {"agent_configs": {"role", "max_concurrency"}, "agent_setting_layers": {"effort"}}
# What a later migration of main adds to a v2.6.0 database, not the rename's doing: columns (agent teams, on for its
# profiles, all on Claude Code runtimes), and built-in rows.
ADDED = {"agent_sessions": {"clean_start": False}, "agent_setting_layers": {"max_concurrency": None, "boundaries": None},
         "agents": {"agent_teams": True}}
ADDED_ROWS = {"llm_providers": {"prov-codex-login", "prov-grok-login"}}
UNCHANGED = ["agent_runtimes", "llm_providers", "llm_provider_secrets", "projects", "tasks"]
# Columns a later migration of main adds to those tables, empty on the rows from before.
NEW_COLUMNS = {"tasks": ("schedule_id", "budget_usd", "chat_id", "original_user_text", "title", "chat_kind",
                         "chat_mention"),
               "projects": ("step_workspaces", "step_workspace_links", "max_step_retries", "planner_roles",
                            "role_text_max_chars", "user_gates", "gate_reminder_hours")}
# What the effort's upgrade, a later migration of main, changes: each layer's effort moved onto the profiles its
# roles ran with it, so these layers and task choices select another profile (by name here; it changes their
# updated_at), and the profiles they take are added (EFFORT_PROFILES: model, effort).
EFFORT_MOVED = {
    "agent_setting_layers": {("system", None, None): "claude-code/glm/glm-5.3[1m] · high",
                             ("role", "planner", None): "claude-code/sonnet · high",
                             ("role", "reviewer", None): "beta/haiku · high",
                             ("role_project", "worker", parity.PROJECT_A): "claude-code/sonnet · low"},
    # Task A chose glm for its Workers, which ran with its project's Worker effort, and sonnet for its Reviewers,
    # which ran with the system's.
    "task_agent_overrides": {(parity.TASK_A, "worker"): "claude-code/glm/glm-5.3[1m] · low",
                             (parity.TASK_A, "reviewer"): "claude-code/sonnet · high"},
}
EFFORT_PROFILES = {"claude-code/glm/glm-5.3[1m] · high": ("glm-5.3[1m]", "high"),
                   "claude-code/glm/glm-5.3[1m] · low": ("glm-5.3[1m]", "low"),
                   "claude-code/sonnet · high": ("sonnet", "high"), "claude-code/sonnet · low": ("sonnet", "low"),
                   "beta/haiku · high": ("haiku", "high")}


def _effort_moved(table: str, rows: list[dict[str, Any]], names: dict[str, str], *, expected: bool,
                  ) -> list[dict[str, Any]]:
    """``rows`` of a table the effort's upgrade changes, with each profile by name; ``expected``: as it makes them."""
    moved = EFFORT_MOVED[table]
    out = []
    for row in rows:
        key = (row["scope"], row["role"], row["project_id"]) if "scope" in row else (row["task_id"], row["role"])
        name = moved[key] if expected and key in moved else names.get(row["profile_id"])
        out.append({k: v for k, v in {**row, "profile_id": name}.items() if not (key in moved and k == "updated_at")})
    return out


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

    async def _main() -> tuple[dict[str, Any], str]:
        async with _schema() as pool:
            await parity.seed_v2_6_0(pool)
            before = {old: await _rows(pool, old, key) for old, _, key, _ in RENAMED}
            unchanged = {table: await _rows(pool, table, "1") for table in UNCHANGED}

            await connection.ensure_schema(pool)

            names = {row["id"]: row["name"] for row in await _rows(pool, "agent_profiles", "id")}
            for old, new, key, columns in RENAMED:
                keyed = ", ".join(columns.get(k.strip(), k.strip()) for k in key.split(","))
                expected = [{**{columns.get(k, k): v for k, v in row.items() if k not in DROPPED.get(old, ())},
                             **ADDED.get(old, {})} for row in before[old]]
                after = await _rows(pool, new, keyed)
                if old == "agents":
                    added = [row for row in after if row["id"] not in {r["id"] for r in before[old]}]
                    assert {r["name"]: (r["model"], r["effort"]) for r in added} == EFFORT_PROFILES
                    after = [row for row in after if row not in added]
                if old in EFFORT_MOVED:
                    expected = _effort_moved(old, expected, names, expected=True)
                    after = _effort_moved(old, after, names, expected=False)
                assert after == expected, new
                assert expected, old  # the seed has rows in every renamed table
            # The dropped column only repeated the key.
            assert all(row["role"] == row["agent_id"] for row in before["agent_configs"])
            after_unchanged = {table: [row for row in await _rows(pool, table, "1")
                                       if row.get("id") not in ADDED_ROWS.get(table, ())] for table in UNCHANGED}
            for table, columns in NEW_COLUMNS.items():
                assert all(row.pop(column) is None for row in after_unchanged[table] for column in columns)
            assert after_unchanged == unchanged
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
                monkeypatch.delenv(agent_launch_parity.AGENT_TEAMS, raising=False)
                # The fixture predates agent teams, which its Claude Code profiles now have by default.
                captured = agent_launch_parity.set_aside_agent_teams(await parity.capture(root))
            return captured, await pool.fetchval("SELECT id FROM agent_profiles WHERE name = $1",
                                                 "claude-code/glm/glm-5.3[1m] · high")

    captured, glm_high = asyncio.run(_main())
    # The Manager runs glm with the system's effort high as before, through the profile that has it: the brief of
    # its new session names that profile's id.
    captured["manager"]["stdin"] = captured["manager"]["stdin"].replace(f"(agent {glm_high})",
                                                                        "(agent agent-glm000000001)")
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


def test_a_database_migrated_beside_other_schemas_is_shaped_like_a_database_of_its_own() -> None:
    """A schema check that looks in the whole database (``pg_constraint`` by name alone) skips what another schema
    of it already has. Next to a schema the schema was applied to, a v2.6.0 schema migrates to the shape a new
    database of its own gets, whatever else the shared test database holds."""
    import asyncpg

    from evals.prompt_regression.runner import fresh_database
    from looper.db.connection import ensure_schema

    async def _alone(url: str) -> dict[str, Any]:
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            return await _structure(pool)
        finally:
            await pool.close()

    async def _beside() -> dict[str, Any]:
        async with _schema() as neighbour, _schema() as migrated:
            await ensure_schema(neighbour)
            await parity.seed_v2_6_0(migrated)
            await ensure_schema(migrated)
            return await _structure(migrated)

    with fresh_database(str(DSN)) as (url, _):
        alone = asyncio.run(_alone(url))
    assert asyncio.run(_beside()) == alone
