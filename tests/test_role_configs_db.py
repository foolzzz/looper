"""Role config rows against a real PostgreSQL: replacing a stale default prompt.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database; the
schema is applied to it and the test's rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_default_prompt_tracks_recommendation_and_reset() -> None:
    """seed upgrades an unedited row to the new recommendation, keeps an edited
    one, and reset restores the recommended prompt."""
    import asyncpg

    from looper.db import role_configs
    from looper.db.connection import ensure_schema

    unedited = f"test-role-{uuid.uuid4().hex[:8]}"
    edited = f"test-role-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            # First seed: both rows get the v1 recommended prompt.
            await role_configs.seed_defaults([
                {"role": unedited, "system_prompt": "recommended v1"},
                {"role": edited, "system_prompt": "recommended v1"},
            ], pool=pool)
            # The user edits one of them.
            await role_configs.set_config(edited, {"system_prompt": "my own prompt"}, pool=pool)

            # Second seed with a new recommendation.
            await role_configs.seed_defaults([
                {"role": unedited, "system_prompt": "recommended v2"},
                {"role": edited, "system_prompt": "recommended v2"},
            ], pool=pool)

            u = await role_configs.get_config(unedited, pool=pool)
            e = await role_configs.get_config(edited, pool=pool)
            # Unedited row followed the new recommendation; edited row kept the user's prompt.
            assert u["system_prompt"] == "recommended v2" and u["default_prompt"] == "recommended v2"
            assert e["system_prompt"] == "my own prompt" and e["default_prompt"] == "recommended v2"

            # Reset the edited row back to the recommended prompt.
            reset = await role_configs.reset_prompt(edited, pool=pool)
            assert reset["system_prompt"] == "recommended v2"
            assert (await role_configs.get_config(edited, pool=pool))["system_prompt"] == "recommended v2"
        finally:
            await pool.execute("DELETE FROM role_configs WHERE role = ANY($1::text[])", [unedited, edited])
            await pool.close()

    asyncio.run(_run())


def test_worker_rows_move_to_the_new_recommended_prompt() -> None:
    """The worker prompt now applies, so its old default moves to the new one:
    a tracked row through seed, a row from before tracking through the
    startup replacement; an edited row is kept."""
    import asyncpg

    from looper.agents.serve import _WORKER_PROMPT_BEFORE_CONTRACTS
    from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT
    from looper.db import role_configs
    from looper.db.connection import ensure_schema

    tracked, untracked, edited = (f"test-worker-{uuid.uuid4().hex[:8]}" for _ in range(3))
    old, new = _WORKER_PROMPT_BEFORE_CONTRACTS, DEFAULT_SYSTEM_PROMPT

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            await role_configs.seed_defaults([
                {"role": tracked, "system_prompt": old},
                {"role": edited, "system_prompt": old},
            ], pool=pool)
            await role_configs.set_config(edited, {"system_prompt": "My own worker prompt."}, pool=pool)
            # A row seeded before the recommended prompt was tracked.
            await pool.execute(
                "INSERT INTO role_configs (role, system_prompt) VALUES ($1, $2)", untracked, old,
            )

            # What startup does: seed the new recommendation, then replace the old default.
            await role_configs.seed_defaults(
                [{"role": a, "system_prompt": new} for a in (tracked, untracked, edited)],
                pool=pool,
            )
            for role in (tracked, untracked, edited):
                await role_configs.replace_default_prompt(role, old=old, new=new, pool=pool)

            for role, expected in ((tracked, new), (untracked, new), (edited, "My own worker prompt.")):
                row = await role_configs.get_config(role, pool=pool)
                assert (row["system_prompt"], row["default_prompt"]) == (expected, new)
            # Once replaced, the next startup leaves it alone.
            assert not await role_configs.replace_default_prompt(tracked, old=old, new=new, pool=pool)
        finally:
            await pool.execute(
                "DELETE FROM role_configs WHERE role = ANY($1::text[])", [tracked, untracked, edited],
            )
            await pool.close()

    asyncio.run(_run())


def test_the_manager_row_moves_from_its_default_before_ready_tasks() -> None:
    """Startup (``seed_role_configs``) gives a manager row still holding the default from before ready tasks the
    current one, whether that default was tracked or not; an edited prompt is kept."""
    import asyncpg

    from looper.agents.serve import _MANAGER_PROMPT_BEFORE_READY_TASKS, seed_role_configs
    from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT
    from looper.db import role_configs
    from looper.db.connection import ensure_schema

    old, new, edited = _MANAGER_PROMPT_BEFORE_READY_TASKS, DEFAULT_SYSTEM_PROMPT, "My own manager prompt."

    async def _run() -> list[tuple[str, str]]:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        seen = []
        try:
            await ensure_schema(pool)
            await seed_role_configs(pool)
            for system_prompt, default_prompt in ((old, old), (old, ""), (edited, old)):
                await pool.execute("UPDATE role_configs SET system_prompt = $1, default_prompt = $2 "
                                   "WHERE role = 'manager'", system_prompt, default_prompt)
                await seed_role_configs(pool)
                row = await role_configs.get_config("manager", pool=pool)
                seen.append((row["system_prompt"], row["default_prompt"]))
        finally:
            await pool.execute("UPDATE role_configs SET system_prompt = $1, default_prompt = $1 WHERE role = 'manager'",
                               new)
            await pool.close()
        return seen

    assert old != new
    assert asyncio.run(_run()) == [(new, new), (new, new), (edited, new)]


def test_permission_mode_is_dropped_and_an_update_that_sends_it_still_applies() -> None:
    """No role ever applied permission_mode (all run with --dangerously-skip-permissions): the startup
    schema drops the column, also from a database that still has it, and an update from an older
    dashboard that still sends the field changes the rest."""
    import asyncpg

    from looper.db import role_configs
    from looper.db.connection import ensure_schema

    role = f"test-role-{uuid.uuid4().hex[:8]}"

    async def _columns(pool: asyncpg.Pool) -> set[str]:
        rows = await pool.fetch("SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND table_name = 'role_configs'")
        return {row["column_name"] for row in rows}

    async def _run() -> None:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("ALTER TABLE IF EXISTS role_configs ADD COLUMN IF NOT EXISTS permission_mode TEXT")
            await ensure_schema(pool)
            await ensure_schema(pool)  # idempotent
            assert "permission_mode" not in await _columns(pool)

            await role_configs.seed_defaults([{"role": role}], pool=pool)
            updated = await role_configs.set_config(
                role, {"permission_mode": "plan", "max_turns": 7}, pool=pool)
            assert updated["max_turns"] == 7 and "permission_mode" not in updated
        finally:
            await pool.execute("DELETE FROM role_configs WHERE role = $1", role)
            await pool.close()

    asyncio.run(_run())
