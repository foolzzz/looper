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
        await agent_settings.update_settings({"default_effort": "high"})  # replaces its row, adds none
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = 'agent.effort'") == 1
        defaults = await agent_settings.load_defaults()
        assert (defaults.harness, defaults.model, defaults.effort, defaults.env) == (
            "Mine.", "sonnet", "high", (("TZ", "UTC"),))
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


def test_seeding_moves_unedited_role_prompts_keeps_edited_ones_and_adds_the_reviewer() -> None:
    from loopx.agents.serve import seed_agent_configs
    from loopx.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT as MANAGER
    from loopx.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER
    from loopx.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER, REVIEWER_SYSTEM_PROMPT as REVIEWER
    from loopx.db import agent_configs

    async def _test(pool: Any) -> None:
        # Rows as the previous release left them: manager and worker unedited, planner edited.
        for agent_id, prompt, recommended in [
            ("manager", "previous manager recommendation", "previous manager recommendation"),
            ("planner", "My own planner prompt.", "previous planner recommendation"),
            ("worker", "previous worker recommendation", "previous worker recommendation"),
        ]:
            await pool.execute(
                "INSERT INTO agent_configs (agent_id, role, system_prompt, default_prompt, model) "
                "VALUES ($1, $1, $2, $3, $4)", agent_id, prompt, recommended, "sonnet" if agent_id == "worker" else None,
            )
        await seed_agent_configs(pool)

        rows = {agent_id: await agent_configs.get_config(agent_id, pool=pool)
                for agent_id in ("manager", "planner", "worker", "reviewer")}
        assert (rows["manager"]["system_prompt"], rows["manager"]["default_prompt"]) == (MANAGER, MANAGER)
        assert (rows["worker"]["system_prompt"], rows["worker"]["default_prompt"]) == (WORKER, WORKER)
        assert rows["worker"]["model"] == "sonnet"  # other fields stay
        assert (rows["planner"]["system_prompt"], rows["planner"]["default_prompt"]) == (
            "My own planner prompt.", PLANNER)
        reviewer = rows["reviewer"]
        assert (reviewer["system_prompt"], reviewer["default_prompt"], reviewer["role"]) == (
            REVIEWER, REVIEWER, "reviewer")
        assert (reviewer["timeout_seconds"], reviewer["max_turns"], reviewer["harness_prompt"]) == (86400, 0, "")
        # The new recommendations hold responsibilities only: character and stability moved to the harness.
        assert "Default to Chinese" not in MANAGER and "Be concise" not in MANAGER
        assert "Answer the user in their language" not in PLANNER
        assert "Check by running things" not in WORKER

        # A second startup changes nothing.
        before = await pool.fetch("SELECT agent_id, system_prompt, default_prompt FROM agent_configs ORDER BY 1")
        await seed_agent_configs(pool)
        assert await pool.fetch("SELECT agent_id, system_prompt, default_prompt FROM agent_configs ORDER BY 1") == before

    _run(_test)
