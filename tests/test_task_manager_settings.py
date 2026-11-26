"""Task Manager profiles belong to the task, independently of Manager sessions."""

import asyncio
from types import SimpleNamespace
from dataclasses import replace
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


@pytest.mark.parametrize("source", [None, Layer.ROLE, Layer.TASK])
def test_task_manager_uses_own_profile_runtime_and_effort(source):
    shared = replace(BUILTIN_PROFILE, effort="xhigh")
    own = replace(BUILTIN_PROFILE, effort="high", agent_teams=False)
    layers = {(Layer.SYSTEM, None): LayerValues(profile=shared),
              (Layer.PROJECT, None): LayerValues(profile=shared),
              (Layer.ROLE_PROJECT, AgentType.TASK_MANAGER): LayerValues(profile=shared),
              (Layer.SESSION, AgentType.MANAGER): LayerValues(profile=shared)}
    if source is not None:
        layers[Layer.ROLE, AgentType.TASK_MANAGER] = LayerValues(profile=shared)
        layers[source, AgentType.TASK_MANAGER] = LayerValues(profile=own)
    chosen = resolve(AgentType.TASK_MANAGER, layers, "project-a")
    assert (chosen.profile.runtime.type, chosen.model, chosen.profile.agent_teams, chosen.effort or None) == (
        "claude-code", "opus" if source else "claude-sonnet-5-5", False, "high" if source else None)
    assert chosen.profile_source is (source or Layer.BUILTIN)


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
    from looper.agents.ops import ErrorCode, OpError
    from looper.agents.tiers import task_manager
    from looper.db import projects, tasks

    stop = AsyncMock()
    pool = SimpleNamespace(_lock=asyncio.Lock(), in_turn=lambda _: True,
                           agents={"task-a": SimpleNamespace(resident=SimpleNamespace(stop=stop))})
    monkeypatch.setattr(task_manager, "current_pool", lambda: pool)

    async def check(db):
        project = await projects.create_project(name="Profile selection", pool=db)
        await tasks.create_task(task_id="task-a", requirement="Synthetic work", project_id=project["id"], pool=db)
        with pytest.raises(OpError, match="stop its turn first") as failure:
            await settings.set_task_manager_profile("task-a", "agent-claude-code-opus")
        assert failure.value.code is ErrorCode.TASK_MANAGER_BUSY
        assert await settings.db_layers.profile_overrides("task-a", pool=db) == {}
        stop.assert_not_awaited()
        pool.in_turn = lambda _: False
        await settings.set_task_manager_profile("task-a", "agent-claude-code-opus")
        assert (await settings.db_layers.profile_overrides("task-a", pool=db))["task_manager"]["id"] == "agent-claude-code-opus"
        stop.assert_awaited_once_with(kill=True)
    run_db(check, monkeypatch)


def test_reselecting_the_same_effective_profile_keeps_the_idle_resident(monkeypatch):
    import json

    from looper.agents import api_v1, task_conversations_settings
    from looper.agents.channels.web import WebChannelAdapter
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.ops.clients import ClientOps
    from looper.agents.tiers import task_manager
    from looper.db import agent_setting_layers, chat, projects, tasks

    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    stop = AsyncMock()
    published = []
    monkeypatch.setattr(task_manager, "current_pool", lambda: SimpleNamespace(
        _lock=asyncio.Lock(), in_turn=lambda _: False,
        agents={"task-a": SimpleNamespace(resident=SimpleNamespace(stop=stop))}))

    async def check(db):
        project = await projects.create_project(name="Reselect profile", pool=db)
        await tasks.create_task(task_id="task-a", requirement="Synthetic work", project_id=project["id"], pool=db)
        # The role already selects this profile. Storing it on the task does not change what is in effect.
        await agent_setting_layers.put_layer("role", "task_manager", None,
                                            {"profile_id": "agent-claude-code-opus"}, pool=db)
        adapter = WebChannelAdapter()

        async def publish(task_id, payload):
            published.append((task_id, dict(payload)))

        adapter.publish_task_event = publish
        server = AgentHTTPServer(web_adapter=adapter, supervisor=SimpleNamespace(), task_router=SimpleNamespace(),
                                 client_ops=ClientOps(SimpleNamespace()))

        async def call(path, body):
            return await server._answer_v1("POST", path, api_v1.find("POST", path), {},
                                           json.dumps(body).encode(), api_v1.OPERATOR)

        get_path = "/api/v1/tasks/task_manager_profile_get"
        set_path = "/api/v1/tasks/task_manager_profile_set"
        code, answer = await call(get_path, {"task_id": "task-a"})
        assert int(code.split()[0]) == 200, answer
        role_view = answer["result"]
        assert role_view["override_profile_id"] is None and role_view["source"] == "role"
        effective_id = role_view["effective"]["profile_id"]
        assert effective_id == "agent-claude-code-opus"

        code, answer = await call(set_path, {"task_id": "task-a", "profile_id": effective_id})
        assert int(code.split()[0]) == 200, answer
        overridden = answer["result"]
        assert overridden["effective"] == role_view["effective"]
        assert overridden["choices"] == role_view["choices"] and overridden["task_id"] == "task-a"
        assert overridden["override_profile_id"] == effective_id and overridden["source"] == "task"
        code, answer = await call(get_path, {"task_id": "task-a"})
        assert int(code.split()[0]) == 200 and answer["result"] == overridden, answer

        # The stored id, selected again, returns that same view.
        code, answer = await call(set_path, {"task_id": "task-a", "profile_id": effective_id})
        assert int(code.split()[0]) == 200 and answer["result"] == overridden, answer

        stop.assert_not_awaited()
        saved = await chat.list_messages(chat.task_thread("task-a"), pool=db)
        switch = {"model_switched", "model_switch_rejected"}
        assert switch.isdisjoint(payload.get("kind") for _, payload in published)
        assert switch.isdisjoint(json.loads(row["content"]).get("kind")
                                 for row in saved if row["role"] == "event")
        assert published == [] and saved == []
    run_db(check, monkeypatch)


def test_profile_switch_refuses_non_claude_runtime(monkeypatch):
    from looper.agents.ops import ErrorCode, OpError
    from looper.agents.tiers import task_manager

    monkeypatch.setattr(task_manager, "current_pool", lambda: None)
    monkeypatch.setattr(settings, "_save_event", AsyncMock())
    monkeypatch.setattr(settings, "check_selection", AsyncMock(return_value=SimpleNamespace(
        name="Other profile", runtime=SimpleNamespace(type="codex"))))
    write = AsyncMock()
    monkeypatch.setattr(settings.db_layers, "set_task_layer", write)
    async def check():
        with pytest.raises(OpError, match="Claude Code") as failure:
            await settings.set_task_manager_profile("task-a", "other-profile")
        assert failure.value.code is ErrorCode.TASK_MANAGER_RUNTIME_UNSUPPORTED
        write.assert_not_awaited()
    asyncio.run(check())


def test_stored_profile_effort_reaches_native_task_manager_launch(monkeypatch, tmp_path):
    from looper.agents.tiers.task_manager import TaskManagerPool
    from tests.task_manager_support import NativeTaskManager, submit_and_wait

    loader = settings.load_task_manager_settings
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "load_task_manager_settings", loader)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    async def check(db):
        await settings.seed_defaults(db)
        task_id, _, _ = await native.create(db)
        await db.execute("UPDATE agent_profiles SET effort='high' WHERE id='agent-claude-code-opus'")
        await db.execute("INSERT INTO agent_setting_layers(scope, profile_id) VALUES ('system', $1)",
                         "agent-claude-code-opus")
        pool = TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            for profile_id, model, effort in [(None, "claude-sonnet-5-5", None),
                                               ("agent-claude-code-opus", "opus", "high"),
                                               (None, "claude-sonnet-5-5", None)]:
                await settings.set_task_manager_profile(task_id, profile_id)
                await submit_and_wait(pool, task_id)
                command = native.records("launch")[-1]["command"]
                assert command[command.index("--model") + 1] == model
                actual_effort = command[command.index("--effort") + 1] if "--effort" in command else None
                assert actual_effort == effort
            assert len(native.records("launch")) == 3
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
