"""The Manager's tools end to end: Claude's MCP calls reach ``looper serve`` and act in the turn they came in.

A stand-in ``claude`` reads the ``--mcp-config`` the Manager launched it with,
starts the ``looper`` server from it (``looper mcp-server``) and calls tools
over MCP; the calls reach a real ``AgentHTTPServer`` and ``ManagerOps`` on a
real PostgreSQL. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable
database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

WEB = channel_address("web")

async def _converse(root: Path, turns: list[list[tuple[str, dict[str, Any]]]], messages: list[str]) -> Any:
    """Send ``messages`` to a Manager whose Claude makes ``turns``' calls; the replies, Claude's user turns and
    the tools it was given."""
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    bus = MessageBus(runtime_root=root)
    manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(root, turns), work_dir=root),
                           runtime_root=root)
    manager._bus = bus
    router = SimpleNamespace(has_planner=lambda task_id: False)
    replies = []
    async with manager_endpoint(manager, bus, router, root):
        for index, text in enumerate(messages):
            await manager._handle(MessageEnvelope.create(
                source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": text, "channel_context": {"session_id": "chat-1"}}, correlation_id=f"corr-{index}",
                metadata={"origin_channel_id": "channel-web"},
            ))
            queue = bus.get_queue(WEB.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    replies.append(envelope.payload["text"])
        await manager.stop()
    await bus.shutdown()
    return SimpleNamespace(
        replies=replies, turns=[json.loads(line) for line in (root / "turns.jsonl").read_text().splitlines()],
        tools=json.loads((root / "tools.json").read_text()), argv=json.loads((root / "argv.json").read_text()))


def test_the_managers_calls_create_a_new_project_only_after_the_next_message_confirms_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import manager_actions, projects, tasks

    create = {"requirement": "build tetris", "title": "俄罗斯方块", "project": "Tetris", "work_dir": "/srv/tetris"}

    async def test(pool: Any) -> None:
        chat = await _converse(tmp_path, [[("looper_task_create", create)],
                                          [("looper_confirm", {"confirmation_id": "<confirmation>"})]],
                               ["建一个俄罗斯方块任务，放在新项目 Tetris 里，目录 /srv/tetris，确认", "确认"])
        assert chat.tools == ["looper_overview", "looper_tasks", "looper_task_create", "looper_task_control",
                              "looper_task_instruct", "looper_task_update", "looper_confirm"]
        assert "--strict-mcp-config" in chat.argv and "--mcp-config" in chat.argv
        asked, created = chat.replies
        # Claude's words come first, then what Looper writes itself.
        (answer,) = json.loads(asked.split("\n\n")[0])
        assert answer["status"] == "needs_confirmation"
        assert "需要你确认：\n项目「Tetris」还不存在" in asked
        assert answer["confirmation_id"] in chat.turns[1] and chat.turns[1].endswith("确认")
        (done,) = json.loads(created.split("\n\n")[0])
        task_id = done["result"]["task_id"]
        assert "任务已创建，还没有开始" in created and task_id in created
        project = await projects.find_project_by_name("Tetris", pool=pool)
        task = await tasks.get_task(task_id, pool=pool)
        assert (task["project_id"], task["work_dir"], task["status"], task["correlation_id"]) == (
            project["id"], "/srv/tetris", "init", "corr-1")
        trail = await manager_actions.for_task(task_id, pool=pool)
        assert [(row["status"], row["channel"]) for row in trail] == [("ok", "web")]

    _run(test, monkeypatch)


def test_a_manager_without_a_turn_running_only_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A call that comes while no message is being answered (as in a turn Claude runs on its own) only reads."""

    async def test(pool: Any) -> None:
        from looper.agents.ops.service import ManagerOps, Platform

        ops = ManagerOps(Platform(bus=MessageBus(runtime_root=tmp_path), task_router=None), url="http://x")
        assert (await ops.call("looper_tasks", {"action": "list"}))["status"] == "ok"
        answer = await ops.call("looper_task_create", {"requirement": "r", "title": "t", "project": "p"})
        assert answer["error"]["code"] == "needs_user_turn"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    _run(test, monkeypatch)
