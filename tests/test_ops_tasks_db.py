"""The Manager's task tools on a real PostgreSQL: what each call changes, refuses, asks first, and leaves in the trail.

Each call goes through ``ManagerOps`` as the endpoint hands it over, in a turn
of a Lark chat; the bus is real, the TaskRouter a stand-in that says which
tasks have a Planner. Runs only when LOOPER_TEST_DATABASE_URL points at a
disposable database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops.tasks import START_QUESTION
from tests.test_usage_api_db import DSN, _get, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LARK = channel_address("lark", "channel-lark")


class _Chat:
    """One Lark chat with the Manager: each ``say`` is a turn in which the given calls run."""

    def __init__(self, tmp: Path, planners: set[str] | None = None) -> None:
        self.bus = MessageBus(runtime_root=tmp)
        self.planners = planners if planners is not None else set()
        router = SimpleNamespace(has_planner=lambda task_id: task_id in self.planners)
        self.ops = ManagerOps(Platform(bus=self.bus, task_router=router), url="http://127.0.0.1:9", token="tok")
        self.turns: list[Turn] = []
        self.notes: list[str] = []

    async def say(self, text: str, *calls: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
        envelope = MessageEnvelope.create(
            source=LARK, target=LARK, message_type=MessageType.USER_MESSAGE,
            payload={"text": text, "channel_context": {"chat_id": "oc_1", "sender_id": "ou_1"}},
            correlation_id=f"corr-{len(self.turns)}", metadata={"origin_channel_id": "channel-lark"},
        )
        turn = Turn(channel="lark", chat="oc_1", message_id=envelope.message_id, envelope=envelope, sender="ou_1")
        self.turns.append(turn)
        self.notes.append(self.ops.begin_turn(turn))
        try:
            return [await self.ops.call(name, arguments) for name, arguments in calls]
        finally:
            self.ops.end_turn(turn)

    def sent(self, agent_id: str = "task-router") -> list[MessageEnvelope]:
        queue, envelopes = self.bus.get_queue(agent_id), []
        while not queue.empty():
            envelopes.append(queue.get_nowait())
        return envelopes


async def _project(pool: Any, name: str = "calc", work_dir: str = "/w") -> dict[str, Any]:
    from looper.db import projects

    return await projects.create_project(name=name, work_dir=work_dir, pool=pool)


async def _task(pool: Any, task_id: str, status: str, project: dict[str, Any], requirement: str = "build calc") -> None:
    from looper.db import tasks

    await tasks.create_task(task_id=task_id, requirement=requirement, project_id=project["id"], work_dir="/w",
                            status=status, pool=pool)


def _error(answer: dict[str, Any]) -> str:
    assert answer["status"] == "error", answer
    return answer["error"]["code"]


def test_a_task_is_created_ready_in_its_project_and_its_configuration_shown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import agent_setting_layers, manager_actions, tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', "
                           "'sonnet')")
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("确认", ("looper_task_create", {
            "requirement": "build a calculator", "title": "计算器", "project": "CALC",
            "profiles": {"worker": "Claude-Code/Sonnet"}}))
        assert answer["status"] == "ok"
        result = answer["result"]
        task_id = result["task_id"]
        assert (result["status"], result["project"], result["project_created"], result["work_dir"]) == (
            "ready", "calc", False, "/w")
        row = await tasks.get_task(task_id, pool=pool)
        assert (row["status"], row["requirement"], row["title"], row["work_dir"], row["project_id"]) == (
            "init", "build a calculator", "计算器", "/w", project["id"])
        # The end notice goes to the Lark sender; progress to the chat the task was asked for in.
        assert (row["chat_id"], row["origin_channel_id"], row["correlation_id"], row["original_user_text"]) == (
            "ou_1", "channel-lark", "corr-0", "确认")
        assert (await agent_setting_layers.profile_overrides(task_id, pool=pool))["worker"]["id"] == "agent-sonnet"
        # Looper shows the configuration itself, after Claude's words; nothing is started.
        (block,) = chat.turns[0].blocks
        lines = block.splitlines()
        assert lines[:4] == ["任务已创建，还没有开始（Ready — not started）：", f"- 任务：计算器（{task_id}）",
                             "- 项目：calc（已有）", "- 目录：/w"]
        assert "- Worker：claude-code/sonnet（effort 默认，来源：本任务），agent teams 关" in lines
        assert lines[-1] == START_QUESTION
        assert chat.sent() == []
        (trail,) = await manager_actions.for_task(task_id, pool=pool)
        assert (trail["tool"], trail["risk"], trail["status"], trail["channel"]) == (
            "looper_task_create", "write", "ok", "lark")
        # The task page shows what the Manager did.
        status, detail = await _get(f"/api/tasks/{task_id}")
        assert status == 200
        assert [(a["tool"], a["status"]) for a in detail["manager_actions"]] == [("looper_task_create", "ok")]

    _run(test, monkeypatch)


def test_a_new_project_is_created_only_when_the_next_message_confirms_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import projects, tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        chat = _Chat(tmp_path)
        create = ("looper_task_create", {"requirement": "build tetris", "title": "俄罗斯方块", "project": "Tetris",
                                         "work_dir": "/srv/tetris"})
        (asked,) = await chat.say("确认", create)
        assert asked["status"] == "needs_confirmation"
        assert await projects.find_project_by_name("Tetris", pool=pool) is None
        (block,) = chat.turns[0].blocks
        assert "项目「Tetris」还不存在" in block and "/srv/tetris" in block and "calc（/w）" in block
        (done,) = await chat.say("确认新建", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert asked["confirmation_id"] in chat.notes[1]
        assert done["status"] == "ok" and done["result"]["project_created"] is True
        project = await projects.find_project_by_name("tetris", pool=pool)
        assert project["work_dir"] == "/srv/tetris"
        assert (await tasks.get_task(done["result"]["task_id"], pool=pool))["project_id"] == project["id"]
        assert "- 项目：Tetris（新建）" in chat.turns[1].blocks[0]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "message"), [
    ({"project": "Tetris"}, "a new project"),  # a new project needs a directory
    ({"project": "notes"}, "no default"),  # so does a project without a default one
    ({"project": "calc", "work_dir": "relative/dir"}, "work_dir"),
    ({"project": "  "}, "project must not be empty"),
    ({"project": "x" * 81}, "ask the user which project"),
    ({"project": "calc", "profiles": {"worker": "claude-code/nope"}}, "registered: claude-code/opus"),
    ({"project": "calc", "profiles": {"worker": None}}, "give an Agent Profile"),
])
def test_a_task_without_what_only_the_user_can_give_is_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool)
        await _project(pool, "notes", "")
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("确认", ("looper_task_create", {"requirement": "r", "title": "t", **arguments}))
        assert _error(answer) == "invalid" and message in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert chat.turns[0].blocks == []

    _run(test, monkeypatch)


@pytest.mark.parametrize(("status", "action", "outcome"), [
    ("init", "start", MessageType.TASK_START),
    ("running", "start", "conflict"),
    ("stopped", "start", "conflict"),
    ("running", "stop", MessageType.TASK_STOP),
    ("pending", "stop", MessageType.TASK_STOP),
    ("init", "stop", "conflict"),
    ("stopped", "restart", MessageType.TASK_RESTART),
    ("failed", "restart", MessageType.TASK_RESTART),
    ("verifying", "restart", "conflict"),
    ("completed", "restart", "conflict"),
])
def test_task_control_follows_the_tasks_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, action: str, outcome: Any,
) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", status, await _project(pool))
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("go", ("looper_task_control", {"task_id": "task-a", "action": action}))
        if isinstance(outcome, MessageType):
            assert answer["status"] == "ok"
            assert [(e.message_type, e.payload) for e in chat.sent()] == [(outcome, {"task_id": "task-a"})]
        else:
            assert _error(answer) == outcome and chat.sent() == []

    _run(test, monkeypatch)


def test_cancelling_asks_first_and_only_a_running_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "executing", project)
        await _task(pool, "task-q", "pending", project)
        chat = _Chat(tmp_path)
        (queued,) = await chat.say("取消排队的", ("looper_task_control", {"task_id": "task-q", "action": "cancel"}))
        assert _error(queued) == "conflict" and chat.turns[0].blocks == []
        (asked,) = await chat.say("取消它", ("looper_task_control", {"task_id": "task-a", "action": "cancel"}))
        assert asked["status"] == "needs_confirmation" and chat.sent() == []
        assert "要取消任务「build calc」（task-a，现在 executing）吗？" in chat.turns[1].blocks[0]
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok"
        assert [(e.message_type, e.payload) for e in chat.sent()] == [(MessageType.TASK_CANCEL, {"task_id": "task-a"})]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("status", "planner", "outcome"), [
    ("executing", True, "ok"),
    ("pending", False, "ok"),  # queued: its Planner reads it at its first turn
    ("init", False, "conflict"),
    ("completed", False, "conflict"),
])
def test_a_supplement_goes_to_a_task_under_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, planner: bool, outcome: str,
) -> None:
    from looper.db import task_changes

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", status, await _project(pool))
        chat = _Chat(tmp_path, planners={"task-a"} if planner else set())
        (answer,) = await chat.say("导出改用 CSV", ("looper_task_instruct", {"task_id": "task-a",
                                                                         "text": "导出改用 CSV"}))
        pending = await task_changes.pending("task-a", pool=pool)
        woken = [e.message_type for e in chat.sent("planner-task-a")]
        if outcome == "ok":
            assert answer["status"] == "ok"
            assert [(s.text, s.via) for s in pending.supplements] == [("导出改用 CSV", "manager:lark")]
            assert woken == ([MessageType.TASK_CHANGE] if planner else [])
        else:
            assert _error(answer) == outcome and pending.empty and woken == []

    _run(test, monkeypatch)


def test_updating_a_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import agent_setting_layers, task_changes, tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-r", "init", project)
        await _task(pool, "task-v", "verifying", project)
        await _task(pool, "task-c", "completed", project)
        chat = _Chat(tmp_path, planners={"task-v"})
        moved, nothing, ended, dir_of_running = await chat.say(
            "改一下",
            ("looper_task_update", {"task_id": "task-r", "requirement": "build calc v2", "work_dir": "/elsewhere",
                                    "profiles": {"reviewer": "claude-code/opus"}}),
            ("looper_task_update", {"task_id": "task-r"}),
            ("looper_task_update", {"task_id": "task-c", "requirement": "again"}),
            ("looper_task_update", {"task_id": "task-v", "work_dir": "/elsewhere"}),
        )
        assert moved["status"] == "ok" and moved["result"]["changed"] == ["profiles", "work_dir", "requirement"]
        row = await tasks.get_task("task-r", pool=pool)
        assert (row["requirement"], row["work_dir"]) == ("build calc v2", "/elsewhere")
        # A task not started plans the new text: nothing is left for a Planner to get.
        assert (await task_changes.pending("task-r", pool=pool)).empty
        assert list(await agent_setting_layers.profile_overrides("task-r", pool=pool)) == ["reviewer"]
        assert (_error(nothing), _error(ended), _error(dir_of_running)) == ("invalid", "conflict", "conflict")

        # A running task's edit is a change its Planner gets in a turn; a profile can be dropped again.
        (edited, cleared) = await chat.say(
            "再改", ("looper_task_update", {"task_id": "task-v", "requirement": "build calc, with a CLI"}),
            ("looper_task_update", {"task_id": "task-r", "profiles": {"reviewer": None}}))
        assert edited["result"]["requirement_version"] == 2 and "requirement_note" in edited["result"]
        assert (await task_changes.pending("task-v", pool=pool)).requirement.text == "build calc, with a CLI"
        assert [e.message_type for e in chat.sent("planner-task-v")] == [MessageType.TASK_CHANGE]
        assert cleared["status"] == "ok" and await agent_setting_layers.profile_overrides("task-r", pool=pool) == {}

    _run(test, monkeypatch)


def test_tasks_are_listed_searched_and_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import task_changes

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "executing", project, requirement="build calc")
        await _task(pool, "task-b", "init", project, requirement="write the docs")
        await task_changes.add_supplement("task-a", "use CSV", via="chat", pool=pool)
        chat = _Chat(tmp_path)
        listed, searched, ready, got, missing = await chat.say(
            "看看", ("looper_tasks", {"action": "list"}), ("looper_tasks", {"action": "list", "query": "DOCS"}),
            ("looper_tasks", {"action": "list", "group": "ready"}), ("looper_tasks", {"action": "get",
                                                                                      "task_id": "task-a"}),
            ("looper_tasks", {"action": "get", "task_id": "task-zzz"}))
        assert [t["task_id"] for t in listed["result"]["tasks"]] == ["task-b", "task-a"]
        assert [t["task_id"] for t in searched["result"]["tasks"]] == ["task-b"]
        assert [t["task_id"] for t in ready["result"]["tasks"]] == ["task-b"]
        task = got["result"]
        assert (task["status"], task["group"], task["project"], task["requirement"]) == (
            "executing", "in_progress", "calc", "build calc")
        assert task["supplements"] == [{"text": "use CSV", "delivered": False}]
        assert _error(missing) == "not_found"

    _run(test, monkeypatch)


@pytest.mark.parametrize(("title", "stored", "problem"), [
    ("  俄罗斯方块\n小游戏 ", "俄罗斯方块 小游戏", None),  # its words on one line
    ("x" * 70, "x" * 60, None),
    (None, None, "title is required"),
    (["not", "text"], None, "title must be a string"),
])
def test_a_task_is_named_by_its_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, title: Any,
                                      stored: str | None, problem: str | None) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("确认", ("looper_task_create", {"requirement": "r", "project": "calc",
                                                                  **({"title": title} if title is not None else {})}))
        if problem is not None:
            assert _error(answer) == "invalid" and answer["error"]["message"] == problem
            return
        task_id = answer["result"]["task_id"]
        assert (await tasks.get_task(task_id, pool=pool))["title"] == stored
        assert f"- 任务：{stored}（{task_id}）" in chat.turns[0].blocks[0]

    _run(test, monkeypatch)
