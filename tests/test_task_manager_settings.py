"""Task Manager profiles belong to the task, independently of Manager sessions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import agent_settings, task_manager_settings as settings
from looper.agents.agent_settings import AgentType, Layer, LayerValues, resolve
from looper.agents.profile_registry import BUILTIN_PROFILE
from tests.db_support import run_db


def test_task_manager_profile_does_not_inherit_manager_or_shared_default():
    layers = {(Layer.SYSTEM, None): LayerValues(profile=BUILTIN_PROFILE),
              (Layer.PROJECT, None): LayerValues(profile=BUILTIN_PROFILE),
              (Layer.ROLE, AgentType.MANAGER): LayerValues(profile=BUILTIN_PROFILE)}
    assert resolve(AgentType.TASK_MANAGER, layers, "project-a").profile.id == settings.DEFAULT_PROFILE_ID
    layers[Layer.TASK, AgentType.TASK_MANAGER] = LayerValues(profile=BUILTIN_PROFILE)
    assert resolve(AgentType.TASK_MANAGER, layers, "project-a").model == "opus"
    layers.pop((Layer.TASK, AgentType.TASK_MANAGER))
    layers[Layer.ROLE, AgentType.TASK_MANAGER] = LayerValues(profile=BUILTIN_PROFILE)
    assert resolve(AgentType.TASK_MANAGER, layers, "project-a").model == "opus"


def test_loads_task_role_instead_of_manager_session(monkeypatch):
    load = AsyncMock()
    monkeypatch.setattr(settings, "load_agent_settings", load)
    asyncio.run(settings.load_task_manager_settings({"id": "task-a", "project_id": "project-a",
                                                    "manager_id": "other-manager"}))
    load.assert_awaited_once_with(AgentType.TASK_MANAGER, project_id="project-a", task_id="task-a",
                                 manager_session=False)


def test_profile_changes_are_durable_and_keep_session_selection_separate(monkeypatch):
    from looper.db import agent_sessions, projects, role_configs, tasks

    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    async def check(pool):
        await settings.seed_defaults(pool)
        own = await role_configs.get_config("task_manager", pool=pool)
        assert own["role"] == "task_manager" and own["timeout_seconds"] == 0
        project = await projects.create_project(name="Task Manager test", pool=pool)
        await tasks.create_task(task_id="task-manager-settings-test", requirement="A synthetic task",
                                project_id=project["id"], pool=pool)
        task = await tasks.get_task("task-manager-settings-test", pool=pool)
        await agent_sessions.select_profile("manager", "agent-claude-code-opus", pool=pool)
        await agent_sessions.save_session(f"task-manager-{task['id']}", "native-session", {}, pool=pool)
        assert (await settings.load_task_manager_settings(task)).model == "claude-sonnet-5-5"
        await settings.set_task_manager_profile(task["id"], "agent-claude-code-opus")
        assert (await settings.load_task_manager_settings(task)).model == "opus"
        overrides = await settings.db_layers.profile_overrides(task["id"], pool=pool)
        assert overrides["task_manager"]["id"] == "agent-claude-code-opus"
        await settings.set_task_manager_profile(task["id"], None)
        assert (await settings.load_task_manager_settings(task)).model == "claude-sonnet-5-5"
        assert await agent_sessions.selects_profile("manager", "agent-claude-code-opus", pool=pool)
        session = await agent_sessions.get_session(f"task-manager-{task['id']}", pool=pool)
        assert session["session_id"] == "native-session" and session["selected_profile_id"] is None
    run_db(check, monkeypatch)


def test_profile_switch_refuses_busy_then_closes_idle_resident(monkeypatch):
    from looper.agents.tiers import task_manager
    from looper.agents.projects import RequestError

    pool = SimpleNamespace(_lock=asyncio.Lock(), in_turn=lambda _: True, close=AsyncMock())
    monkeypatch.setattr(task_manager, "current_pool", lambda: pool)
    write = AsyncMock()
    monkeypatch.setattr(settings.db_layers, "set_task_layer", write)
    monkeypatch.setattr(settings, "check_selection", AsyncMock(return_value=SimpleNamespace(
        runtime=SimpleNamespace(type="claude-code"))))
    async def check():
        with pytest.raises(RequestError, match="stop its turn first") as failure:
            await settings.set_task_manager_profile("task-a", "agent-claude-code-opus")
        assert failure.value.status == "409 Conflict"
        write.assert_not_awaited()
        pool.close.assert_not_awaited()
        pool.in_turn = lambda _: False
        await settings.set_task_manager_profile("task-a", "agent-claude-code-opus")
        write.assert_awaited_once_with("task-a", profiles={"task_manager": "agent-claude-code-opus"})
        pool.close.assert_awaited_once_with("task-a")
    asyncio.run(check())


def test_profile_switch_refuses_non_claude_runtime(monkeypatch):
    from looper.agents.runtimes import RuntimeUnsupported
    from looper.agents.tiers import task_manager

    monkeypatch.setattr(task_manager, "current_pool", lambda: None)
    monkeypatch.setattr(settings, "check_selection", AsyncMock(return_value=SimpleNamespace(
        name="Other profile", runtime=SimpleNamespace(type="codex"))))
    write = AsyncMock()
    monkeypatch.setattr(settings.db_layers, "set_task_layer", write)
    async def check():
        with pytest.raises(RuntimeUnsupported, match="Claude Code"):
            await settings.set_task_manager_profile("task-a", "other-profile")
        write.assert_not_awaited()
    asyncio.run(check())
