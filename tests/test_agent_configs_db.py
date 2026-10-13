"""Agent config rows against a real PostgreSQL: replacing a stale default prompt.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database; the
schema is applied to it and the test's rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


def test_only_the_old_default_prompt_is_replaced() -> None:
    import asyncpg

    from loopx.agents.serve import _PLANNER_PROMPT_BEFORE_CLAUDE
    from loopx.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT
    from loopx.db import agent_configs
    from loopx.db.connection import ensure_schema

    stale, edited = f"test-planner-{uuid.uuid4().hex[:8]}", f"test-planner-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            await agent_configs.seed_defaults([
                {"agent_id": stale, "role": "planner", "system_prompt": _PLANNER_PROMPT_BEFORE_CLAUDE},
                {"agent_id": edited, "role": "planner", "system_prompt": "My own planner prompt."},
            ], pool=pool)

            for agent_id in (stale, edited):
                await agent_configs.replace_default_prompt(
                    agent_id, old=_PLANNER_PROMPT_BEFORE_CLAUDE, new=DEFAULT_SYSTEM_PROMPT, pool=pool,
                )
            assert (await agent_configs.get_config(stale, pool=pool))["system_prompt"] == DEFAULT_SYSTEM_PROMPT
            assert (await agent_configs.get_config(edited, pool=pool))["system_prompt"] == "My own planner prompt."
            # Once replaced, the next startup leaves it alone.
            assert not await agent_configs.replace_default_prompt(
                stale, old=_PLANNER_PROMPT_BEFORE_CLAUDE, new=DEFAULT_SYSTEM_PROMPT, pool=pool,
            )
        finally:
            await pool.execute("DELETE FROM agent_configs WHERE agent_id = ANY($1::text[])", [stale, edited])
            await pool.close()

    asyncio.run(_run())


def test_only_the_old_default_manager_prompt_is_replaced() -> None:
    import asyncpg

    from loopx.agents.serve import _MANAGER_PROMPT_BEFORE_PROJECTS
    from loopx.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT
    from loopx.db import agent_configs
    from loopx.db.connection import ensure_schema

    stale, edited = f"test-manager-{uuid.uuid4().hex[:8]}", f"test-manager-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            await agent_configs.seed_defaults([
                {"agent_id": stale, "role": "manager", "system_prompt": _MANAGER_PROMPT_BEFORE_PROJECTS},
                {"agent_id": edited, "role": "manager", "system_prompt": "My own manager prompt."},
            ], pool=pool)

            for agent_id in (stale, edited):
                await agent_configs.replace_default_prompt(
                    agent_id, old=_MANAGER_PROMPT_BEFORE_PROJECTS, new=DEFAULT_SYSTEM_PROMPT, pool=pool,
                )
            assert (await agent_configs.get_config(stale, pool=pool))["system_prompt"] == DEFAULT_SYSTEM_PROMPT
            assert (await agent_configs.get_config(edited, pool=pool))["system_prompt"] == "My own manager prompt."
            assert not await agent_configs.replace_default_prompt(
                stale, old=_MANAGER_PROMPT_BEFORE_PROJECTS, new=DEFAULT_SYSTEM_PROMPT, pool=pool,
            )
        finally:
            await pool.execute("DELETE FROM agent_configs WHERE agent_id = ANY($1::text[])", [stale, edited])
            await pool.close()

    asyncio.run(_run())


def test_default_prompt_tracks_recommendation_and_reset() -> None:
    """seed upgrades an unedited row to the new recommendation, keeps an edited
    one, and reset restores the recommended prompt."""
    import asyncpg

    from loopx.db import agent_configs
    from loopx.db.connection import ensure_schema

    unedited = f"test-agent-{uuid.uuid4().hex[:8]}"
    edited = f"test-agent-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            # First seed: both rows get the v1 recommended prompt.
            await agent_configs.seed_defaults([
                {"agent_id": unedited, "role": "worker", "system_prompt": "recommended v1"},
                {"agent_id": edited, "role": "worker", "system_prompt": "recommended v1"},
            ], pool=pool)
            # The user edits one of them.
            await agent_configs.set_config(edited, {"system_prompt": "my own prompt"}, pool=pool)

            # Second seed with a new recommendation.
            await agent_configs.seed_defaults([
                {"agent_id": unedited, "role": "worker", "system_prompt": "recommended v2"},
                {"agent_id": edited, "role": "worker", "system_prompt": "recommended v2"},
            ], pool=pool)

            u = await agent_configs.get_config(unedited, pool=pool)
            e = await agent_configs.get_config(edited, pool=pool)
            # Unedited row followed the new recommendation; edited row kept the user's prompt.
            assert u["system_prompt"] == "recommended v2" and u["default_prompt"] == "recommended v2"
            assert e["system_prompt"] == "my own prompt" and e["default_prompt"] == "recommended v2"

            # Reset the edited row back to the recommended prompt.
            reset = await agent_configs.reset_prompt(edited, pool=pool)
            assert reset["system_prompt"] == "recommended v2"
            assert (await agent_configs.get_config(edited, pool=pool))["system_prompt"] == "recommended v2"
        finally:
            await pool.execute("DELETE FROM agent_configs WHERE agent_id = ANY($1::text[])", [unedited, edited])
            await pool.close()

    asyncio.run(_run())


def test_worker_rows_move_to_the_new_recommended_prompt() -> None:
    """The worker prompt now applies, so its old default moves to the new one:
    a tracked row through seed, a row from before tracking through the
    startup replacement; an edited row is kept."""
    import asyncpg

    from loopx.agents.serve import _WORKER_PROMPT_BEFORE_CONTRACTS
    from loopx.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT
    from loopx.db import agent_configs
    from loopx.db.connection import ensure_schema

    tracked, untracked, edited = (f"test-worker-{uuid.uuid4().hex[:8]}" for _ in range(3))
    old, new = _WORKER_PROMPT_BEFORE_CONTRACTS, DEFAULT_SYSTEM_PROMPT

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            await agent_configs.seed_defaults([
                {"agent_id": tracked, "role": "worker", "system_prompt": old},
                {"agent_id": edited, "role": "worker", "system_prompt": old},
            ], pool=pool)
            await agent_configs.set_config(edited, {"system_prompt": "My own worker prompt."}, pool=pool)
            # A row seeded before the recommended prompt was tracked.
            await pool.execute(
                "INSERT INTO agent_configs (agent_id, role, system_prompt) VALUES ($1, 'worker', $2)", untracked, old,
            )

            # What startup does: seed the new recommendation, then replace the old default.
            await agent_configs.seed_defaults(
                [{"agent_id": a, "role": "worker", "system_prompt": new} for a in (tracked, untracked, edited)],
                pool=pool,
            )
            for agent_id in (tracked, untracked, edited):
                await agent_configs.replace_default_prompt(agent_id, old=old, new=new, pool=pool)

            for agent_id, expected in ((tracked, new), (untracked, new), (edited, "My own worker prompt.")):
                row = await agent_configs.get_config(agent_id, pool=pool)
                assert (row["system_prompt"], row["default_prompt"]) == (expected, new)
        finally:
            await pool.execute(
                "DELETE FROM agent_configs WHERE agent_id = ANY($1::text[])", [tracked, untracked, edited],
            )
            await pool.close()

    asyncio.run(_run())
