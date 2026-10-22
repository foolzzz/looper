"""The Manager's profile resolves from the one chosen for its current session first (``Layer.SESSION``)."""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents import agent_settings
from looper.agents.agent_settings import AgentType, Layer, _chain, index_layers, resolve
from tests.test_agent_settings import _profile, _row
from tests.test_catalog_db import DSN, _run


def _layer(scope: str, role: str | None, name: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"scope": scope, "role": role}
    if name:
        row["profile"] = _row(_profile(name))
    return row


def _resolved(role: AgentType, *rows: dict[str, Any]) -> tuple[str, Layer]:
    settings = resolve(role, index_layers(rows, None))
    return settings.profile.name, settings.profile_source


def test_a_session_row_alone_gives_its_profile() -> None:
    assert _resolved(AgentType.MANAGER, _layer("session", "manager", "chosen")) == ("claude-code/chosen", Layer.SESSION)


def test_the_session_profile_wins_over_the_role_layer() -> None:
    rows = (_layer("role", "manager", "configured"), _layer("session", "manager", "chosen"))
    assert _resolved(AgentType.MANAGER, *rows) == ("claude-code/chosen", Layer.SESSION)


def test_without_a_session_row_the_role_layer_stays() -> None:
    rows = ({"scope": "system"}, _layer("role", "manager", "configured"))
    assert _resolved(AgentType.MANAGER, *rows) == ("claude-code/configured", Layer.ROLE)


def test_a_session_row_is_ignored_for_the_planner() -> None:
    rows = (_layer("planner_task", "planner", "its-own"), _layer("session", "manager", "chosen"))
    assert resolve(AgentType.PLANNER, index_layers(rows, None)).profile.name != "claude-code/chosen"
    assert _resolved(AgentType.PLANNER, _layer("session", "manager", "chosen"))[1] is Layer.BUILTIN


@pytest.mark.parametrize("role", list(AgentType))
def test_only_the_managers_chain_gains_the_session_layer(role: AgentType) -> None:
    chain = _chain(role, "proj")
    assert ((Layer.SESSION, AgentType.MANAGER) in chain) is (role is AgentType.MANAGER)
    if role is AgentType.MANAGER:
        assert chain == [(Layer.SYSTEM, None), (Layer.ROLE, AgentType.MANAGER),
                         (Layer.MANAGER, AgentType.MANAGER), (Layer.SESSION, AgentType.MANAGER)]
    else:
        assert chain[-1] == (Layer.TASK, role)


@pytest.mark.parametrize("role, expected", [(AgentType.MANAGER, True), (AgentType.PLANNER, False)])
def test_load_agent_settings_asks_for_the_session_row_only_for_the_manager(
    monkeypatch: pytest.MonkeyPatch, role: AgentType, expected: bool,
) -> None:
    import asyncio
    from looper.db import agent_setting_layers

    calls: list[dict[str, Any]] = []

    async def fake(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(agent_setting_layers, "list_layers", fake)
    asyncio.run(agent_settings.load_agent_settings(role))
    assert len(calls) == 1
    assert ("manager_id" in calls[0]) is expected
    if expected:
        assert calls[0]["manager_id"] == "manager"


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_list_layers_returns_the_session_row_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.profile_registry import BUILTIN_PROFILE
    from looper.db import agent_sessions, agent_setting_layers

    async def _test(_pool: Any) -> None:
        assert not [r for r in await agent_setting_layers.list_layers(manager_id="manager") if r["scope"] == "session"]
        await agent_sessions.select_profile("manager", BUILTIN_PROFILE.id)
        rows = await agent_setting_layers.list_layers(manager_id="manager")
        session = [r for r in rows if r["scope"] == "session"]
        assert len(session) == 1
        assert (session[0]["role"], session[0]["profile_id"]) == ("manager", BUILTIN_PROFILE.id)
        assert session[0]["profile"]["id"] == BUILTIN_PROFILE.id
        assert not [r for r in await agent_setting_layers.list_layers() if r["scope"] == "session"]

    _run(_test, monkeypatch)
