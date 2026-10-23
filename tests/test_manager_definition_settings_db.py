"""A Manager's Agent Profile resolves from its definition and its own session (PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.agent_settings import AgentType, Layer, load_agent_settings
from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _profile, stand_in as stand_in  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

REVIEW = "manager-review"


async def _define(pool: Any, profile_id: str | None) -> None:
    await pool.execute(
        "INSERT INTO managers (id, name, system_prompt, profile_id) VALUES ($1, 'Review', 'review', $2)",
        REVIEW, profile_id)


async def _resolved(**kwargs: Any) -> tuple[str, Layer]:
    settings = await load_agent_settings(AgentType.MANAGER, **kwargs)
    return settings.profile.id, settings.profile_source


def test_the_default_manager_resolves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.profile_registry import BUILTIN_PROFILE
    from looper.db import agent_setting_layers

    async def _test(pool: Any) -> None:
        p2 = await _profile("claude-code/sonnet")
        assert await _resolved() == (BUILTIN_PROFILE.id, Layer.BUILTIN)
        await agent_setting_layers.put_layer("role", "manager", None, {"profile_id": p2}, pool=pool)
        assert await _resolved() == (p2, Layer.ROLE)
        await agent_setting_layers.put_layer("role", "manager", None, {"profile_id": None}, pool=pool)
        assert await _resolved() == (BUILTIN_PROFILE.id, Layer.BUILTIN)

    run_db(_test, monkeypatch)


def test_a_manager_resolves_its_definition_then_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions, agent_setting_layers

    async def _test(pool: Any) -> None:
        p1, p2, p3, p4 = [await _profile(f"p{i}", model="sonnet") for i in range(1, 5)]
        await _define(pool, p1)
        assert await _resolved(manager_id=REVIEW) == (p1, Layer.MANAGER)
        await agent_setting_layers.put_layer("role", "manager", None, {"profile_id": p2}, pool=pool)
        assert await _resolved(manager_id=REVIEW) == (p1, Layer.MANAGER)
        assert await _resolved() == (p2, Layer.ROLE)

        await agent_sessions.select_profile(REVIEW, p3)
        assert await _resolved(manager_id=REVIEW) == (p3, Layer.SESSION)
        assert await _resolved(manager_id=REVIEW, manager_session=False) == (p1, Layer.MANAGER)

        await agent_sessions.select_profile("manager", p4)
        assert await _resolved(manager_id=REVIEW) == (p3, Layer.SESSION)
        assert await _resolved() == (p4, Layer.SESSION)

        await agent_sessions.select_profile("manager", None)
        assert await _resolved() == (p2, Layer.ROLE)

    run_db(_test, monkeypatch)


def test_profile_usage_lists_every_manager_session_and_blocks_deleting(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry
    from looper.db import agent_sessions, profile_registry as db

    async def _test(pool: Any) -> None:
        p3 = await _profile("p3", model="sonnet")
        await _define(pool, None)
        await agent_sessions.select_profile(REVIEW, p3)
        usage = (await db.profile_usage(p3))[p3]
        assert usage["manager_session"] is True and usage["manager_sessions"] == [REVIEW]
        assert "current session of Manager(s) manager-review" in profile_registry.usage_text(usage)
        with pytest.raises(profile_registry.RegistryConflict) as err:
            await profile_registry.delete_profile(p3)
        assert REVIEW in str(err.value)
        assert await agent_sessions.manager_sessions_selecting(p3) == [REVIEW]

    run_db(_test, monkeypatch)
