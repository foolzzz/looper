"""A ManagerAgent runs as one Manager definition: identity, scope contract, turned-off turns, per-id settings."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import agent_settings as agent_settings_module
from looper.agents.agent_settings import AgentType, load_agent_settings as real_load
from looper.agents.bus import MessageBus
from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, manager_address
from looper.agents.system_prompt import build_system_prompt
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import (
    DEFAULT_SYSTEM_PROMPT, TOOLS_CONTRACT, TURNED_OFF_REPLY, ManagerAgent, ManagerConfig, scope_contract,
)
from tests.test_manager_reply import WEB, _fake_claude, _resident

def sync(test: Any) -> Any:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))
    return run


PROMPT = "You review pull requests."


def _definition(**changes: Any) -> ManagerDefinition:
    base = ManagerDefinition(id="manager-review", name="Review", description="", system_prompt=PROMPT,
                             profile_id=None, tools=ManagerTools.TASKS, task_scope=TaskScope.OWN,
                             task_policy=TaskPolicy.FREE, enabled=True, builtin=False, revision=1)
    return dataclasses.replace(base, **changes)


def _row(definition: ManagerDefinition, **changes: Any) -> dict[str, Any]:
    row = {"id": definition.id, "name": definition.name, "description": definition.description,
           "system_prompt": definition.system_prompt, "profile_id": definition.profile_id,
           "tools": definition.tools.value, "task_scope": definition.task_scope.value,
           "task_policy": definition.task_policy.value, "enabled": definition.enabled,
           "revision": definition.revision}
    row.update(changes)
    return row


def _agent(tmp_path: Path, definition: ManagerDefinition | None = None) -> ManagerAgent:
    return ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=tmp_path), runtime_root=tmp_path,
                        definition=definition)


def test_default_identity(tmp_path: Path) -> None:
    manager = _agent(tmp_path)
    assert manager.agent_id == "manager"
    assert manager.agent_address == MANAGER_ADDRESS


def test_scenario_identity(tmp_path: Path) -> None:
    manager = _agent(tmp_path, _definition())
    assert manager.agent_id == "manager-review"
    assert manager.agent_address == manager_address("manager-review")
    assert manager.agent_address.agent_type == "manager"


@pytest.mark.usefixtures("agent_db")
@sync
async def test_default_prompt_is_unchanged(tmp_path: Path) -> None:
    manager = _agent(tmp_path)
    settings = await manager._load_agent_settings()
    expected = build_system_prompt(harness=settings.harness, custom_prompt=settings.profile.custom_prompt,
                                   role_id="manager", role=manager._system_prompt,
                                   recommended=manager._prompt_recommended, contract_name="TOOLS_CONTRACT",
                                   contract=TOOLS_CONTRACT)
    got = manager._composed_prompt(settings)
    assert got.text == expected.text
    assert "This Manager's scope" not in got.text


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize(("tools", "scope", "tool_text", "scope_texts"), [
    (ManagerTools.TASKS, TaskScope.OWN, "task tools only", ["only the tasks you created"]),
    (ManagerTools.ALL, TaskScope.OWN, "every platform tool", ["only the tasks you created"]),
    (ManagerTools.TASKS, TaskScope.ALL, "task tools only", ["every task", "list only the tasks you created"]),
    (ManagerTools.ALL, TaskScope.ALL, "every platform tool", ["every task", "list only the tasks you created"]),
])
@sync
async def test_scenario_prompt(tmp_path: Path, tools: ManagerTools, scope: TaskScope, tool_text: str,
                               scope_texts: list[str]) -> None:
    definition = _definition(tools=tools, task_scope=scope)
    manager = _agent(tmp_path, definition)
    manager._system_prompt, manager._prompt_recommended = PROMPT, False
    text = manager._composed_prompt(await manager._load_agent_settings()).text
    assert text.endswith(scope_contract(definition))
    assert PROMPT in text
    assert DEFAULT_SYSTEM_PROMPT not in text
    contract = " ".join(scope_contract(definition).split())
    assert tool_text in contract
    for fragment in scope_texts:
        assert fragment in contract
    assert all(len(line) < 80 for line in scope_contract(definition).splitlines())


@pytest.mark.usefixtures("agent_db")
@sync
async def test_definition_is_read_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    definition = _definition()
    row = _row(definition)

    async def _get(manager_id: str, **_: Any) -> dict[str, Any] | None:
        return row

    monkeypatch.setattr(manager_module.db_managers, "get_manager", _get)
    manager = _agent(tmp_path, definition)
    await manager._load_db_config()
    assert PROMPT in manager._composed_prompt(await manager._load_agent_settings()).text
    row = _row(definition, system_prompt="New prompt.", task_scope="all")
    await manager._load_db_config()
    text = manager._composed_prompt(await manager._load_agent_settings()).text
    assert "New prompt." in text and PROMPT not in text
    assert text.endswith(scope_contract(dataclasses.replace(definition, task_scope=TaskScope.ALL)))
    assert manager._definition.task_scope is TaskScope.ALL
    assert not manager._turned_off


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize("gone", [{"enabled": False}, None])
@sync
async def test_turned_off_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gone: dict[str, Any] | None) -> None:
    definition = _definition()

    async def _get(manager_id: str, **_: Any) -> dict[str, Any] | None:
        return None if gone is None else _row(definition, **gone)

    monkeypatch.setattr(manager_module.db_managers, "get_manager", _get)
    told: list[str] = []

    async def _run() -> list[MessageEnvelope]:
        bus = MessageBus(runtime_root=tmp_path)
        manager = _agent(tmp_path, definition)
        manager._bus = bus
        manager.on_turned_off = told.append
        starts: list[Any] = []
        manager._claude_command = lambda *a, **k: starts.append(a)  # type: ignore[method-assign]
        for n in range(2):
            await manager._handle(MessageEnvelope.create(
                source=WEB, target=manager.agent_address, message_type=MessageType.USER_MESSAGE,
                payload={"text": f"hello {n}"}))
        assert not starts and not manager._claude.started
        replies = []
        queue = bus.get_queue(WEB.agent_id)
        while not queue.empty():
            envelope = queue.get_nowait()
            if envelope.message_type == MessageType.AGENT_REPLY:
                replies.append(envelope)
        await manager.stop()
        await bus.shutdown()
        return replies

    replies = await _run()
    assert [r.payload["text"] for r in replies] == [TURNED_OFF_REPLY] * 2
    assert replies[0].source == manager_address("manager-review")
    assert told == ["manager-review"]


@pytest.mark.usefixtures("agent_db")
@sync
async def test_profile_uses_its_own_definition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[str, bool]] = []

    async def _load(role: Any, *args: Any, manager_id: str = "manager", manager_session: bool = True,
                    **kwargs: Any) -> Any:
        seen.append((manager_id, manager_session))
        settings = await real_load(role, *args, manager_id=manager_id, manager_session=manager_session, **kwargs)
        profile = dataclasses.replace(settings.profile, name="P1" if manager_id == "manager-review" else "P2")
        return dataclasses.replace(settings, profile=profile)

    monkeypatch.setattr(manager_module, "load_agent_settings", _load)
    manager = _agent(tmp_path, _definition())
    assert "P1" in await manager.settings_profile_name()
    await manager.select_session_profile(None, origin={"channel": "web"}, via="test")
    assert seen
    assert all(manager_id == "manager-review" for manager_id, _ in seen)
    assert ("manager-review", False) in seen


@pytest.mark.usefixtures("agent_db")
@sync
async def test_run_is_recorded_under_its_id(agent_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _get(manager_id: str, **_: Any) -> dict[str, Any]:
        return _row(_definition())

    monkeypatch.setattr(manager_module.db_managers, "get_manager", _get)
    manager = ManagerAgent(
        config=ManagerConfig(claude_bin=_fake_claude(tmp_path, _resident()), work_dir=tmp_path),
        runtime_root=tmp_path, definition=_definition())
    manager._bus = MessageBus(runtime_root=tmp_path)

    async def _quiet(*_a: Any, **_k: Any) -> None:
        return None

    manager._send_status = _quiet  # type: ignore[method-assign]
    envelope = MessageEnvelope.create(source=WEB, target=manager.agent_address,
                                      message_type=MessageType.USER_MESSAGE, payload={"text": "hi"})
    try:
        await manager._run_turn("hi", envelope)
    finally:
        await manager._claude.stop()
        await manager._bus.shutdown()
    ids = [call.kwargs.get("agent_id") for call in agent_db.start_record.await_args_list]
    assert ids and set(ids) == {"manager-review"}
