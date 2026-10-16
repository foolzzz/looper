"""A task's Planner's own tools on a real PostgreSQL: its token works for its task only; it chooses its Workers' and
Reviewers' Agent Profiles from its project's list, under the user's choice for the task, from the next run on; and
it changes its task's and its project's concurrency at once, journaled, telling the task's chat.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address, planner_address, worker_address
from tests.test_ops_tasks_db import _error, _project, _task
from tests.test_task_agents_db import _LISTENING, _call, _Pool, _profiles, _Rechecks
from tests.test_usage_api_db import DSN, _run, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _ops(bus: Any = None, **parts: Any) -> Any:
    """``PlannerOps`` on a platform whose TaskRouter and WorkerPool count the rechecks they are asked for (or
    ``parts``')."""
    from looper.agents.ops.planner import PlannerOps

    platform = SimpleNamespace(bus=bus, task_router=parts.get("task_router") or _Rechecks(),
                               worker_pool=parts.get("worker_pool") or _Rechecks())
    return PlannerOps(platform, url="http://127.0.0.1:9", dashboard_url="http://looper.test")


class _Planner:
    """One task's Planner as its tools see it: its token, and whether Looper's turn runs."""

    def __init__(self, ops: Any, task_id: str) -> None:
        self.ops, self.token = ops, ops.issue(task_id)
        ops.begin_turn(self.token)

    async def __call__(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.ops.call(f"Bearer {self.token}", name, arguments)


async def _steps(pool: Any, task_id: str, **statuses: str) -> None:
    """Steps of ``task_id``, by label (its id is step-<label>) and status, in that order."""
    for index, (label, status) in enumerate(statuses.items()):
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, status) "
                           "VALUES ($1, $2, $3, $4, 'do it', $5)", f"step-{label}", task_id, index, label, status)


async def _haiku(pool: Any) -> None:
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                       "('agent-haiku', 'claude-code/haiku', 'rt-claude-code', 'prov-claude-subscription', 'haiku')")


async def _allow(project: dict[str, Any], *profiles: str) -> dict[str, Any]:
    status, answer = await _call("PUT", f"/api/projects/{project['id']}/allowed-profiles", {"profile_ids": list(profiles)})
    assert status == 200, answer
    return answer


async def _runs_on(task_id: str, project: dict[str, Any], step: str = "", role: str = "worker") -> tuple[str, str]:
    """The profile a run of ``role`` for ``step`` (its label) would start with now, and where it comes from."""
    from looper.agents.agent_settings import load_agent_settings

    settings = await load_agent_settings(role, project["id"], task_id, step_id=f"step-{step}" if step else None)
    return settings.profile.name, settings.profile_source.value


async def _journal(pool: Any, task_id: str, kind: str) -> list[tuple[str, str]]:
    return [(r["step_id"], r["summary"]) for r in await pool.fetch(
        "SELECT step_id, summary FROM task_journal WHERE task_id = $1 AND kind = $2 ORDER BY id", task_id, kind)]


# --- The token ---


async def _endpoint(server: Any, token: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    writer = _Writer()
    raw = b"" if body is None else json.dumps(body).encode()
    await server._handle_agent_api(method, f"/api/agent/v1/{path}", {"authorization": f"Bearer {token}"}, raw,
                                   writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_a_planners_token_works_for_its_own_task_and_its_own_tools_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.ops.service import ManagerOps, Platform
    from looper.agents.tiers.task import TaskRouter

    async def test(pool: Any) -> None:
        project = await _project(pool)
        for task_id in ("task-a", "task-b"):
            await _task(pool, task_id, "running", project)
        router = TaskRouter(runtime_root=tmp_path)
        manager = ManagerOps(Platform(bus=None, task_router=router), url="http://127.0.0.1:9", token="manager-tok")
        planners = _ops()
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(), task_router=router,
                                 manager_ops=manager, planner_ops=planners)
        token = planners.issue("task-a")

        # Each token lists its own agent's tools.
        status, listed = await _endpoint(server, token, "GET", "tools")
        assert status == 200 and [t["name"] for t in listed["tools"]] == [
            "looper_task_profiles", "looper_task_profile_set", "looper_task_concurrency", "looper_project_concurrency"]
        status, listed = await _endpoint(server, "manager-tok", "GET", "tools")
        assert status == 200 and "looper_task_profiles" not in {t["name"] for t in listed["tools"]}

        # The Planner's task only, its tools only; nor the Planner's tools for the Manager.
        status, answer = await _endpoint(server, token, "POST", "ops/looper_task_profiles", {"task_id": "task-b"})
        assert status == 200 and _error(answer) == "forbidden" and "not on task-b" in answer["error"]["message"]
        status, answer = await _endpoint(server, token, "POST", "ops/looper_task_profiles", {"task_id": "task-a"})
        assert answer["status"] == "ok" and answer["result"]["allowed"] == []
        status, answer = await _endpoint(server, token, "POST", "ops/looper_tasks", {"action": "list"})
        assert _error(answer) == "forbidden" and "is a tool of the Manager" in answer["error"]["message"]
        status, answer = await _endpoint(server, "manager-tok", "POST", "ops/looper_task_profile_set",
                                         {"role": "worker", "profile_id": "x"})
        assert _error(answer) == "forbidden" and "a tool of a task's Planner" in answer["error"]["message"]

        # Outside a turn Looper sent it, the Planner reads but changes nothing.
        status, answer = await _endpoint(server, token, "POST", "ops/looper_task_concurrency",
                                         {"limits": {"worker": 1}})
        assert _error(answer) == "needs_user_turn"
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0

        # A new Planner of the task replaces the token; the one that stops ends its own only.
        newer = planners.issue("task-a")
        assert (await _endpoint(server, token, "GET", "tools"))[0] == 401
        assert (await planners.call(f"Bearer {token}", "looper_task_profiles", {}))["error"]["code"] == "forbidden"
        planners.revoke(token)
        assert (await _endpoint(server, newer, "GET", "tools"))[0] == 200
        planners.revoke(newer)
        assert (await _endpoint(server, newer, "GET", "tools"))[0] == 401

        # Once the task ends, its Planner's tools stop working, a token still held included.
        held = planners.issue("task-b")
        await pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-b'")
        status, answer = await _endpoint(server, held, "POST", "ops/looper_task_profiles", {})
        assert _error(answer) == "forbidden" and "task task-b has ended" in answer["error"]["message"]
        assert (await _endpoint(server, held, "GET", "tools"))[0] == 401

    _run(test, monkeypatch)


# --- looper_task_profiles and looper_task_profile_set ---


def test_with_the_projects_list_empty_the_planner_chooses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _profiles(pool)
        planner = _Planner(_ops(), "task-a")
        listed = (await planner("looper_task_profiles", {}))["result"]
        assert listed["allowed"] == [] and "its list is empty" in listed["note"]
        assert listed["roles"]["worker"] == {"runs": {"name": "claude-code/opus", "source": "builtin"},
                                             "user_choice": None, "your_choice": None}
        for arguments in ({"role": "worker", "profile_id": "claude-code/sonnet"},
                          {"role": "reviewer", "profile_id": None}):
            answer = await planner("looper_task_profile_set", arguments)
            assert _error(answer) == "forbidden" and "(its list is empty)" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 0
        assert await _runs_on("task-a", project) == ("claude-code/opus", "builtin")

    _run(test, monkeypatch)


def test_the_projects_list_is_set_whole_by_id_or_name_and_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _profiles(pool)
        await _haiku(pool)
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES "
                           "('rt-resident', 'resident', 'claude-code', ARRAY['resident_session', 'resume'])")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('agent-resident', 'resident/opus', 'rt-resident', 'prov-claude-subscription', 'opus')")
        path = f"/api/projects/{project['id']}/allowed-profiles"
        assert await _call("GET", path) == (200, {"project_id": project["id"], "profiles": []})

        answer = await _allow(project, "Claude-Code/Sonnet", "agent-haiku", "agent-sonnet")
        assert [(p["profile_id"], p["roles"]) for p in answer["profiles"]] == [
            ("agent-haiku", ["worker", "reviewer"]), ("agent-sonnet", ["worker", "reviewer"])]
        assert set(answer["profiles"][0]) == {"profile_id", "name", "runtime", "model", "effort", "roles",
                                              "usd_per_million_tokens"}
        # Claude Code's own login runs an alias as Anthropic's model, at its price.
        assert [p["usd_per_million_tokens"] for p in answer["profiles"]] == [
            {"input": 1.0, "output": 5.0}, {"input": 2.0, "output": 10.0}]
        assert await _call("GET", path) == (200, answer)

        # A bad list changes nothing.
        for body, status, message in [
            ({"profile_ids": ["agent-haiku", "nope"]}, 400, "no Agent Profile 'nope'; registered: "),
            ({"profile_ids": ["resident/opus"]}, 400, "'resident/opus' can run neither the worker nor the reviewer"),
            ({"profile_ids": "agent-haiku"}, 400, "profile_ids must be a list"),
            ({"profiles": []}, 400, '{"profile_ids": [...]}'),
        ]:
            got, refused = await _call("PUT", path, body)
            assert got == status and message in refused["error"], refused
        assert await _call("GET", path) == (200, answer)
        assert (await _call("PUT", "/api/projects/proj-nope/allowed-profiles", {"profile_ids": []}))[0] == 404
        assert (await _call("GET", "/api/projects/proj-nope/allowed-profiles"))[0] == 404
        assert (await _allow(project))["profiles"] == []

    _run(test, monkeypatch)


def test_the_planners_choice_runs_from_the_next_run_on_and_a_steps_choice_wins_over_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    recording = f"import json, sys\nopen({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n" + _LISTENING

    def models() -> list[str]:
        runs = [json.loads(line) for line in argv_log.read_text().splitlines()]
        return [argv[argv.index("--model") + 1] for argv in runs]

    async def send(runs: _Pool, worker_id: str, step: str) -> None:
        await runs.bus.publish(MessageEnvelope.create(
            source=planner_address("task-a"), target=worker_address(worker_id), message_type=MessageType.WORK_ITEM,
            payload={"task_id": f"step-{step}", "parent_task_id": "task-a", "instruction": "do it",
                     "context": {"work_dir": str(tmp_path / "work")}}))
        await runs.settled(1, 0)
        await runs.finish(worker_id)

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="dispatched", s2="pending", s3="pending")
        await _profiles(pool)
        await _haiku(pool)
        await _allow(project, "agent-sonnet", "agent-haiku")
        planner = _Planner(_ops(), "task-a")
        async with _Pool(tmp_path, recording) as runs:
            await runs.bus.publish(MessageEnvelope.create(
                source=planner_address("task-a"), target=worker_address("w1"), message_type=MessageType.WORK_ITEM,
                payload={"task_id": "step-s1", "parent_task_id": "task-a", "instruction": "do it",
                         "context": {"work_dir": str(tmp_path / "work")}}))
            await runs.settled(1, 0)
            # While s1 runs: sonnet for the rest of the task, haiku for s3 (each attempt of it).
            answer = await planner("looper_task_profile_set", {"role": "worker", "profile_id": "claude-code/sonnet",
                                                               "reason": "the next steps are hard"})
            assert answer["result"]["next_run"] == {"name": "claude-code/sonnet", "source": "planner_task"}
            answer = await planner("looper_task_profile_set", {"role": "worker", "profile_id": "agent-haiku",
                                                               "step": "s3"})
            assert answer["result"]["next_run"] == {"name": "claude-code/haiku", "source": "planner_step"}
            await runs.finish("w1")  # the run that had started keeps its profile
            await send(runs, "w2", "s2")
            await send(runs, "w3", "s3")
            await send(runs, "w4", "s3")  # a retry of s3
        assert models() == ["opus", "sonnet", "haiku", "haiku"]
        assert await _runs_on("task-a", project, "s2") == ("claude-code/sonnet", "planner_task")
        assert await _runs_on("task-a", project, "s3") == ("claude-code/haiku", "planner_step")
        # The Reviewers keep theirs.
        assert await _runs_on("task-a", project, "s2", role="reviewer") == ("claude-code/opus", "builtin")
        listed = (await planner("looper_task_profiles", {}))["result"]
        assert listed["roles"]["worker"]["your_choice"] == "claude-code/sonnet"
        assert listed["steps"] == [{"step": "s3", "role": "worker", "profile": "claude-code/haiku"}]

    _run(test, monkeypatch)


def test_the_users_choice_for_the_task_wins_over_the_planners(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="pending")
        await _profiles(pool)
        await _allow(project, "agent-sonnet")
        planner = _Planner(_ops(), "task-a")
        assert (await planner("looper_task_profile_set", {"role": "worker", "profile_id": "agent-sonnet",
                                                          "step": "s1"}))["status"] == "ok"
        # The user chooses after the Planner did: theirs runs, for the step the Planner chose for too.
        assert (await _call("PUT", "/api/tasks/task-a/agents", {"profiles": {"worker": "one-off/opus"}}))[0] == 200
        assert await _runs_on("task-a", project, "s1") == ("one-off/opus", "task")
        # And the Planner cannot change the role the user chose for; the other one it can.
        answer = await planner("looper_task_profile_set", {"role": "worker", "profile_id": "agent-sonnet"})
        assert _error(answer) == "forbidden" and "the user chose one-off/opus for this task's Workers" in answer[
            "error"]["message"]
        assert (await planner("looper_task_profile_set", {"role": "reviewer", "profile_id": "agent-sonnet"}))[
            "status"] == "ok"
        assert await _runs_on("task-a", project, role="reviewer") == ("claude-code/sonnet", "planner_task")
        listed = (await planner("looper_task_profiles", {}))["result"]["roles"]["worker"]
        assert listed == {"runs": {"name": "one-off/opus", "source": "task"}, "user_choice": "one-off/opus",
                          "your_choice": None}

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    ({"role": "planner", "profile_id": "agent-sonnet"}, "invalid", "role must be one of: worker, reviewer"),
    ({"role": "worker", "profile_id": "agent-sonnet", "step": "s1"}, "invalid", "step s1 has ended (verified)"),
    ({"role": "worker", "profile_id": "agent-sonnet", "step": "s9"}, "invalid",
     "no step 's9' in this task's plan; the steps not done are: s2"),
    ({"role": "worker", "profile_id": "claude-code/opus"}, "forbidden",
     "'claude-code/opus' is not on the project's list; you may choose: claude-code/sonnet, one-off/opus"),
    ({"role": "worker"}, "invalid", "profile_id is required"),
    ({"role": "worker", "profile_id": "agent-sonnet", "reason": "x" * 501}, "invalid", "at most 500"),
    # A profile whose runtime can no longer run the role, with the reason the dashboard gives.
    ({"role": "worker", "profile_id": "one-off/opus"}, "invalid",
     "Agent Profile 'one-off/opus' cannot run the worker: its runtime 'one-off'"),
])
def test_a_choice_the_planner_may_not_make_is_refused_and_nothing_is_stored(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="verified", s2="pending")
        await _profiles(pool)
        await _allow(project, "agent-sonnet", "agent-one-off")
        await pool.execute("UPDATE agent_runtimes SET capabilities = ARRAY['resident_session'] WHERE id = 'rt-one-off'")
        answer = await _Planner(_ops(), "task-a")("looper_task_profile_set", arguments)
        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 0
        assert await _journal(pool, "task-a", "profile_choice") == []

    _run(test, monkeypatch)


def test_each_choice_is_journaled_and_the_task_shows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="pending")
        await _profiles(pool)
        await _allow(project, "agent-sonnet")
        planner = _Planner(_ops(), "task-a")
        for arguments in ({"role": "worker", "profile_id": "agent-sonnet", "reason": "the  steps\nare hard"},
                          {"role": "reviewer", "profile_id": "claude-code/sonnet", "step": "s1"},
                          {"role": "worker", "profile_id": None}):
            assert (await planner("looper_task_profile_set", arguments))["result"]["changed"] is True
        # Clearing what it never chose changes nothing and journals nothing.
        answer = (await planner("looper_task_profile_set", {"role": "worker", "profile_id": None}))["result"]
        assert answer["changed"] is False and "you had chosen nothing" in answer["note"]
        assert await _journal(pool, "task-a", "profile_choice") == [
            ("", "Workers of the rest of the task: claude-code/sonnet. Reason: the steps are hard"),
            ("step-s1", "Reviewers of step s1: claude-code/sonnet"),
            ("", "Workers of the rest of the task: the Planner's choice (claude-code/sonnet) cleared"),
        ]
        status, task = await _call("GET", "/api/tasks/task-a")
        shown = [(e["kind"], e["step_key"]) for e in task["journal"]]
        assert shown == [("profile_choice", ""), ("profile_choice", "s1"), ("profile_choice", "")]
        assert task["agents"]["roles"]["worker"]["profile"]["source"] == "builtin"
        await planner("looper_task_profile_set", {"role": "worker", "profile_id": "agent-sonnet"})
        status, task = await _call("GET", "/api/tasks/task-a")
        assert task["agents"]["roles"]["worker"]["profile"]["source"] == "planner_task"
        activity = [r["summary"] for r in await pool.fetch(
            "SELECT summary FROM activity WHERE task_id = 'task-a' ORDER BY seq")]
        assert activity[0] == ("Planner chose the Agent Profile: Workers of the rest of the task: claude-code/sonnet. "
                               "Reason: the steps are hard")

    _run(test, monkeypatch)


def test_the_planners_choice_counts_only_while_the_tasks_project_allows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project, other = await _project(pool), await _project(pool, "other")
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="pending")
        await _profiles(pool)
        await _allow(project, "agent-sonnet")
        planner = _Planner(_ops(), "task-a")
        choose = {"role": "worker", "profile_id": "agent-sonnet"}
        await planner("looper_task_profile_set", choose)
        assert await _runs_on("task-a", project) == ("claude-code/sonnet", "planner_task")
        # Moved to a project that does not allow it, the task runs what its settings give.
        await pool.execute("UPDATE tasks SET project_id = $1 WHERE id = 'task-a'", other["id"])
        assert await _runs_on("task-a", other) == ("claude-code/opus", "builtin")
        await pool.execute("UPDATE tasks SET project_id = $1 WHERE id = 'task-a'", project["id"])
        # Dropped from the list, the choice goes with it.
        await _allow(project)
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 0
        await _allow(project, "agent-sonnet")
        assert await _runs_on("task-a", project) == ("claude-code/opus", "builtin")
        # Planned anew, the task drops its Planner's choices; going on from its plan, it keeps them.
        await planner("looper_task_profile_set", {**choose, "step": "s1"})
        await planner("looper_task_profile_set", choose)
        await tasks.reset_task_for_restart("task-a", keep_steps=True)
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 2
        await tasks.reset_task_for_restart("task-a")
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 0

    _run(test, monkeypatch)


# --- looper_task_concurrency and looper_project_concurrency ---


async def _project_limits(pool: Any, project_id: str) -> dict[str, int]:
    return {r["role"]: r["max_concurrency"] for r in await pool.fetch(
        "SELECT role, max_concurrency FROM agent_setting_layers WHERE scope = 'role_project' AND project_id = $1 "
        "AND max_concurrency IS NOT NULL", project_id)}


def _notices(bus: MessageBus, channel: str) -> list[dict[str, Any]]:
    queue = bus.get_queue(channel_address(channel).agent_id)
    notices = []
    while not queue.empty():
        envelope = queue.get_nowait()
        assert envelope.message_type is MessageType.CHAT_NOTICE
        notices.append(envelope.payload)
    return notices


def test_the_planner_changes_its_tasks_and_its_projects_limits_at_once_and_the_chat_is_told(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await tasks.create_task(task_id="task-a", requirement="build calc", project_id=project["id"], work_dir="/w",
                                status="running", origin_channel_id="channel-lark", chat_id="ou_1", chat_kind="user",
                                title="计算器", pool=pool)
        await _task(pool, "task-web", "running", project)
        bus = MessageBus(runtime_root=tmp_path)
        router, workers = _Rechecks(), _Rechecks()
        ops = _ops(bus, task_router=router, worker_pool=workers)
        planner = _Planner(ops, "task-a")

        answer = (await planner("looper_task_concurrency", {"limits": {"worker": 1}, "reason": "one database"}))[
            "result"]
        assert answer["changes"][0]["before"] == {"value": 16, "source": "builtin"}
        assert answer["changes"][0]["after"] == {"value": 1, "source": "task"} and answer["user_told"] is True
        assert await pool.fetchval("SELECT max_concurrency FROM task_concurrency_limits WHERE task_id = 'task-a'") == 1
        answer = (await planner("looper_project_concurrency", {"limits": {"worker": 3, "planner": 2}}))["result"]
        assert (answer["scope"], answer["project"]) == ("project", "calc")
        # No confirmation: the project's limits are set at once.
        assert await _project_limits(pool, project["id"]) == {"worker": 3, "planner": 2}
        assert (router.rechecks, workers.rechecks) == (2, 2)
        assert await _journal(pool, "task-a", "concurrency_change") == [
            ("", "Workers at once in this task: 16 → 1. Reason: one database"),
            ("", "Workers at once in project calc: 16 → 3; tasks at once in project calc: 8 → 2"),
        ]
        notices = _notices(bus, "lark")
        assert [(n["chat"], n["kind"]) for n in notices] == [("ou_1", "user"), ("ou_1", "user")]
        assert notices[0]["text"] == (
            "⚙️ 规划者调整了并发上限 · 计算器\n- 本任务：Worker（同时运行的 Worker 数）16（内置默认） → 1（本任务）\n"
            "理由：one database\n[http://looper.test/#tasks/task-a](http://looper.test/#tasks/task-a)")
        assert "项目「calc」：Planner（同时运行的任务数）8（内置默认） → 2（本项目里的这个角色）" in notices[1]["text"]

        # A task from the web chat has no chat to tell: the journal records it all the same.
        web = _Planner(ops, "task-web")
        answer = (await web("looper_task_concurrency", {"limits": {"reviewer": 2}}))["result"]
        assert answer["user_told"] is False and "the user is told" not in answer["note"]
        assert len(await _journal(pool, "task-web", "concurrency_change")) == 1
        assert _notices(bus, "lark") == [] and _notices(bus, "web") == []
        await bus.shutdown()

    _run(test, monkeypatch)


def test_a_limit_the_planner_raises_starts_the_waiting_runs_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        async with _Pool(tmp_path) as runs:
            planner = _Planner(_ops(runs.bus, worker_pool=runs.pool), "task-a")
            await planner("looper_task_concurrency", {"limits": {"worker": 1}})
            for worker_id in ("w1", "w2", "w3"):
                await runs.send(worker_id, "task-a")
            await runs.settled(1, 2)
            await planner("looper_task_concurrency", {"limits": {"worker": 3}})
            running, _ = await runs.settled(3, 0)
            assert running == {"w1", "w2", "w3"}
            for worker_id in running:
                await runs.finish(worker_id)

    _run(test, monkeypatch)


@pytest.mark.parametrize(("tool", "arguments", "message"), [
    # A task's tool lists no planner: one Planner runs a task.
    ("looper_task_concurrency", {"limits": {"planner": 2, "worker": 1}},
     "limits has an unknown key 'planner'; its keys are: worker, reviewer"),
    ("looper_task_concurrency", {"limits": {"worker": 0}}, "limits.worker must be from 1 to 64"),
    ("looper_project_concurrency", {"limits": {"reviewer": 65}}, "limits.reviewer must be from 1 to 64"),
    ("looper_project_concurrency", {"limits": {}}, "give at least one role's limit"),
    ("looper_task_concurrency", {"reason": "x"}, "limits is required"),
    ("looper_project_concurrency", {"limits": {"manager": 1}}, "limits has an unknown key 'manager'"),
    ("looper_task_concurrency", {"scope": "task", "limits": {"worker": 1}}, "unknown argument 'scope'"),
])
def test_a_limit_the_planner_may_not_set_is_refused_and_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any], message: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        answer = await _Planner(_ops(), "task-a")(tool, arguments)
        assert _error(answer) == "invalid" and message in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0
        assert await _project_limits(pool, project["id"]) == {}
        assert await _journal(pool, "task-a", "concurrency_change") == []

    _run(test, monkeypatch)
