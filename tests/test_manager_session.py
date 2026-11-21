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
        "key": MAIN_SESSION, "session_id": None, "session_totals": Usage(), "session_runtime": None,
        "session_profile": None, "session_provider": None, "process": None, "pending_switch": None,
        "pending_handoff": None, "clean_start": False, "session_loaded": False, "state_shown": None,
        "state_lost": "no whole block yet", "state_generation": 0, "stopped_notes": [], "lock_owner": None,
        "running_token": None, "research": None, "agent_settings": None, "last_activity": 7.0}
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

    # Nothing set here: every old name still kept is main's field.
    assert manager._current.get() is None and manager._session_now() is main
    for field in fields(ManagerSession):
        if hasattr(ManagerAgent, f"_{field.name}"):
            assert getattr(manager, f"_{field.name}") is getattr(main, field.name), field.name


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


def test_group_turns_share_the_operators_session_while_there_is_only_main() -> None:
    from looper.agents.session_routing import GroupIsolation, group_isolation

    assert group_isolation("manager") is GroupIsolation.SHARED
