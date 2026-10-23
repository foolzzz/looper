"""An API client's operations on a real PostgreSQL: a task it creates is its own in the trail, its two tools read the
pipeline and the gates, and a client's chat thread gets its tasks' reports. Runs only when LOOPER_TEST_DATABASE_URL
points at a disposable database."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.api_v1 import OPERATOR, Principal
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Risk, Tool, Turn
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from tests.test_ops_tasks_db import _Chat, _project, _task
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

WEB = channel_address("web", "channel-web")


def _ops(tmp: Path, planners: set[str] | None = None) -> ClientOps:
    router = SimpleNamespace(has_planner=lambda task_id: task_id in (planners or set()))
    return ClientOps(Platform(bus=MessageBus(runtime_root=tmp), task_router=router))


async def _principal(pool: Any, name: str = "agent") -> Principal:
    from looper.db import api_clients

    view, _ = await api_clients.create(name, pool=pool)
    return Principal("client", view["id"], view["name"])


def test_a_client_creates_a_task_in_a_new_project_after_confirming(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    from looper.db import manager_actions, projects, tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        client = await _principal(pool)
        ops = _ops(tmp_path)
        asked = await ops.call(client, "s1", "looper_task_create", {
            "requirement": "build tetris", "title": "俄罗斯方块", "project": "Tetris", "work_dir": "/srv/tetris"})
        assert asked["status"] == "needs_confirmation" and "Tetris" in asked["impact"]
        assert await projects.find_project_by_name("Tetris", pool=pool) is None

        done = await ops.confirm(client, "s1", asked["confirmation_id"])
        assert done["status"] == "ok" and done["result"]["project_created"] is True
        assert "shown_to_user" not in done["result"] and done["notes"]
        task_id = done["result"]["task_id"]
        row = await tasks.get_task(task_id, pool=pool)
        assert (row["client_id"], row["manager_id"], row["origin_thread"]) == (client.client_id, "manager", None)

        rows = await pool.fetch("SELECT status, risk, client_id, channel, chat, manager_id, confirmation_id, task_id "
                                "FROM manager_actions ORDER BY id")
        assert [(r["status"], r["risk"], r["client_id"], r["channel"], r["chat"], r["manager_id"],
                 r["confirmation_id"], r["task_id"]) for r in rows] == [
            ("needs_confirmation", "high", client.client_id, "api", "s1", "manager", asked["confirmation_id"], None),
            ("ok", "high", client.client_id, "api", "s1", "manager", asked["confirmation_id"], task_id)]
        (trail,) = await manager_actions.for_task(task_id, pool=pool)
        assert trail["client_name"] == "agent"

        pipeline = await ops.call(client, "s1", "looper_task_pipeline", {"task_id": task_id})
        assert pipeline["status"] == "ok"
        assert (pipeline["result"]["task_id"], pipeline["result"]["steps"], pipeline["result"]["gates"],
                pipeline["result"]["waits"]) == (task_id, [], [], [])
        got = await ops.call(client, "s1", "looper_tasks", {"action": "get", "task_id": task_id})
        assert got["status"] == "ok" and got["result"]["name"] == "俄罗斯方块"
        missing = await ops.call(client, "s1", "looper_task_pipeline", {"task_id": "task-nope"})
        assert missing["error"]["code"] == "not_found"

    run_db(test, monkeypatch)


def test_gates_list_empty_and_reply_to_a_gate_that_does_not_exist(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-g1", "init", project)
        ops = _ops(tmp_path)
        listed = await ops.call(OPERATOR, "default", "looper_gates", {"action": "list"})
        assert listed["status"] == "ok"
        assert (listed["result"]["gates"], listed["result"]["total"]) == ([], 0)
        for arguments in ({"action": "reply", "task_id": "task-g1", "gate": "g", "text": "yes"},
                          {"action": "end", "task_id": "task-g1", "gate": "g"},
                          {"action": "messages", "task_id": "task-g1", "gate": "g"}):
            answer = await ops.call(OPERATOR, "default", "looper_gates", arguments)
            assert answer["error"]["code"] == "not_found", answer
        incomplete = await ops.call(OPERATOR, "default", "looper_gates", {"action": "reply", "task_id": "task-g1"})
        assert incomplete["error"]["code"] == "invalid" and "task_id and gate" in incomplete["error"]["message"]
        no_text = await ops.call(OPERATOR, "default", "looper_gates",
                                 {"action": "reply", "task_id": "task-g1", "gate": "g"})
        assert no_text["error"]["code"] == "invalid"
        # A list is a read: only the refused writes are in the trail.
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status = 'ok'") == 0

    run_db(test, monkeypatch)


async def _manager_create(chat: _Chat, *, client_id: str | None) -> dict[str, Any]:
    envelope = MessageEnvelope.create(
        source=WEB, target=WEB, message_type=MessageType.USER_MESSAGE,
        payload={"text": "确认", "channel_context": {"session_id": "cli-x"}},
        correlation_id="c-cli", metadata={"origin_channel_id": "channel-web"})
    turn = Turn(channel="web", chat="cli-x", message_id="m1", client_id=client_id, envelope=envelope)
    chat.ops.begin_turn(turn)
    try:
        return await chat.ops.call("looper_task_create", {"requirement": "build", "title": "计算器", "project": "calc"})
    finally:
        chat.ops.end_turn(turn)


def test_the_manager_answering_a_clients_chat_gives_the_task_its_client_and_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        client = await _principal(pool)
        chat = _Chat(tmp_path)
        with_client = await _manager_create(chat, client_id=client.client_id)
        row = await tasks.get_task(with_client["result"]["task_id"], pool=pool)
        assert (row["client_id"], row["origin_thread"], row["manager_id"]) == (client.client_id, "cli-x", "manager")
        action = await pool.fetchrow("SELECT client_id, channel FROM manager_actions WHERE status = 'ok'")
        assert (action["client_id"], action["channel"]) == (client.client_id, "web")
        # The dashboard's own chat for the default Manager is unchanged.
        without = await _manager_create(_Chat(tmp_path), client_id=None)
        row = await tasks.get_task(without["result"]["task_id"], pool=pool)
        assert (row["client_id"], row["origin_thread"]) == (None, None)

    run_db(test, monkeypatch)


def test_a_call_that_fails_unexpectedly_stays_in_the_trail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def broken(call: Any) -> dict[str, Any]:
        raise RuntimeError("disk full")

    async def ask(call: Any) -> str | None:
        return "do it?" if call.arguments.get("confirm") else None

    schema = {"type": "object", "properties": {"confirm": {"type": "boolean"}}}

    async def test(pool: Any) -> None:
        client = await _principal(pool)
        ops = ClientOps(_ops(tmp_path)._platform, tools=(Tool("t_broken", "fails", schema, Risk.WRITE, broken,
                                                               escalate=ask),))
        failed = await ops.call(client, "s1", "t_broken", {})
        assert failed["error"]["code"] == "unavailable"
        asked = await ops.call(client, "s1", "t_broken", {"confirm": True})
        confirmed = await ops.confirm(client, "s1", asked["confirmation_id"])
        assert confirmed["error"]["code"] == "unavailable"
        rows = await pool.fetch("SELECT status, error_code, confirmation_id, client_id FROM manager_actions ORDER BY id")
        assert [(r["status"], r["error_code"], r["confirmation_id"], r["client_id"]) for r in rows] == [
            ("error", "unavailable", None, client.client_id),
            ("needs_confirmation", None, asked["confirmation_id"], client.client_id),
            ("error", "unavailable", asked["confirmation_id"], client.client_id)]

    run_db(test, monkeypatch)
