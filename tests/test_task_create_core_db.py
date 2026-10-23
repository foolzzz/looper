"""Both task entry points preserve the same stored configuration on PostgreSQL."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops import Call, Turn
from looper.agents.ops.service import Platform
from looper.agents.ops.tasks import _create
from looper.db import agent_setting_layers, projects, tasks
from tests.db_support import DSN, run_db
from tests.test_usage_api_db import _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("new_project", [False, True])
def test_both_create_entries_preserve_all_task_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, new_project: bool,
) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
                           "('profile-worker', 'Worker choice', 'rt-claude-code', "
                           "(SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' "
                           "AND name = 'sonnet'))")
        work = tmp_path / "work"
        work.mkdir()
        config = {"nested": {"enabled": False, "items": [1, "two"]}, "zero": 0}
        fields = {"requirement": " build a calculator ", "title": "计算器", "goal_id": "calculator",
                  "config": config, "work_dir": str(work), "workspace_mode": "in_place"}
        console_project = {"project": {"name": "Console project", "description": "description",
                                       "background": "background", "work_dir": str(work),
                                       "task_workspaces": "in_place", "task_budget_usd": 12}}
        if not new_project:
            project = await projects.create_project(name="Existing", work_dir=str(work), pool=pool)
            console_project = {"project_id": project["id"]}
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace())
        writer = _Writer()
        await server._route_api("POST", "/api/tasks/create", {}, json.dumps({
            **fields, **console_project, "profile_overrides": {"worker": "profile-worker"},
        }).encode(), writer)
        head, _, body = writer.data.partition(b"\r\n\r\n")
        assert int(head.split()[1]) == 201, body
        console = json.loads(body)
        stored = await tasks.get_task(console["task_id"])
        assert stored["config"] == config
        if new_project:
            row = await projects.get_project(console["project_id"])
            assert (row["description"], row["background"], row["task_budget_usd"], row["task_workspaces"]) == (
                "description", "background", 12, "in_place")
        # _create is the Manager parameter adapter. Its public tool schema stays unchanged;
        # the shared implementation accepts the union, including the console-only config.
        turn = Turn(channel="web", chat="create", message_id="create")
        platform = Platform(bus=MessageBus(runtime_root=tmp_path), task_router=SimpleNamespace())
        result = await _create(Call(platform=platform, turn=turn, arguments={
            **fields, "project": "Manager project" if new_project else "Existing",
            "profiles": {"worker": "profile-worker"},
        }))
        manager = await tasks.get_task(result["task_id"])
        ignored = {"id", "created_at", "updated_at", "project_id"} if new_project else {
            "id", "created_at", "updated_at"}
        assert {k: v for k, v in stored.items() if k not in ignored and k != "project_name"} == {
            k: v for k, v in manager.items() if k not in ignored and k != "project_name"}
        assert await agent_setting_layers.profile_overrides(console["task_id"]) == (
            await agent_setting_layers.profile_overrides(result["task_id"]))
        assert len(turn.blocks) == 1 and "计算器" in turn.blocks[0]
    run_db(test, monkeypatch, timeout=30)
