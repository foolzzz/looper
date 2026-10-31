"""The task tool persists progress routing, including through a baseline schema upgrade."""

from types import SimpleNamespace

import pytest

from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import connection, projects, tasks
from tests.db_support import run_db


@pytest.mark.parametrize("channel,group", [("telegram", False), ("telegram", True),
                                          ("lark", False), ("lark_bot", True)])
def test_task_creation_keeps_progress_destination(monkeypatch, channel, group):
    async def run(pool):
        await projects.create_project(name="calc", work_dir="/w", pool=pool)
        chat = "-99" if group and channel == "telegram" else "99" if channel == "telegram" else "oc_original"
        channel_id = f"channel-{channel}"
        question = MessageEnvelope.create(
            source=channel_address(channel), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "create a task", "channel_context": {"chat_id": chat, "message_id": "om_original"}},
            metadata={"origin_channel_id": channel_id})
        ops = ManagerOps(Platform(bus=None, task_router=SimpleNamespace()), url="http://127.0.0.1:9")
        turn = Turn(channel=channel, chat=chat, sender="ou_1", group=group, channel_id=channel_id,
                    message_id=question.message_id, envelope=question)
        ops.begin_turn(turn)
        try:
            answer = await ops.call("looper_task_create", {
                "title": "Progress", "requirement": "finish", "project": "calc"})
            assert answer["status"] == "ok", answer
        finally:
            ops.end_turn(turn)
        row = await tasks.get_task(answer["result"]["task_id"], pool=pool)
        expected = {"chat_id": chat, **({"reply_to": "om_original"} if channel != "telegram" else {})}
        assert row["origin_reply_route"] == expected
        # Lark direct-message notifications store the sender, which is not the progress reply destination.
        if channel != "telegram" and not group:
            assert row["chat_id"] == "ou_1" and row["chat_id"] != expected["chat_id"]
        restored = Task.from_row(row["id"], row)
        assert TaskRouter._report_metadata(question, restored)["reply_route"] == expected

    run_db(run, monkeypatch, timeout=20)


def test_existing_baseline_upgrades_once_and_preserves_tasks(monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="old", work_dir="/w", pool=pool)
        await tasks.create_task(task_id="task-old", requirement="finish", project_id=project["id"], pool=pool)
        await pool.execute("ALTER TABLE tasks DROP COLUMN origin_reply_route")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert (await tasks.get_task("task-old", pool=pool))["origin_reply_route"] is None
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'task_reply_route'") == 1

    run_db(run, monkeypatch, timeout=20)
