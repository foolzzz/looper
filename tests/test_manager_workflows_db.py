"""Manager workflow selection and project readiness settings at the tool boundary."""
import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from looper.agents.bus import MessageBus
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops.tasks import task_create_tool
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.workflows import Workflow
from looper.db import projects, tasks, workflows
from tests.db_support import DSN, run_db


@pytest.mark.parametrize("templates,sha", [
    ((), "36c402d6a8ba23436fbeeaefe090970f4906c120ddf8c322447cf656a0d3c50c"),
    (("review",), "ec9bdb62c97f57544ad3c918d724d78d5e971e04acde574945f11d6730487956"),
])
def test_manager_without_workflows_keeps_legacy_creation_schema(templates, sha):
    # This pins the legacy Manager schema, including its template variant.
    schema = task_create_tool(templates).schema
    assert hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == sha


def test_workflow_context_is_absent_without_presets(tmp_path, agent_db):
    async def test():
        agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        assert await agent._workflows_section() == []
        assert await agent._build_user_message("Hello") == "Projects:\n  (none yet)\n\nHello"
    asyncio.run(test())


def test_workflow_refresh_does_not_restore_a_concurrently_restricted_scope(tmp_path, monkeypatch):
    async def test():
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9")
        restricted = replace(ops.scope, tools={})

        async def rows():
            ops.set_scope(restricted)
            return [{"key": "reviewed"}]

        monkeypatch.setattr(workflows, "list_workflows", rows)
        assert (await ops.current_scope()).tools == {}
        assert ops.scope is restricted
    asyncio.run(test())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_manager_refreshes_workflow_enum_and_creates_a_snapshot(tmp_path, monkeypatch):
    async def test(pool):
        bus = MessageBus(runtime_root=tmp_path)
        ops = ManagerOps(Platform(bus=bus, task_router=SimpleNamespace()), url="http://127.0.0.1:9")
        project = await projects.create_project(name="example", work_dir=str(tmp_path),
                                                execution={"task_workspaces": "in_place"})
        assert "workflow" not in next(t for t in await ops.current_listing()
                                      if t["name"] == "looper_task_create")["inputSchema"]["properties"]
        await workflows.put(Workflow.from_dict({"key": "reviewed", "name": "Reviewed",
                                               "roles": [{"key": "dev", "kind": "work",
                                                          "role_prompt": "Implement the requirement."}]}))
        schema = next(t for t in await ops.current_listing() if t["name"] == "looper_task_create")["inputSchema"]
        assert schema["properties"]["workflow"]["enum"] == ["reviewed", "none"]
        turn = Turn(channel="lark", chat="chat", message_id="confirmed")
        ops.begin_turn(turn)
        try:
            args = {"project": project["name"], "title": "Example", "requirement": "Build", "workflow": "missing"}
            invalid = await ops.call("looper_task_create", args)
            assert invalid["error"]["code"] == "invalid"
            assert "reviewed" in invalid["error"]["message"] and "none" in invalid["error"]["message"]
            created = await ops.call("looper_task_create", {**args, "workflow": "reviewed"})
            assert created["status"] == "ok", created
            task_id = created["result"]["task_id"]
            snapshot = (await tasks.get_task(task_id))["workflow"]
            assert snapshot["key"] == "reviewed" and snapshot["roles"][0]["key"] == "dev"
            assert "Reviewed" in turn.blocks[0]
            got = await ops.call("looper_tasks", {"action": "get", "task_id": task_id})
            assert got["result"]["workflow"] == snapshot
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            section = "\n".join(await agent._workflows_section())
            assert "reviewed: Reviewed" in section
            await workflows.delete("reviewed")
            assert "workflow" not in next(t for t in await ops.current_listing()
                                          if t["name"] == "looper_task_create")["inputSchema"]["properties"]
        finally:
            ops.end_turn(turn)
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_manager_project_readiness_setting_updates_and_reads_back(tmp_path, monkeypatch):
    async def test(pool):
        await projects.create_project(name="example", work_dir=str(tmp_path))
        ops = ManagerOps(Platform(bus=MessageBus(runtime_root=tmp_path), task_router=SimpleNamespace()),
                         url="http://127.0.0.1:9")
        turn = Turn(channel="telegram", chat="chat", message_id="settings")
        ops.begin_turn(turn)
        try:
            changed = await ops.call("looper_projects", {"action": "update", "project": "example",
                                                         "dependency_conditions": "on"})
            assert changed["status"] == "ok", changed
            assert changed["result"]["project"]["dependency_conditions"] == "on"
            for action in ("get", "list"):
                got = await ops.call("looper_projects", {"action": action, "project": "example"})
                view = got["result"]["project"] if action == "get" else got["result"]["projects"][0]
                assert view["dependency_conditions"] == "on"
            invalid = await ops.call("looper_projects", {"action": "update", "project": "example",
                                                         "dependency_conditions": "enabled"})
            assert invalid["error"]["code"] == "invalid"
            reset = await ops.call("looper_projects", {"action": "update", "project": "example",
                                                       "dependency_conditions": None})
            assert reset["result"]["project"]["dependency_conditions"] is None
        finally:
            ops.end_turn(turn)
    run_db(test, monkeypatch, timeout=30)
