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
