"""Role config rows against a real PostgreSQL: replacing a stale default prompt.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database; the
schema is applied to it and the test's rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.db_support import DSN

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
            assert reset["system_prompt"] == reset["default_prompt"] == "recommended v2"
            await role_configs.seed_defaults([{ "role": edited, "system_prompt": "recommended v3"}], pool=pool)
            assert (await role_configs.get_config(edited, pool=pool))["system_prompt"] == "recommended v3"
            assert (await role_configs.get_config(edited, pool=pool))["system_prompt"] == "recommended v3"
        finally:
            await pool.execute("DELETE FROM role_configs WHERE role = ANY($1::text[])", [unedited, edited])
            await pool.close()

    asyncio.run(_run())


def test_current_role_max_turns_updates() -> None:
    from looper.db import role_configs
    from tests.db_support import run_db

    async def run(pool) -> None:
        await role_configs.seed_defaults([{"role": "test-current"}], pool=pool)
        updated = await role_configs.set_config("test-current", {"max_turns": 7}, pool=pool)
        assert updated["max_turns"] == 7

    run_db(run)
