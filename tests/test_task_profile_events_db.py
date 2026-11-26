"""Profile selection events are durable and identical on the open-thread stream."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import jsonschema

from looper.agents import agent_settings, api_v1, task_conversations_settings, task_manager_settings
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops.clients import ClientOps
from looper.agents.tiers import task_manager
from looper.agents.task_manager_settings import DEFAULT_PROFILE_ID
from looper.db import chat, projects, tasks
from tests.db_support import run_db


@pytest.mark.parametrize("selection,reason,status", [
    ("agent-claude-code-opus", None, 200),
    ("agent-claude-code-opus", "busy", 409),
    ("missing-profile", "invalid_profile", 400),
    ("agent-codex-gpt-5.4", "runtime_unsupported", 400),
])
def test_selection_history_and_live_event_match(monkeypatch, selection, reason, status):
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    if reason == "busy":
        monkeypatch.setattr(task_manager, "current_pool", lambda: SimpleNamespace(
            _lock=asyncio.Lock(), in_turn=lambda _: True, agents={}))
    else:
        monkeypatch.setattr(task_manager, "current_pool", lambda: None)
    async def check(pool):
        # Resolve the registered Codex built-in instead of depending on its versioned name.
        nonlocal selection
        if reason == "runtime_unsupported":
            await pool.execute("""
                INSERT INTO agent_runtimes (id, name, type, executable, capabilities)
                SELECT 'rt-test-other', 'Other runtime', 'codex', '', capabilities FROM agent_runtimes
                WHERE id = 'rt-claude-code';
                INSERT INTO agent_profiles (id, name, runtime_id, model_id)
                SELECT 'profile-other', 'Other profile', 'rt-test-other', model_id FROM agent_profiles
                WHERE id = 'agent-claude-code-opus';
                """)
            selection = "profile-other"
        project = await projects.create_project(name="Profile events", pool=pool)
        await tasks.create_task(task_id="profile-task", requirement="Synthetic work", project_id=project["id"],
                                status="draft", pool=pool)
        adapter = WebChannelAdapter()
        server = AgentHTTPServer(web_adapter=adapter, supervisor=SimpleNamespace(), task_router=SimpleNamespace(),
                                 client_ops=ClientOps(SimpleNamespace()))
        path = "/api/v1/tasks/task_manager_profile_set"
        async def select(value):
            return await server._answer_v1("POST", path, api_v1.find("POST", path), {}, json.dumps({
                "task_id": "profile-task", "profile_id": value}).encode(), api_v1.OPERATOR)
        events = adapter.iter_chat_events(chat.task_thread("profile-task"))
        try:
            if reason is None:
                await select(DEFAULT_PROFILE_ID)
                await select(None)
                assert await chat.list_messages(chat.task_thread("profile-task")) == []
            code, answer = await select(selection)
            assert int(code.split()[0]) == status, answer
            live = await asyncio.wait_for(anext(events), 1)
            rows = await chat.list_messages(chat.task_thread("profile-task"))
            assert len(rows) == 1 and rows[0]["role"] == "event"
            expected = ({"kind": "model_switched", "from_profile_id": DEFAULT_PROFILE_ID,
                         "to_profile_id": selection} if reason is None else {
                         "kind": "model_switch_rejected", "profile_id": selection, "reason": reason})
            assert json.loads(rows[0]["content"]) == live["payload"] == expected
            assert live["kind"] == "task.event"
            jsonschema.validate(expected, api_v1.document()["components"]["schemas"]["OperatorChatTaskEvent"])
            if reason is None:
                await select(selection)
                assert len(await chat.list_messages(chat.task_thread("profile-task"))) == 1
                # Clearing restores the effective role profile and is a second actual change.
                await select(None)
                restored = json.loads((await chat.list_messages(chat.task_thread("profile-task")))[-1]["content"])
                assert restored == {"kind": "model_switched", "from_profile_id": selection,
                                    "to_profile_id": DEFAULT_PROFILE_ID}
                assert (await asyncio.wait_for(anext(events), 1))["payload"] == restored
        finally:
            await events.aclose()
    run_db(check, monkeypatch)


def test_new_task_events_remain_closed_and_published_on_thread_stream():
    document = api_v1.document()
    schema = document["components"]["schemas"]["OperatorChatTaskEvent"]
    for payload in [{"type": "unknown"}, {"kind": "model_switch_rejected", "profile_id": None, "reason": "unknown"},
                    {"kind": "model_switched", "type": "model_switched", "from_profile_id": "a", "to_profile_id": "b"},
                    {"kind": "model_switched", "from_profile_id": "a", "to_profile_id": "b", "extra": True},
                    {"kind": "queued", "reason": "cap", "position": 1, "active": -1}]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, schema)
    events = document["paths"]["/operator_chat/thread_events"]["post"]["responses"]["200"]["content"][
        "text/event-stream"]["schema"]["x-looper-events"]
    assert "task.event" in events


def test_failed_event_write_rolls_back_the_profile_selection(monkeypatch):
    monkeypatch.setattr(task_manager, "current_pool", lambda: None)
    monkeypatch.setattr(task_manager_settings, "_save_event", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    publish = AsyncMock()
    async def check(pool):
        project = await projects.create_project(name="Atomic profile selection", pool=pool)
        await tasks.create_task(task_id="atomic-profile", requirement="Synthetic work", project_id=project["id"], pool=pool)
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await task_manager_settings.set_task_manager_profile("atomic-profile", "agent-claude-code-opus", publish=publish)
        assert await task_manager_settings.db_layers.profile_overrides("atomic-profile", pool=pool) == {}
        assert await chat.list_messages(chat.task_thread("atomic-profile")) == []
        publish.assert_not_awaited()
    run_db(check, monkeypatch)


def _one_kind(payload, name):
    assert {key for key in ("kind", "type") if key in payload} == {"kind"}
    assert payload["kind"] == name


def test_history_and_live_profile_events_use_one_kind(monkeypatch):
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(task_manager, "current_pool", lambda: None)

    async def check(pool):
        project = await projects.create_project(name="Kind discriminator", pool=pool)
        await tasks.create_task(task_id="kind-task", requirement="Synthetic work", project_id=project["id"],
                                status="draft", pool=pool)
        adapter = WebChannelAdapter()
        server = AgentHTTPServer(web_adapter=adapter, supervisor=SimpleNamespace(), task_router=SimpleNamespace(),
                                 client_ops=ClientOps(SimpleNamespace()))
        path = "/api/v1/tasks/task_manager_profile_set"
        events = adapter.iter_chat_events(chat.task_thread("kind-task"))

        async def select(value):
            return await server._answer_v1("POST", path, api_v1.find("POST", path), {}, json.dumps({
                "task_id": "kind-task", "profile_id": value}).encode(), api_v1.OPERATOR)

        try:
            code, answer = await select("agent-claude-code-opus")
            assert int(code.split()[0]) == 200, answer
            live = await asyncio.wait_for(anext(events), 1)
            stored = json.loads((await chat.list_messages(chat.task_thread("kind-task")))[-1]["content"])
            assert live["kind"] == "task.event"
            _one_kind(live["payload"], "model_switched")
            _one_kind(stored, "model_switched")
            assert stored == live["payload"]
            code, answer = await select("missing-profile")
            assert int(code.split()[0]) == 400, answer
            live = await asyncio.wait_for(anext(events), 1)
            stored = json.loads((await chat.list_messages(chat.task_thread("kind-task")))[-1]["content"])
            _one_kind(live["payload"], "model_switch_rejected")
            _one_kind(stored, "model_switch_rejected")
            assert stored == live["payload"]
            assert stored["reason"] == "invalid_profile"
        finally:
            await events.aclose()
    run_db(check, monkeypatch)


def test_event_save_failure_keeps_the_original_refusal(monkeypatch, caplog):
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(task_manager, "current_pool", lambda: SimpleNamespace(
        _lock=asyncio.Lock(), in_turn=lambda _: True, agents={}))
    caplog.set_level(logging.WARNING, logger="looper.agents.task_manager_settings")

    async def check(pool):
        project = await projects.create_project(name="Refusal preserved", pool=pool)
        await tasks.create_task(task_id="refusal-task", requirement="Synthetic work", project_id=project["id"],
                                status="draft", pool=pool)
        server = AgentHTTPServer(web_adapter=WebChannelAdapter(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(), client_ops=ClientOps(SimpleNamespace()))
        path = "/api/v1/tasks/task_manager_profile_set"
        body = json.dumps({"task_id": "refusal-task", "profile_id": "agent-claude-code-opus"}).encode()

        async def call():
            return await server._answer_v1("POST", path, api_v1.find("POST", path), {}, body, api_v1.OPERATOR)

        baseline = await call()
        assert baseline[1]["error"]["code"] == "conflict"
        assert baseline[1]["error"]["message"] == "the task Manager is working in this task: stop its turn first"
        before = len(await chat.list_messages(chat.task_thread("refusal-task")))
        monkeypatch.setattr(task_manager_settings, "_save_event", AsyncMock(side_effect=RuntimeError("storage unavailable")))
        failed = await call()
        assert failed[0] == baseline[0]
        assert failed[1]["error"]["code"] == baseline[1]["error"]["code"]
        assert failed[1]["error"]["message"] == baseline[1]["error"]["message"]
        assert len(await chat.list_messages(chat.task_thread("refusal-task"))) == before
        assert any(record.name == "looper.agents.task_manager_settings" and record.exc_info
                   and "could not save the model switch refusal" in record.getMessage()
                   for record in caplog.records)
    run_db(check, monkeypatch)


def test_publish_failure_after_commit_still_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(task_manager, "current_pool", lambda: None)
    caplog.set_level(logging.WARNING, logger="looper.agents.task_manager_settings")

    async def check(pool):
        project = await projects.create_project(name="Publish failure", pool=pool)
        await tasks.create_task(task_id="publish-task", requirement="Synthetic work", project_id=project["id"],
                                status="draft", pool=pool)
        adapter = WebChannelAdapter()
        server = AgentHTTPServer(web_adapter=adapter, supervisor=SimpleNamespace(), task_router=SimpleNamespace(),
                                 client_ops=ClientOps(SimpleNamespace()))
        published = []

        async def fail_publish(task_id, payload):
            published.append(dict(payload))
            raise RuntimeError("bus down")

        server._web_adapter.publish_task_event = fail_publish
        path = "/api/v1/tasks/task_manager_profile_set"
        code, answer = await server._answer_v1("POST", path, api_v1.find("POST", path), {}, json.dumps({
            "task_id": "publish-task", "profile_id": "agent-claude-code-opus"}).encode(), api_v1.OPERATOR)
        assert int(code.split()[0]) == 200, answer
        assert answer["status"] == "ok"
        assert answer["result"] == {"task_id": "publish-task", "profile_id": "agent-claude-code-opus"}
        rows = await chat.list_messages(chat.task_thread("publish-task"))
        assert len(rows) == 1 and rows[0]["role"] == "event"
        stored = json.loads(rows[0]["content"])
        _one_kind(stored, "model_switched")
        assert published and published[0] == stored
        overrides = await task_manager_settings.db_layers.profile_overrides("publish-task", pool=pool)
        assert overrides["task_manager"]["id"] == "agent-claude-code-opus"
        assert any(record.name == "looper.agents.task_manager_settings" and record.exc_info
                   and "could not publish the model switch" in record.getMessage()
                   for record in caplog.records)
    run_db(check, monkeypatch)
