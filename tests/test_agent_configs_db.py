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


def test_default_prompt_tracks_recommendation_and_reset() -> None:
    """seed upgrades an unedited row to the new recommendation, keeps an edited
    one, and reset restores the recommended prompt."""
    import asyncpg

    from looper.db import agent_configs
    from looper.db.connection import ensure_schema

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

    from looper.agents.serve import _WORKER_PROMPT_BEFORE_CONTRACTS
    from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT
    from looper.db import agent_configs
    from looper.db.connection import ensure_schema

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
            # Once replaced, the next startup leaves it alone.
            assert not await agent_configs.replace_default_prompt(tracked, old=old, new=new, pool=pool)
        finally:
            await pool.execute(
                "DELETE FROM agent_configs WHERE agent_id = ANY($1::text[])", [tracked, untracked, edited],
            )
            await pool.close()

    asyncio.run(_run())


def test_permission_mode_is_dropped_and_an_update_that_sends_it_still_applies() -> None:
    """No agent ever applied permission_mode (all run with --dangerously-skip-permissions): the startup
    schema drops the column, also from a database that still has it, and an update from an older
    dashboard that still sends the field changes the rest."""
    import asyncpg

    from looper.db import agent_configs
    from looper.db.connection import ensure_schema

    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"

    async def _columns(pool: asyncpg.Pool) -> set[str]:
        rows = await pool.fetch("SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND table_name = 'agent_configs'")
        return {row["column_name"] for row in rows}

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("ALTER TABLE IF EXISTS agent_configs ADD COLUMN IF NOT EXISTS permission_mode TEXT")
            await ensure_schema(pool)
            await ensure_schema(pool)  # idempotent
            assert "permission_mode" not in await _columns(pool)

            await agent_configs.seed_defaults([{"agent_id": agent_id, "role": "worker"}], pool=pool)
            updated = await agent_configs.set_config(
                agent_id, {"permission_mode": "plan", "max_turns": 7}, pool=pool)
            assert updated["max_turns"] == 7 and "permission_mode" not in updated
        finally:
            await pool.execute("DELETE FROM agent_configs WHERE agent_id = $1", agent_id)
            await pool.close()

    asyncio.run(_run())
