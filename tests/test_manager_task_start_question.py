"""A task's configuration in the Manager's reply reflects this turn's successful start."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops.tasks import START_QUESTION
from looper.agents.projects import HTTP_409, RequestError
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import manager_actions, projects, tasks


@pytest.mark.usefixtures("current_turn")
@pytest.mark.parametrize("start", ["same-task", "none", "failed", "another-task"])
def test_created_task_start_question_in_manager_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_db: Any, start: str,
) -> None:
    project = {"id": "project-calc", "name": "calc", "work_dir": str(tmp_path)}
    monkeypatch.setattr(projects, "find_project_by_name", AsyncMock(return_value=project))
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    monkeypatch.setattr(tasks, "get_task", AsyncMock(side_effect=lambda task_id: {
        **agent_db.store.ready[task_id], "status": tasks.READY}))
    if start == "failed":
        monkeypatch.setattr("looper.agents.projects.start_ready_task", AsyncMock(
            side_effect=RequestError(HTTP_409, "start refused")))

    async def run() -> str:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        router = SimpleNamespace(has_planner=lambda task_id: False)
        ops = ManagerOps(Platform(bus=bus, task_router=router), url="http://127.0.0.1:9")
        manager.ops = ops
        monkeypatch.setattr(manager, "_tools_problem", AsyncMock(return_value=None))

        async def model_turn(token: Any, prepared: Any) -> str:
            created = await ops.call("looper_task_create", {
                "title": "计算器", "requirement": "build a calculator", "project": "calc"})
            assert created["status"] == "ok", created
            if start != "none":
                if start == "another-task":
                    created = await ops.call("looper_task_create", {
                        "title": "另一个任务", "requirement": "another task", "project": "calc"})
                    assert created["status"] == "ok", created
                started = await ops.call("looper_task_control", {
                    "task_id": created["result"]["task_id"], "action": "start"})
                if start == "failed":
                    assert started["status"] == "error" and started["error"]["code"] == "conflict", started
                else:
                    assert started["status"] == "ok" and started["result"]["started"], started
            return "已启动" if start == "same-task" else "已创建"

        monkeypatch.setattr(manager, "_run_prepared", model_turn)
        web = channel_address("web")
        try:
            await manager._handle(MessageEnvelope.create(
                source=web, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": "在 calc 里创建计算器任务"}))
            replies = []
            queue = bus.get_queue(web.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    assert not envelope.payload.get("error"), envelope.payload
                    replies.append(envelope.payload["text"])
            assert len(replies) == 1
            return replies[0]
        finally:
            await manager.stop()
            await bus.shutdown()

    reply = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert "- 任务：计算器（task-" in reply
    assert "- 项目：calc（已有）" in reply
    assert f"- 目录：{tmp_path}" in reply and "- Planner：" in reply
    assert reply.count(START_QUESTION) == (0 if start == "same-task" else 1)
    assert ("Ready — not started" in reply) is (start != "same-task")
    if start == "another-task":
        assert "任务已创建：\n- 任务：另一个任务" in reply
