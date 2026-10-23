"""The Manager session's choice of an Agent Profile counts as a use of it (PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _call, _profile, _stand_in_profile, stand_in as stand_in  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_manager_session_selection_is_a_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry
    from looper.db import agent_sessions, profile_registry as db

    async def _test(_pool: Any) -> None:
        chosen = await _profile("claude-code/sonnet")
        free = await _profile("claude-code/haiku")
        await agent_sessions.select_profile("manager", chosen)

        usage = await db.profile_usage()
        assert usage[chosen]["manager_session"] is True and free not in usage
        assert "the Manager's current session" in profile_registry.usage_text(usage[chosen])
        assert (await db.profile_usage(chosen))[chosen]["manager_session"] is True

        with pytest.raises(profile_registry.RegistryConflict) as err:
            await profile_registry.delete_profile(chosen)
        assert "the Manager's current session" in str(err.value) and "/profile" in str(err.value)

        await agent_sessions.select_profile("manager", None)
        assert chosen not in await db.profile_usage()
        assert await profile_registry.delete_profile(chosen) == {"deleted": chosen}

    run_db(_test, monkeypatch)


def test_update_keeps_the_manager_session_profile_able_to_run_the_manager(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    from looper.agents import profile_registry
    from looper.db import agent_sessions

    async def _test(_pool: Any) -> None:
        chosen = await _profile("claude-code/sonnet")
        other = await _stand_in_profile(stand_in)  # a runtime without RESIDENT_SESSION
        await agent_sessions.select_profile("manager", chosen)
        row = await _pool.fetchrow("SELECT runtime_id, model_id FROM agent_profiles WHERE id = $1", other)
        with pytest.raises(profile_registry.RegistryError, match="cannot run the manager"):
            await profile_registry.update_profile(chosen, {"runtime_id": row["runtime_id"],
                                                           "model_id": row["model_id"]})
        await agent_sessions.select_profile("manager", None)
        await profile_registry.update_profile(chosen, {"runtime_id": row["runtime_id"],
                                                       "model_id": row["model_id"]})

    run_db(_test, monkeypatch)
