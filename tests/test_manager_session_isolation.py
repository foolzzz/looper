"""Routed sessions cannot acquire local tools or another session's files through a shared launch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import session_routing
from looper.agents.agent_settings import Access, AgentSettings, Launch
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.library import AgentLibrary
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import GROUP_A, GROUP_B, OPEN_ID, OTHER, _lark, _published, _Resident, _Rig


class CapturedResident(_Resident):
    async def start(self, command: list[str], **kwargs: Any) -> None:
        self.launch = kwargs
        await super().start(command, **kwargs)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_routed_launches_have_only_platform_tools_and_separate_files(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    # User configuration cannot point a routed session back at shared caches.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "operator-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "operator-cache"))
    installed = AsyncMock(return_value=AgentLibrary(plugin_dirs=(str(tmp_path / "operator-plugin"),)))
    monkeypatch.setattr(manager_module, "load_installed", installed)

    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        ops = rig.manager.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9",
                                          token="synthetic-main-token",
                                          turn_file=tmp_path / ".local" / "manager-turn.json")
        ops._working_at = ops._clock()
        residents = [rig.group(chat, CapturedResident(f"session-{index}"))
                     for index, chat in enumerate((GROUP_A, GROUP_B))]
        await rig.start()
        try:
            for chat in (GROUP_A, GROUP_B):
                await rig.reply(await _published(rig, _lark("hello", chat, OTHER, group=True)))
        finally:
            await rig.close()
        workspaces = []
        for resident in residents:
            command = resident.starts[0]
            assert "--tools" in command, "a routed session still has local Read/Bash tools"
            assert command[command.index("--tools") + 1] == ""
            assert "--dangerously-skip-permissions" not in command
            assert command.count("--allowedTools") == 1
            assert command[command.index("--allowedTools") + 1] == "mcp__looper__*"
            assert "--disable-slash-commands" in command and "--plugin-dir" not in command
            cwd = Path(resident.launch["cwd"])
            assert cwd != tmp_path
            workspaces.append(cwd)
            environment = resident.launch["env"]
            home, cache, config = (Path(environment[k]) for k in ("HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"))
            assert all(p.is_relative_to(cwd.parent) for p in (home, cache, config))
            assert all(p.is_dir() for p in (cwd, home, cache, config))
            for flag in ("--system-prompt-file", "--mcp-config"):
                assert Path(command[command.index(flag) + 1]).is_relative_to(cwd.parent)
            servers = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())["mcpServers"]
            assert set(servers) == {"looper"}
            binding_file = Path(servers["looper"]["env"]["LOOPER_AGENT_TURN_FILE"])
            assert binding_file.is_relative_to(cwd.parent)
        assert len(set(workspaces)) == 2
        installed.assert_not_awaited()  # no catalog MCP, plugin hooks or skill can bypass the platform boundary

    run_db(body, monkeypatch, timeout=90)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_unverified_tool_restriction_refuses_routed_turn_without_fallback(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(CLAUDE_CODE, "supports_platform_tools_only", False)

    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        group = rig.group(GROUP_A, CapturedResident("group"))
        await rig.start()
        try:
            reply = await rig.reply(await _published(rig, _lark("hello", GROUP_A, OTHER, group=True)))
            assert "尚不支持" in reply
            assert group.starts == rig.main.starts == []
            from tests.test_manager_session_routing_db import _web

            await rig.reply(await _published(rig, _web("hello")))
            assert len(rig.main.starts) == 1
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("config", [None, "original-config"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_isolated_config_reuses_the_login_namespace_not_its_histories(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path, config: str | None,
                                                                   platform: str) -> None:
    import sys

    original_home = tmp_path / "operator-home"
    keychains = original_home / "Library" / "Keychains"
    keychains.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    if config is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / config))
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    session = manager._router.session(SessionKey(SessionKind.GROUP, "synthetic-bot", "synthetic-chat"))
    manager._current.set(session)
    environment = manager._session_environment(Launch(AgentSettings(profile=builtin_resolved_profile())))
    # Claude's keychain service name otherwise acquires the new config directory's hash and cannot log in.
    namespace = str(tmp_path / config) if config else ("" if platform == "darwin" else str(original_home / ".claude"))
    assert environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == namespace
    assert environment["CLAUDE_CONFIG_DIR"] != environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
    if sys.platform == "darwin":
        assert (Path(environment["HOME"]) / "Library" / "Keychains").resolve() == keychains
        if config is None:
            # With the default keychain namespace, Claude's refresh lock is HOME/.claude. Sessions sharing that
            # login must also share its lock, while their actual Claude state stays in CLAUDE_CONFIG_DIR.
            assert Path(environment["HOME"]) == original_home


def test_platform_tools_argv_excludes_builtin_tools_without_a_database() -> None:
    launch = Launch(AgentSettings(profile=builtin_resolved_profile()), access=Access.PLATFORM_TOOLS)
    argv = CLAUDE_CODE.launch_args(launch)
    assert argv.count("--tools") == 1 and argv[argv.index("--tools") + 1] == ""
    assert "--dangerously-skip-permissions" not in argv
    assert argv.count("--allowedTools") == 1
    assert argv[argv.index("--allowedTools") + 1] == "mcp__looper__*"
    assert "--disable-slash-commands" in argv


def test_unverified_platform_restriction_refuses_launch_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import RuntimeUnsupported

    launch = Launch(AgentSettings(profile=builtin_resolved_profile()), access=Access.PLATFORM_TOOLS)
    monkeypatch.setattr(CLAUDE_CODE, "supports_platform_tools_only", False)
    with pytest.raises(RuntimeUnsupported, match="cannot restrict a routed Manager session"):
        launch.args()


def test_routing_on_with_chat_tools_off_keeps_the_empty_tools_public_prompt(monkeypatch: pytest.MonkeyPatch,
                                                                           tmp_path: Path) -> None:
    import asyncio

    from looper.agents import session_tools_settings, session_visibility

    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(session_tools_settings, "_enabled", True)
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    session = manager._router.session(SessionKey(SessionKind.GROUP, "channel-lark", "oc_synthetic_freeze"))
    manager._current.set(session)
    manager._stable_context = False
    version = session_visibility.for_session(session.key, manager_id=manager.agent_id).version
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic-token")
    assert {tool["name"] for tool in asyncio.run(ops.current_listing(session_key=session.key))} == set(
        session_visibility.SESSION_TOOL_RULES)
    assert session_visibility.PUBLIC_PROMPT_TOOLS in manager._composed_prompt(
        AgentSettings(profile=builtin_resolved_profile())).text
    monkeypatch.setattr(session_tools_settings, "_enabled", False)
    assert session_visibility.for_session(session.key, manager_id=manager.agent_id).version == version
    expected = (
        "You are Looper's chat assistant for this conversation. Answer the current message using only this chat. "
        "You have no access to the operator's projects, tasks, settings, memories or other conversations, and no "
        "platform tools. Do not claim to read or change them. Ask the operator to use their own chat for those actions."
    )
    assert session_visibility.for_session(session.key, manager_id=manager.agent_id) is session_visibility.NON_MAIN_ALLOWLIST
    assert manager._composed_prompt(AgentSettings(profile=builtin_resolved_profile())).text.encode() == expected.encode()
    assert asyncio.run(ops.current_listing(session_key=session.key)) == []


@pytest.mark.parametrize("guard,code", [("unmatched", "not_this_turn"), ("stopped", "stopped"), ("ended", "expired")])
def test_chat_write_refuses_an_unmatched_or_finished_turn(monkeypatch: pytest.MonkeyPatch,
                                                         guard: str, code: str) -> None:
    import asyncio
    from dataclasses import replace

    from looper.agents import session_tools_settings
    from looper.agents.ops import Turn, TurnState
    from looper.agents.ops import session_tools

    monkeypatch.setattr(session_tools_settings, "_enabled", True)
    operator = AsyncMock(return_value=OPEN_ID)
    monkeypatch.setattr(session_tools, "operator_sender_id", operator)
    run = AsyncMock(return_value={"task_id": "unexpected", "title": "Unexpected", "status": "ready"})
    monkeypatch.setattr(session_tools, "TOOLS", tuple(replace(tool, run=run) for tool in session_tools.TOOLS))
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic-token")
    monkeypatch.setattr(ops, "_record", AsyncMock())
    matched = AsyncMock(return_value=False)
    monkeypatch.setattr(ops, "_matched", matched)
    key = "group:channel-lark:oc_synthetic_turn_guard"
    turn = Turn(channel="lark", channel_id="channel-lark", chat="oc_synthetic_turn_guard", sender=OPEN_ID,
                message_id="turn-guard", group=True, require_match=True)
    ops.begin_turn(turn, binding="turn-guard", session_key=key)
    if guard != "unmatched":
        turn.state = TurnState.STOPPED if guard == "stopped" else TurnState.ENDED
    result = asyncio.run(ops.call("looper_chat_task_create",
                                 {"title": "Chat request", "requirement": "A public request", "project": "Chat project"},
                                 binding="turn-guard", call_id="synthetic-call", session_key=key))
    assert result["error"]["code"] == code
    run.assert_not_awaited()
    if guard == "unmatched":
        matched.assert_awaited_once_with("turn-guard", "looper_chat_task_create",
                                        {"title": "Chat request", "requirement": "A public request", "project": "Chat project"},
                                        "synthetic-call")
