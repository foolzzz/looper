"""Routed sessions cannot acquire local tools or another session's files through a shared launch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import session_routing
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.library import AgentLibrary
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import GROUP_A, GROUP_B, OTHER, _lark, _published, _Resident, _Rig


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
