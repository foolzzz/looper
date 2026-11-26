"""The Manager's session slot: ``ManagerAgent``'s session attributes keep their old names and read and write the
session the context sets, else main."""

from __future__ import annotations

import asyncio
import contextvars
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from looper.agents.run_usage import Usage
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.session_routing import MAIN_KEY
from looper.agents.tiers.manager_session import MAIN_SESSION, ManagerSession
from looper.agents.tiers.state_view import StateShown


def _manager(tmp_path: Path) -> ManagerAgent:
    return ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, clock=lambda: 7.0)


def test_a_new_manager_has_only_main_with_the_old_defaults(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    main = manager._sessions[MAIN_SESSION]
    assert list(manager._sessions) == [MAIN_SESSION] and manager._session_now() is main
    assert main.claude_runtime is CLAUDE_CODE and not main.claude.started and not main.turn_lock.locked()
    assert {f.name: getattr(main, f.name) for f in fields(ManagerSession)
            if f.name not in ("claude", "claude_runtime", "turn_lock", "lifecycle_gate")} == {
        "key": MAIN_SESSION, "route": MAIN_KEY, "session_id": None, "session_totals": Usage(), "session_runtime": None,
        "session_profile": None, "session_provider": None, "process": None, "pending_switch": None,
        "pending_handoff": None, "clean_start": False, "session_loaded": False, "state_shown": None,
        "state_lost": "no whole block yet", "state_pending_whole": None, "state_generation": 0, "stopped_notes": [], "lock_owner": None,
        "last_user_turn_ended_at": None, "last_background_turn_ended_at": None,
        "running_token": None, "research": None, "agent_settings": None, "last_activity": 7.0,
        "turn_ended_at": None, "budget": None}
    assert not main.lifecycle_gate.locked() and main.lifecycle_gate is not main.turn_lock


def test_the_old_names_read_and_write_the_session_the_context_sets(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    main = manager._sessions[MAIN_SESSION]
    group = ManagerSession(key="group:lark:chat-1", claude=CLAUDE_CODE.resident_session(manager.agent_id),
                           claude_runtime=CLAUDE_CODE, last_activity=0.0)
    shown_main, shown_group = StateShown("r1", ("a",), "s-main"), StateShown("r2", ("b",), "s-group")

    def turn(session: ManagerSession, session_id: str, shown: StateShown) -> tuple[object, ...]:
        manager._current.set(session)
        manager._session_now().session_id, manager._state_shown = session_id, shown
        return manager._session_now(), manager._session_now().session_id, manager._turn_lock, manager._state_shown

    assert contextvars.copy_context().run(turn, group, "s-group", shown_group) == (
        group, "s-group", group.turn_lock, shown_group)
    assert contextvars.copy_context().run(turn, main, "s-main", shown_main) == (
        main, "s-main", main.turn_lock, shown_main)
    assert (group.session_id, group.state_shown) == ("s-group", shown_group)
    assert main.turn_lock is not group.turn_lock
    ended = object()
    main.last_user_turn_ended_at = main.last_background_turn_ended_at = ended
    assert group.last_user_turn_ended_at is None and group.last_background_turn_ended_at is None

    # Nothing set here: every old name still kept is main's field.
    assert manager._current.get() is None and manager._session_now() is main


@pytest.mark.parametrize("name", (
    "claude", "claude_runtime", "session_totals", "session_profile", "session_provider", "turn_lock",
    "process", "pending_switch", "pending_handoff", "clean_start", "session_loaded", "state_shown",
    "state_lost", "state_pending_whole", "state_generation", "stopped_notes", "lock_owner", "running_token", "lifecycle_gate",
    "research", "agent_settings", "last_activity",
))
def test_each_forwarding_property_reads_and_writes_only_the_selected_session(tmp_path: Path, name: str) -> None:
    manager = _manager(tmp_path)
    main = manager._sessions[MAIN_SESSION]
    group = ManagerSession(key="group:lark:chat-1", claude=CLAUDE_CODE.resident_session(manager.agent_id),
                           claude_runtime=CLAUDE_CODE, last_activity=0.0)
    attribute = f"_{name}"
    assert isinstance(getattr(ManagerAgent, attribute), property)
    main_value, group_value, replacement = object(), object(), object()
    setattr(main, name, main_value)
    setattr(group, name, group_value)

    def group_turn() -> None:
        manager._current.set(group)
        assert getattr(manager, attribute) is group_value
        setattr(manager, attribute, replacement)
        assert getattr(group, name) is replacement
        assert getattr(main, name) is main_value

    contextvars.copy_context().run(group_turn)
    assert manager._current.get() is None
    assert getattr(manager, attribute) is main_value
    setattr(manager, attribute, group_value)
    assert getattr(main, name) is group_value
    assert getattr(group, name) is replacement


@pytest.mark.parametrize("dispatch", ("task", "callback"))
def test_settings_cache_fallback_keeps_the_scheduling_sessions_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatch: str,
) -> None:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.tiers import manager as manager_module

    manager = _manager(tmp_path)
    main = manager._sessions[MAIN_SESSION]
    group = ManagerSession(key="group:lark:chat-1", claude=CLAUDE_CODE.resident_session(manager.agent_id),
                           claude_runtime=CLAUDE_CODE, last_activity=0.0)
    main_cached, group_cached = AgentSettings(), AgentSettings()
    failed = AgentSettings(error="settings read failed")
    loaded = failed

    async def load(*args: object, **kwargs: object) -> AgentSettings:
        return loaded

    monkeypatch.setattr(manager_module, "load_agent_settings", load)

    async def run() -> None:
        nonlocal loaded
        for session in (main, group):
            token = manager._current.set(session)
            try:
                assert await manager._load_agent_settings() is failed  # no cache before a successful read
            finally:
                manager._current.reset(token)
        for session, cached in ((main, main_cached), (group, group_cached)):
            token = manager._current.set(session)
            try:
                loaded = cached
                assert await manager._load_agent_settings() is cached
                loaded = failed
            finally:
                manager._current.reset(token)

        ready = asyncio.Event()

        async def read_cache() -> tuple[ManagerSession, AgentSettings, AgentSettings]:
            await ready.wait()
            return (manager._session_now(), await manager._load_agent_settings(),
                    await manager._load_agent_settings(fallback=False))

        pending = []
        for session in (None, main, group):
            token = manager._current.set(session)
            try:
                started = asyncio.get_running_loop().create_future()
                if dispatch == "task":
                    started.set_result(asyncio.create_task(read_cache()))
                else:
                    asyncio.get_running_loop().call_soon(
                        lambda target=started: target.set_result(asyncio.create_task(read_cache())))
                pending.append(started)
            finally:
                manager._current.reset(token)

        # The parent moves to group before either the callbacks or the delayed reads run.
        token = manager._current.set(group)
        try:
            tasks = [await started for started in pending]
            ready.set()
            results = await asyncio.gather(*tasks)
        finally:
            manager._current.reset(token)
        for (session, cached, uncached), expected in zip(results, (main, main, group), strict=True):
            assert session is expected
            assert cached is (main_cached if expected is main else group_cached)
            assert uncached is failed
        assert main.agent_settings is main_cached and group.agent_settings is group_cached

    asyncio.run(run())


def test_main_and_group_lifecycle_gates_can_be_held_independently(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    main = manager._sessions[MAIN_SESSION]
    group = ManagerSession(key="group:lark:chat-1", claude=CLAUDE_CODE.resident_session(manager.agent_id),
                           claude_runtime=CLAUDE_CODE, last_activity=0.0)
    manager._sessions[group.key] = group

    async def run() -> None:
        assert main.lifecycle_gate is not group.lifecycle_gate
        assert not manager.deletion_blocked
        for session, other in ((main, group), (group, main)):
            token = manager._current.set(session)
            try:
                assert manager._lifecycle_gate is session.lifecycle_gate
                async with manager._lifecycle_gate:
                    assert session.lifecycle_gate.locked() and not other.lifecycle_gate.locked()
                    assert manager.deletion_blocked
                    async with other.lifecycle_gate:
                        assert main.lifecycle_gate.locked() and group.lifecycle_gate.locked()
                assert not manager.deletion_blocked
            finally:
                manager._current.reset(token)

    asyncio.run(run())


def test_the_main_sessions_binding_file_and_bridge_env_are_as_before(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import ManagerOps, Platform

    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)
    manager = _manager(tmp_path)
    turn_file = tmp_path / ".local" / "manager-turn.json"  # where the fleet puts it (``ManagerFleet._build``)
    manager.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="tok",
                             turn_file=turn_file)
    manager._publish_binding(None)
    assert turn_file.read_bytes() == b'{"epoch": 1, "turn": null}'
    library = asyncio.run(manager._library_with_server(AgentLibrary()))
    entry = json.loads(Path(library.mcp_config).read_text(encoding="utf-8"))["mcpServers"]["looper"]
    assert entry == {"command": sys.executable, "args": ["-m", "looper.cli", "mcp-server"],
                     "env": {"LOOPER_AGENT_URL": "http://127.0.0.1:9", "LOOPER_AGENT_TOKEN": "tok",
                             "LOOPER_AGENT_TURN_FILE": str(turn_file)}}


def test_a_turns_research_sources_reach_only_its_own_sessions_prompt(tmp_path: Path) -> None:
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.research_order import CONTRACT_NAME, ResearchSources

    manager = _manager(tmp_path)
    group = ManagerSession(key="group:lark:chat-1", claude=CLAUDE_CODE.resident_session(manager.agent_id),
                           claude_runtime=CLAUDE_CODE, last_activity=0.0)
    settings = AgentSettings(profile=builtin_resolved_profile())
    manager._research = ResearchSources(vault=True, leads=True)  # main's turn, prepared first

    def group_prompt() -> str:
        manager._current.set(group)
        return manager._composed_prompt(settings).layers["contract"]["name"]

    assert CONTRACT_NAME not in contextvars.copy_context().run(group_prompt)
    assert CONTRACT_NAME in manager._composed_prompt(settings).layers["contract"]["name"]
