"""Agent settings, harness columns and role-prompt seeding against a real PostgreSQL.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    """Run ``test(pool)`` on a new schema; with ``monkeypatch`` the pool is also the server's pool."""
    from loopx.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            if monkeypatch is not None:
                monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


async def _columns(pool: Any, table: str) -> dict[str, tuple[str, str, str | None]]:
    rows = await pool.fetch(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1", table,
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"]) for r in rows}


def test_the_new_table_and_columns_are_created_idempotently() -> None:
    from loopx.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await ensure_schema(pool)  # a second startup changes nothing
        settings = await _columns(pool, "system_settings")
        assert settings["key"][:2] == ("text", "NO") and settings["value"][:2] == ("text", "NO")
        assert settings["updated_at"][:2] == ("timestamp with time zone", "NO")
        primary = await pool.fetchval(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'system_settings'::regclass AND i.indisprimary"
        )
        assert primary == "key"
        assert (await _columns(pool, "agent_configs"))["harness_prompt"] == ("text", "NO", "''::text")
        projects = await _columns(pool, "projects")
        assert projects["planner_harness_prompt"] == ("text", "NO", "''::text")
        assert projects["worker_harness_prompt"] == ("text", "NO", "''::text")

    _run(_test)


def test_system_settings_store_replace_and_remove() -> None:
    from loopx.db import system_settings

    async def _test(pool: Any) -> None:
        assert await system_settings.get_settings(["agent.model"], pool=pool) == {}
        await system_settings.put_settings({"agent.model": "sonnet", "agent.effort": "high"}, pool=pool)
        await system_settings.put_settings({"agent.model": "opus"}, pool=pool)
        assert await system_settings.get_settings(["agent.model", "agent.effort", "other"], pool=pool) == {
            "agent.model": "opus", "agent.effort": "high"}
        await system_settings.put_settings({"agent.effort": None}, pool=pool)
        assert await system_settings.get_settings(["agent.effort"], pool=pool) == {}
        assert await pool.fetchval("SELECT count(*) FROM system_settings") == 1

    _run(_test)


def test_the_settings_api_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    from loopx.agents import agent_settings

    async def _test(pool: Any) -> None:
        view = await agent_settings.get_settings()
        assert (view["harness_source"], view["default_model"], view["default_env"]) == ("builtin_default", "opus", [])
        env = [{"key": "TZ", "value": "UTC"}]
        view = await agent_settings.update_settings({
            "default_harness_prompt": "Mine.", "default_model": "sonnet", "default_effort": "xhigh", "default_env": env,
        })
        assert (view["default_harness_prompt"], view["default_model"], view["default_effort"]) == (
            "Mine.", "sonnet", "xhigh")
        stored = {r["key"]: r["value"] for r in await pool.fetch("SELECT key, value FROM system_settings")}
        assert stored["agent.harness_prompt"] == "Mine." and json.loads(stored["agent.env"]) == env
        defaults = await agent_settings.load_defaults()
        assert (defaults.harness, defaults.model, defaults.effort, defaults.env) == (
            "Mine.", "sonnet", "xhigh", (("TZ", "UTC"),))
        view = await agent_settings.reset_harness()
        assert view["harness_source"] == "builtin_default" and view["default_model"] == "sonnet"
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = 'agent.harness_prompt'") == 0

    _run(_test, monkeypatch)


def test_the_managers_harness_and_the_projects_harnesses_persist() -> None:
    from loopx.db import agent_configs, projects

    async def _test(pool: Any) -> None:
        await agent_configs.seed_defaults([{"agent_id": "manager", "role": "manager", "system_prompt": "M."}],
                                          pool=pool)
        assert (await agent_configs.get_config("manager", pool=pool))["harness_prompt"] == ""
        updated = await agent_configs.set_config("manager", {"harness_prompt": "Be brief."}, pool=pool)
        assert updated is not None and updated["harness_prompt"] == "Be brief."

        made = await projects.create_project(name="calc", planner_harness_prompt="Plan.", pool=pool)
        assert (made["planner_harness_prompt"], made["worker_harness_prompt"]) == ("Plan.", "")
        assert await projects.update_project(made["id"], worker_harness_prompt="Work.", pool=pool)
        row = await projects.get_project(made["id"], pool=pool)
        assert (row["planner_harness_prompt"], row["worker_harness_prompt"]) == ("Plan.", "Work.")
        assert await projects.update_project(made["id"], planner_harness_prompt="", pool=pool)
        assert (await projects.get_project(made["id"], pool=pool))["planner_harness_prompt"] == ""

    _run(_test)

