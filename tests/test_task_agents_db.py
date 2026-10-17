"""A task's own choices per role on a real PostgreSQL: its concurrency limit, which the WorkerPool holds its
Workers to (a project counts nothing: each task its own), and its Agent Profile, which its next step runs; the task
API sets and shows both, and the Manager's ``looper_concurrency`` changes a task's limit at once and refuses a
project's, which no longer exists.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from tests.test_ops_tasks_db import _Chat, _error, _project, _task
from tests.test_usage_api_db import DSN, _run, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# A Worker that runs until it gets a user turn, then reports.
_LISTENING = (
    "import json, sys\n"
    "sys.stdin.readline()\n"
    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
    "sys.stdin.readline()\n"
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
    "'result': json.dumps({'status': 'completed', 'changed_files': {}, 'summary': 'done'})}), flush=True)\n"
    "sys.stdin.read()\n"
)


class _Rechecks:
    """A WorkerPool stand-in that counts the rechecks it is asked for (or a TaskRouter's, which is asked for none)."""

    def __init__(self, **parts: Any) -> None:
        self.rechecks = 0
        self.__dict__.update(parts)

    async def recheck(self) -> None:
        self.rechecks += 1


def _server(worker_pool: Any = None) -> Any:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    return AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                           task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())), worker_pool=worker_pool)


async def _call(method: str, path: str, body: Any = None, *, worker_pool: Any = None) -> tuple[int, Any]:
    writer = _Writer()
    raw = b"" if body is None else json.dumps(body).encode()
    await _server(worker_pool)._route_api(method, path, {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _until(check: Callable[[], bool], what: str, seconds: float = 15) -> None:
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _Pool:
    """A real WorkerPool on a fake ``claude`` whose runs wait for a user turn, fed work items for tasks."""

    def __init__(self, tmp: Path, body: str = _LISTENING) -> None:
        from looper.agents.tiers.worker import WorkerConfig, WorkerPool

        self.tmp = tmp
        (tmp / "work").mkdir(exist_ok=True)
        self.bus = MessageBus(runtime_root=tmp)
        self.pool = WorkerPool(config=WorkerConfig(claude_bin=fake_claude(tmp / "claude", body)),
                               runtime_root=tmp / "runtime")

    async def __aenter__(self) -> _Pool:
        await self.pool.start(self.bus)
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.pool.stop()
        await self.bus.shutdown()

    async def send(self, worker_id: str, task_id: str) -> None:
        await self.bus.publish(MessageEnvelope.create(
            source=planner_address(task_id), target=worker_address(worker_id), message_type=MessageType.WORK_ITEM,
            payload={"task_id": f"step-{worker_id}", "parent_task_id": task_id, "instruction": "do it",
                     "context": {"work_dir": str(self.tmp / "work")}}))

    def running(self) -> set[str]:
        return {w for w, wp in self.pool._active.items() if wp.status == "running" and self.pool.accepts_input(w)}

    def waiting(self) -> set[str]:
        return {w for w, wp in self.pool._active.items() if wp.status == "pending"}

    async def settled(self, running: int, waiting: int) -> tuple[set[str], set[str]]:
        """Once ``running`` runs run and ``waiting`` wait, and a moment more for any run over a limit to start."""
        await _until(lambda: len(self.running()) == running and len(self.waiting()) == waiting,
                     f"{running} running and {waiting} waiting: {self.pool.status()}")
        await asyncio.sleep(0.3)
        return self.running(), self.waiting()

    async def finish(self, worker_id: str) -> None:
        assert await self.pool.send_input(worker_id, "finish")
        await _until(lambda: worker_id not in self.pool._active, f"{worker_id} to end")


async def _waits(pool: Any, task_id: str) -> list[str]:
    return [r["summary"] for r in await pool.fetch(
        "SELECT summary FROM activity WHERE task_id = $1 AND summary LIKE '%waits for room%' ORDER BY seq", task_id)]


def test_a_tasks_limit_holds_its_workers_and_the_next_starts_as_one_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "running", project)
        status, _ = await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 1}})
        assert status == 200
        async with _Pool(tmp_path) as runs:
            for worker_id in ("w1", "w2", "w3"):
                await runs.send(worker_id, "task-a")
            running, waiting = await runs.settled(1, 2)
            # Another task of the same project is not held by task-a's limit.
            await runs.send("b1", "task-b")
            running, waiting = await runs.settled(2, 2)
            assert "b1" in running and len(running & {"w1", "w2", "w3"}) == 1
            for _ in range(2):
                (first,) = running - {"b1"}
                await runs.finish(first)
                running, waiting = await runs.settled(2, len(waiting) - 1)
                assert first not in running and len(running - {"b1"}) == 1
        # The step says why it has not started, once per waiting run, naming the task's limit.
        assert await _waits(pool, "task-a") == ["Worker waits for room in this task (1 running, limit 1)"] * 2
        assert await _waits(pool, "task-b") == []

    _run(test, monkeypatch)


def test_two_tasks_of_one_project_each_run_their_own_limit_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        for task_id in ("task-a", "task-b"):
            await _task(pool, task_id, "running", project)
            assert (await _call("PUT", f"/api/tasks/{task_id}/agents", {"concurrency": {"worker": 2}}))[0] == 200
        async with _Pool(tmp_path) as runs:
            for worker_id in ("a1", "a2", "a3", "b1", "b2", "b3"):
                await runs.send(worker_id, f"task-{worker_id[0]}")
            running, waiting = await runs.settled(4, 2)
        # Four Workers of one project at once: two in each task, one of each waiting.
        assert sorted(w[0] for w in running) == ["a", "a", "b", "b"] and sorted(w[0] for w in waiting) == ["a", "b"]
        for task_id in ("task-a", "task-b"):
            assert await _waits(pool, task_id) == ["Worker waits for room in this task (2 running, limit 2)"]

    _run(test, monkeypatch)


def test_a_tasks_own_limit_above_the_default_applies_and_a_task_without_one_runs_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "running", project)
        assert (await _call("PUT", "/api/agent-settings/roles/worker", {"max_concurrency": 1}))[0] == 200
        # Nothing caps a task's own limit but the most a limit may be: 3 over the default 1 is taken as it is.
        status, answer = await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 3}})
        assert status == 200 and answer["agents"]["roles"]["worker"]["concurrency"] == {
            "value": 3, "source": "task", "task": 3, "default": {"value": 1, "source": "role"}}
        async with _Pool(tmp_path) as runs:
            for worker_id in ("a1", "a2", "a3", "b1", "b2"):
                await runs.send(worker_id, f"task-{worker_id[0]}")
            running, waiting = await runs.settled(4, 1)
        assert sorted(running) == ["a1", "a2", "a3", "b1"] or sorted(running) == ["a1", "a2", "a3", "b2"]
        assert await _waits(pool, "task-b") == ["Worker waits for room in this task (1 running, limit 1)"]

    _run(test, monkeypatch)


def test_raising_a_tasks_limit_starts_its_waiting_runs_at_once_and_lowering_it_stops_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        async with _Pool(tmp_path) as runs:
            assert (await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 1}}, worker_pool=runs.pool))[0] \
                == 200
            for worker_id in ("w1", "w2", "w3"):
                await runs.send(worker_id, "task-a")
            await runs.settled(1, 2)
            # No run ends: the raise alone starts the two that wait.
            assert (await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 3}}, worker_pool=runs.pool))[0] \
                == 200
            await runs.settled(3, 0)
            assert (await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 1}}, worker_pool=runs.pool))[0] \
                == 200
            running, _ = await runs.settled(3, 0)
            # Over the lowered limit, a new run waits until fewer than one run.
            await runs.send("w4", "task-a")
            await runs.settled(3, 1)
            for worker_id in sorted(running)[:2]:
                await runs.finish(worker_id)
            await runs.settled(1, 1)
            await runs.finish(sorted(running)[2])
            await runs.settled(1, 0)

    _run(test, monkeypatch)


async def _profiles(pool: Any) -> None:
    """claude-code/sonnet, and a profile on a runtime that runs one-off runs only (no Planner, no Manager)."""
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                       "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', 'sonnet')")
    await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES "
                       "('rt-one-off', 'one-off', 'claude-code', ARRAY['run'])")
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                       "('agent-one-off', 'one-off/opus', 'rt-one-off', 'prov-claude-subscription', 'opus')")


@pytest.mark.parametrize(("body", "message"), [
    ({"concurrency": {"worker": 0}}, "concurrency.worker: max_concurrency must be a whole number from 1 to 64"),
    ({"concurrency": {"worker": 65}}, "got 65"),
    ({"concurrency": {"reviewer": True}}, "got True"),
    ({"concurrency": {"worker": "2"}}, "got '2'"),
    ({"concurrency": {"worker": 2.0}}, "got 2.0"),
    ({"concurrency": {"planner": 2}}, "concurrency: one Planner runs a task, so a task has no Planner limit, and "
                                      "nothing limits how many tasks run at once"),
    ({"concurrency": {"manager": 1}}, "concurrency: unknown role 'manager'"),
    ({"concurrency": [2]}, "concurrency must be an object"),
    ({"profiles": {"worker": "nope"}}, "profiles.worker: no Agent Profile 'nope'; registered: "),
    ({"profiles": {"planner": "one-off/opus"}}, "profiles.planner: Agent Profile 'one-off/opus' cannot run the "
                                                "planner"),
    ({"profiles": {"manager": "claude-code/sonnet"}}, "not 'manager'"),
    ({"profiles": {"worker": 3}}, "profiles.worker: give an Agent Profile's id or name, or null"),
    ({"profiles": "claude-code/sonnet"}, "profiles must be an object"),
    # One bad value refuses the whole change: nothing of it is stored.
    ({"profiles": {"worker": "claude-code/sonnet"}, "concurrency": {"worker": 0}}, "got 0"),
    ({"profiles": {"worker": "claude-code/sonnet", "reviewer": "nope"}, "concurrency": {"worker": 2}}, "'nope'"),
    ({}, "give at least one role's profiles or concurrency"),
    ({"concurrency": {}}, "give at least one role's profiles or concurrency"),
    ({"worker": 2}, "unknown field 'worker'; the fields are profiles, concurrency"),
    ([1], "body must be a JSON object"),
])
def test_a_bad_choice_is_refused_and_nothing_is_stored(
    monkeypatch: pytest.MonkeyPatch, body: Any, message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await _profiles(pool)
        status, answer = await _call("PUT", "/api/tasks/task-a/agents", body)
        assert status == 400 and message in answer["error"], answer
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0
        assert await pool.fetchval("SELECT count(*) FROM task_profile_overrides") == 0

    _run(test, monkeypatch)


def test_a_new_task_chooses_its_profiles_by_id_or_name_as_the_agents_endpoint_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _profiles(pool)

        async def create(profiles: dict[str, Any]) -> tuple[int, Any]:
            return await _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": project["id"],
                                                             "profile_overrides": profiles})

        status, answer = await create({"worker": "Claude-Code/Sonnet", "planner": "agent-sonnet", "reviewer": None})
        assert status == 201, answer
        assert dict(await pool.fetch("SELECT role, profile_id FROM task_profile_overrides WHERE task_id = $1",
                                     answer["task_id"])) == {"worker": "agent-sonnet", "planner": "agent-sonnet"}
        # An unknown profile is refused as the agents endpoint refuses it, under the field the request named.
        status, created = await create({"worker": "nope"})
        refused = (await _call("PUT", "/api/tasks/task-a/agents", {"profiles": {"worker": "nope"}}))[1]["error"]
        assert (status, created["error"]) == (400, "profile_overrides." + refused.removeprefix("profiles."))
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 2

    _run(test, monkeypatch)


@pytest.mark.parametrize("body", [{"concurrency": {"worker": 2}}, {"profiles": {"worker": "claude-code/sonnet"}}])
def test_every_task_but_a_completed_one_changes_its_choices(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _profiles(pool)
        status, answer = await _call("PUT", "/api/tasks/task-nope/agents", body)
        assert (status, answer) == (404, {"error": "task not found"})
        await _task(pool, "task-completed", "completed", project)
        status, answer = await _call("PUT", "/api/tasks/task-completed/agents", body)
        assert status == 409 and "the task is completed" in answer["error"]
        # A failed, stopped or cancelled task changes too: its restart runs with the new choices.
        can_run = ("init", "pending", "running", "executing", "failed", "stopped", "cancelled")
        for state in can_run:
            await _task(pool, f"task-{state}", state, project)
            status, answer = await _call("PUT", f"/api/tasks/task-{state}/agents", body)
            assert (status, answer["agents"]["editable"]) == (200, True), (state, answer)
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") + await pool.fetchval(
            "SELECT count(*) FROM task_profile_overrides") == len(can_run)

    _run(test, monkeypatch)


def test_the_task_shows_each_roles_profile_and_limit_with_their_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.agent_settings import Layer, load_agent_settings

    async def agents(task_id: str) -> dict[str, Any]:
        status, task = await _call("GET", f"/api/tasks/{task_id}")
        assert status == 200
        return task["agents"]

    async def limit(task_id: str, role: str) -> dict[str, Any]:
        return (await agents(task_id))["roles"][role]["concurrency"]

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _profiles(pool)
        builtin = {"value": 16, "source": "builtin"}
        shown = await agents("task-a")
        assert (shown["concurrency_max"], shown["editable"]) == (64, True)
        assert {role: (v["profile"]["value"]["name"], v["profile"]["source"]) for role, v in shown["roles"].items()} \
            == {role: ("claude-code/opus", "builtin") for role in ("planner", "worker", "reviewer")}
        # The Planner has no limit: one Planner runs a task, and nothing limits how many tasks run.
        assert {role: v.get("concurrency") for role, v in shown["roles"].items()} == {
            "planner": None,
            "worker": {**builtin, "task": None, "default": builtin},
            "reviewer": {"value": 8, "source": "builtin", "task": None, "default": {"value": 8, "source": "builtin"}},
        }
        assert (await _call("PUT", "/api/agent-settings/roles/worker", {"max_concurrency": 10}))[0] == 200
        role = {"value": 10, "source": "role"}
        assert await limit("task-a", "worker") == {**role, "task": None, "default": role}
        status, answer = await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": 2}})
        assert await limit("task-a", "worker") == {"value": 2, "source": "task", "task": 2, "default": role}
        assert answer["agents"] == await agents("task-a") and "note" not in answer
        # Cleared, the task follows the default again.
        await _call("PUT", "/api/tasks/task-a/agents", {"concurrency": {"worker": None}})
        assert await limit("task-a", "worker") == {**role, "task": None, "default": role}

        # A profile, by name or by id, and a limit of the same role are one task layer, set together.
        status, answer = await _call("PUT", "/api/tasks/task-a/agents", {
            "profiles": {"worker": "Claude-Code/Sonnet", "planner": "agent-sonnet"},
            "concurrency": {"worker": 1, "reviewer": 4}})
        assert status == 200 and "from its next step on" in answer["note"]
        roles = (await agents("task-a"))["roles"]
        assert (roles["worker"]["profile"]["value"]["profile_id"], roles["worker"]["profile"]["source"]) == (
            "agent-sonnet", "task")
        assert (roles["planner"]["profile"]["source"], roles["reviewer"]["profile"]["source"]) == ("task", "builtin")
        assert (roles["worker"]["concurrency"]["task"], roles["reviewer"]["concurrency"]["task"]) == (1, 4)
        settings = await load_agent_settings("worker", project["id"], "task-a")
        assert (settings.profile.id, settings.profile_source) == ("agent-sonnet", Layer.TASK)
        assert set((await _call("GET", "/api/tasks/task-a"))[1]["profile_overrides"]) == {"worker", "planner"}
        # Dropping the task's profile leaves its limit, and the other way round.
        await _call("PUT", "/api/tasks/task-a/agents", {"profiles": {"worker": None}})
        roles = (await agents("task-a"))["roles"]
        assert (roles["worker"]["profile"]["source"], roles["worker"]["concurrency"]["task"]) == ("builtin", 1)
        await pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-a'")
        assert (await agents("task-a"))["editable"] is False

    _run(test, monkeypatch)


def test_a_running_tasks_new_worker_profile_runs_from_its_next_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    recording = f"import json, sys\nopen({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n" + _LISTENING

    def models() -> list[str]:
        runs = [json.loads(line) for line in argv_log.read_text().splitlines()]
        return [argv[argv.index("--model") + 1] for argv in runs]

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await _profiles(pool)
        async with _Pool(tmp_path, recording) as runs:
            await runs.send("w1", "task-a")
            await runs.settled(1, 0)
            # The step that runs keeps the profile it started with; the next one runs the new one.
            status, answer = await _call("PUT", "/api/tasks/task-a/agents",
                                         {"profiles": {"worker": "claude-code/sonnet"}})
            assert status == 200, answer
            await runs.finish("w1")
            await runs.send("w2", "task-a")
            await runs.settled(1, 0)
            await runs.finish("w2")
        assert models() == ["opus", "sonnet"]

    _run(test, monkeypatch)


# --- The Manager's looper_concurrency ---


def _concurrency_chat(tmp: Path, **parts: Any) -> _Chat:
    """A Lark chat whose platform counts the rechecks a change asks for."""
    chat = _Chat(tmp, **parts)
    chat.router = _Rechecks(has_planner=lambda task_id: False)
    chat.workers = _Rechecks()
    chat.ops._platform = SimpleNamespace(bus=chat.bus, task_router=chat.router, worker_pool=chat.workers)
    return chat


def test_a_tasks_limit_changes_at_once_and_reads_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import manager_actions

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        chat = _concurrency_chat(tmp_path)
        (answer,) = await chat.say("把任务 task-a 的 Worker 并发改成 1", ("looper_concurrency", {
            "task_id": "task-a", "limits": {"worker": 1}}))
        assert answer["status"] == "ok", answer
        assert answer["result"]["changes"] == [{
            "role": "worker", "before": {"value": 16, "source": "builtin"}, "after": {"value": 1, "source": "task"},
            "task_limit": 1, "default": {"value": 16, "source": "builtin"}}]
        assert chat.turns[0].blocks == [] and chat.workers.rechecks == 1
        assert await pool.fetchval("SELECT max_concurrency FROM task_concurrency_limits WHERE task_id = 'task-a' "
                                   "AND role = 'worker'") == 1
        (trail,) = await manager_actions.for_task("task-a", pool=pool)
        assert (trail["tool"], trail["risk"], trail["status"]) == ("looper_concurrency", "write", "ok")

        # Over the default, the task's own applies as it is.
        await pool.execute("INSERT INTO agent_setting_layers (scope, role, max_concurrency) VALUES "
                           "('role', 'reviewer', 2)")
        (answer,) = await chat.say("Reviewer 并发 5", ("looper_concurrency", {
            "task_id": "task-a", "limits": {"reviewer": 5}}))
        (change,) = answer["result"]["changes"]
        assert (change["before"], change["after"], change["task_limit"], change["default"]) == (
            {"value": 2, "source": "role"}, {"value": 5, "source": "task"}, 5, {"value": 2, "source": "role"})

        # looper_tasks get and looper_overview show the task's own limits.
        (got,) = await chat.say("看看 task-a", ("looper_tasks", {"action": "get", "task_id": "task-a"}))
        assert got["result"]["concurrency"]["worker"] == {
            "value": 1, "source": "task", "task": 1, "default": {"value": 16, "source": "builtin"}}
        assert set(got["result"]["concurrency"]) == {"worker", "reviewer"}
        chat.router.running_planners = lambda: [{"planner_id": "planner-task-a", "task_id": "task-a",
                                                 "project_id": project["id"], "requirement": "r", "name": "a"}]
        chat.workers.runs = lambda: []
        chat.ops._platform.manager = None
        chat.ops._platform.supervisor = SimpleNamespace(agent_status=lambda: {})
        (overview,) = await chat.say("现在怎么样", ("looper_overview", {}))
        assert overview["result"]["running_tasks"] == [
            {"task_id": "task-a", "name": "a", "project": "calc", "concurrency": {"reviewer": 5, "worker": 1}}]

    _run(test, monkeypatch)


@pytest.mark.parametrize("ended", ["failed", "stopped", "cancelled"])
def test_the_manager_changes_the_agents_of_a_task_that_ended_but_not_its_requirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ended: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _profiles(pool)
        await _task(pool, "task-a", ended, project)
        await _task(pool, "task-done", "completed", project)
        chat = _concurrency_chat(tmp_path)
        profile, limit, requirement, completed = await chat.say(
            "失败的任务换成 sonnet、Worker 并发 1，再重启",
            ("looper_task_update", {"task_id": "task-a", "profiles": {"worker": "claude-code/sonnet"}}),
            ("looper_concurrency", {"task_id": "task-a", "limits": {"worker": 1}}),
            # One refused part refuses the whole call: its profile is not stored either.
            ("looper_task_update", {"task_id": "task-a", "requirement": "build calc v2",
                                    "profiles": {"reviewer": "claude-code/sonnet"}}),
            ("looper_task_update", {"task_id": "task-done", "profiles": {"worker": "claude-code/sonnet"}}))
        assert profile["status"] == "ok" and profile["result"]["changed"] == ["profiles"], profile
        assert limit["status"] == "ok" and limit["result"]["changes"][0]["after"] == {"value": 1, "source": "task"}
        assert _error(requirement) == "conflict" and "requirement and directory do not change" in \
            requirement["error"]["message"]
        assert _error(completed) == "conflict" and "the task is completed" in completed["error"]["message"]
        assert [tuple(r) for r in await pool.fetch("SELECT task_id, role, profile_id FROM task_profile_overrides")] \
            == [("task-a", "worker", "agent-sonnet")]
        assert [tuple(r) for r in await pool.fetch("SELECT task_id, role, max_concurrency FROM "
                                                   "task_concurrency_limits")] == [("task-a", "worker", 1)]
        assert await pool.fetchval("SELECT requirement FROM tasks WHERE id = 'task-a'") == "build calc"

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    # A project has no limits, nor does the Planner: the call is refused, and nothing is asked or changed.
    ({"project": "calc", "limits": {"worker": 2}}, "invalid", "unknown argument 'project'"),
    ({"task_id": "task-a", "project": "calc", "limits": {"worker": 2}}, "invalid", "unknown argument 'project'"),
    ({"task_id": "task-a", "limits": {"planner": 2}}, "invalid", "unknown key 'planner'; its keys are: worker, "
                                                                 "reviewer"),
    ({"limits": {"worker": 2}}, "invalid", "task_id is required"),
    ({"task_id": "task-a", "limits": {}}, "invalid", "at least one role"),
    ({"task_id": "task-a", "limits": {"worker": 0}}, "invalid", "limits.worker must be from 1 to 64"),
    ({"task_id": "task-a", "limits": {"worker": True}}, "invalid", "limits.worker must be an integer"),
    ({"task_id": "task-a", "limits": {"worker": "2"}}, "invalid", "limits.worker must be an integer"),
    ({"task_id": "task-a", "limits": {"manager": 2}}, "invalid", "unknown key 'manager'"),
    ({"task_id": "task-done", "limits": {"worker": 2}}, "conflict", "the task is completed"),
    ({"task_id": "task-nope", "limits": {"worker": 2}}, "not_found", "no task task-nope"),
])
def test_the_manager_is_told_why_a_change_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-done", "completed", project)
        chat = _concurrency_chat(tmp_path)
        (answer,) = await chat.say("改并发", ("looper_concurrency", arguments))
        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert chat.turns[0].blocks == [] and (chat.router.rechecks, chat.workers.rechecks) == (0, 0)
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE max_concurrency IS NOT NULL") == 0
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status = 'needs_confirmation'") == 0

    _run(test, monkeypatch)


def test_a_turn_the_manager_runs_on_its_own_changes_no_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.ops import Turn

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        chat = _concurrency_chat(tmp_path)
        chat.ops.begin_turn(Turn(channel=None, chat=None))
        answer = await chat.ops.call("looper_concurrency", {"task_id": "task-a", "limits": {"worker": 1}})
        assert _error(answer) == "needs_user_turn"
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0

    _run(test, monkeypatch)

