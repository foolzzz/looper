"""Project cascade deletion, retries, refusal ordering and confirmation on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from looper.agents import projects, schedules, task_workspaces
from looper.agents.bus import MessageBus
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address, planner_address
from looper.db import plans, projects as db_projects, schedules as db_schedules, tasks
from tests.db_support import DSN, run_db
from tests.schedule_support import Bus
from tests.test_api_v1_project_role_templates_db import _api
from tests.test_ops_tasks_db import _Chat
from tests.test_step_workspaces_db import _workspace
from tests.test_task_workspaces_db import _repo, git

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SOURCE = channel_address("web")
EMPTY_IMPACT = (
    "要删除项目「Example」（默认目录：没有）吗？它没有任务和定时任务。它的 Agent 设置、规划者可选的 Agent Profile 列表、"
    "技能和 MCP 的安装、项目知识和它自己写的技能会一起删除，不能恢复；目录里的文件不受影响。"
)


@pytest.fixture(autouse=True)
def _git_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


async def _task(pool, project, task_id, status="completed", *, manager_id="manager", **fields):
    return await tasks.create_task(task_id=task_id, requirement="Build the example", project_id=project["id"],
                                   work_dir=fields.pop("work_dir", ""), status=status, manager_id=manager_id,
                                   pool=pool, **fields)


async def _schedule(pool, project, *, manager_id="manager", task_id=None):
    made = await schedules.create_schedule(project["id"], {
        "name": f"Every minute {manager_id}", "cron": "* * * * *", "timezone": "UTC", "requirement": "Read reports",
        "work_dir": "/example", "manager_id": manager_id, "gate_command": "echo example"})
    sid = made["schedule"]["schedule_id"]
    now = datetime.now(timezone.utc)
    await db_schedules.insert_run(pool, schedule_id=sid, scheduled_for=now, trigger="manual", status="completed",
                                  task_id=task_id, finished_at=now)
    return sid


async def _delete(project, root, bus=None, **kwargs):
    return await projects.delete_project(project["id"], bus or Bus(), SOURCE, root, **kwargs)


def test_empty_project_keeps_its_exact_confirmation_and_result(monkeypatch, tmp_path):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        async with _api(tmp_path, pool) as call:
            status, asked = await call("project_admin.delete", {"project_id": project["id"]}, client=True)
            assert status == 202 and asked["impact"] == EMPTY_IMPACT, asked
            status, done = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 200 and done["result"] == {"deleted": project["id"]}, done
        assert await db_projects.get_project(project["id"], pool=pool) is None

    run_db(run, monkeypatch, timeout=30)


def test_mixed_tasks_schedules_worktrees_and_sessions_are_removed_but_result_branch_stays(monkeypatch, tmp_path):
    repo, root = _repo(tmp_path / "repo"), tmp_path / "runtime"

    async def run(pool):
        from looper.agents.room import SESSION_KEEPING_RUNTIMES, run_session_dir

        project = await db_projects.create_project(name="Example", work_dir=str(repo), pool=pool)
        for task_id, state in (("task-ready", tasks.READY), ("task-done", "completed"),
                               ("task-failed", "failed"), ("task-worktree", "stopped")):
            await _task(pool, project, task_id, state, work_dir=str(repo), workspace_dir=str(repo),
                        workspace_mode="worktree" if task_id == "task-worktree" else "in_place")
        async def note(*args):
            pass
        checkout = Path(await task_workspaces.prepare(await tasks.get_task("task-worktree", pool=pool),
                                                       runtime_root=root, links=(), note=note))
        (checkout / "result.txt").write_text("Keep this result.\n")
        await plans.save("task-worktree", steps=[{"id": "task-worktree-s1", "index": 0, "key": "s1",
                                                  "instruction": "Build it"}], pool=pool)
        step = await _workspace(str(checkout), root, "task-worktree", "s1")
        sessions = []
        for runtime in SESSION_KEEPING_RUNTIMES:
            path = run_session_dir(root, runtime, "task-worktree", "run-1")
            path.mkdir(parents=True)
            (path / "session.json").write_text("{}")
            sessions.append(path)
        sid = await _schedule(pool, project, task_id="task-done")
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 1
        assert checkout.exists() and Path(step.path).exists() and all(path.exists() for path in sessions)

        assert await _delete(project, root) == {"deleted": project["id"]}
        for table in ("tasks", "task_steps", "task_worktrees", "step_workspaces", "schedules", "schedule_runs", "projects"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table
        assert not checkout.exists() and not Path(step.path).exists()
        assert all(not path.exists() for path in sessions)
        assert git(repo, "show", "looper/task-worktree/task:result.txt") == "Keep this result."

    run_db(run, monkeypatch, timeout=45)


@pytest.mark.parametrize("end_kind", ["stop", "completed", "failed"])
def test_running_task_is_stopped_through_router_and_deleted_only_after_its_agent_ends(monkeypatch, tmp_path, end_kind):
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import Task, TaskRouter
    from tests.test_manager_stop import _Session

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-running", "running")
        await _schedule(pool, project)
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        planner = PlannerAgent(task_id="task-running", runtime_root=tmp_path)
        session = _Session(hold=False)
        session.started = True
        planner._claude = session
        stopping, release = asyncio.Event(), asyncio.Event()
        original_stop = session.stop
        async def held_stop(**kwargs):
            stopping.set()
            await release.wait()
            await original_stop(**kwargs)
        monkeypatch.setattr(session, "stop", held_stop)
        seen, stop_sent = [], asyncio.Event()
        def observe_stop(envelope):
            seen.append(envelope)
            stop_sent.set()
        bus.subscribe_topic(MessageType.TASK_STOP.value, observe_stop)
        await router.start(bus)
        await planner.start(bus)
        router._tasks["task-running"] = Task(task_id="task-running", requirement="Build", goal_id="", work_dir="",
                                              project_id=project["id"], status="running")
        router._planners["task-running"] = planner
        deleting = None
        try:
            if end_kind != "stop":
                await bus.publish(MessageEnvelope.create(
                    source=planner_address("task-running"), target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"task_id": "task-running", "status": end_kind, "summary": "The Planner ended."}))
                await asyncio.wait_for(stopping.wait(), 5)
            deleting = asyncio.create_task(_delete(project, tmp_path, bus, task_router=router))
            await asyncio.wait_for(stopping.wait(), 5)
            await asyncio.wait_for(stop_sent.wait(), 5)
            assert not deleting.done()
            assert (await tasks.get_task("task-running", pool=pool))["status"] in tasks.ACTIVE_STATUSES
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
            assert [e.payload["task_id"] for e in seen] == ["task-running"]
            assert session.started  # Cleanup is still held; the row must survive until it ends.
            release.set()
            assert await asyncio.wait_for(deleting, 5) == {"deleted": project["id"]}
            assert "task-running" not in router._tasks and not router.has_planner("task-running")
            assert not planner._running and not session.started
            assert await tasks.get_task("task-running", pool=pool) is None
        finally:
            release.set()
            if deleting is not None:
                if not deleting.done():
                    deleting.cancel()
                await asyncio.gather(deleting, return_exceptions=True)
            await router.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=20)


def test_stop_timeout_removes_schedules_but_keeps_all_tasks_and_retry_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "PROJECT_DELETE_STOP_WAIT_SECONDS", 1, raising=False)

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-running", "running")
        await _task(pool, project, "task-stopping", "stopping")
        await _task(pool, project, "task-done")
        await _schedule(pool, project)
        bus = Bus()  # A fake runtime that receives the stop and does not stop.
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path, bus)
        assert raised.value.status == projects.HTTP_409
        message = str(raised.value)
        assert "2 task(s) of project 'Example' are still stopping" in message
        assert "task-running" in message and "task-stopping" in message
        assert message.endswith("delete the project again to finish")
        assert bus.of("task_stop") == ["task-running"]
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 3
        assert await db_projects.get_project(project["id"], pool=pool)
        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE status = ANY($1::text[])", list(tasks.ACTIVE_STATUSES))
        assert await _delete(project, tmp_path, bus) == {"deleted": project["id"]}
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    run_db(run, monkeypatch, timeout=15)


def test_kept_task_worktree_reports_partial_progress_and_retry_finishes(monkeypatch, tmp_path):
    repo = _repo(tmp_path / "repo")

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-first")
        await _task(pool, project, "task-kept", "stopped", work_dir=str(repo), workspace_dir=str(repo),
                    workspace_mode="worktree")
        await _task(pool, project, "task-last")
        async def note(*args):
            pass
        checkout = Path(await task_workspaces.prepare(await tasks.get_task("task-kept", pool=pool),
                                                       runtime_root=tmp_path, links=(), note=note))
        (checkout / "result.txt").write_text("Keep this work.\n")
        git(checkout, "checkout", "--detach", "HEAD")  # The user changed the task checkout's branch.
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path)
        assert raised.value.status == projects.HTTP_409
        assert str(raised.value).startswith("task task-kept kept its worktree, so nothing is lost: ")
        assert "no longer on its branch looper/task-kept/task" in str(raised.value)
        assert str(raised.value).endswith(
            "; 1 of 3 tasks were deleted. Resolve it, then delete the project again")
        assert await tasks.get_task("task-first", pool=pool) is None
        assert await tasks.get_task("task-kept", pool=pool) and await tasks.get_task("task-last", pool=pool)
        assert await db_projects.get_project(project["id"], pool=pool)
        assert (checkout / "result.txt").read_text() == "Keep this work.\n"
        git(checkout, "checkout", "looper/task-kept/task")
        assert await _delete(project, tmp_path) == {"deleted": project["id"]}
        assert not checkout.exists()
        assert git(repo, "show", "looper/task-kept/task:result.txt") == "Keep this work."

    run_db(run, monkeypatch, timeout=30)


def test_unknown_and_template_refusals_happen_before_any_stop_or_delete(monkeypatch, tmp_path):
    async def run(pool):
        bus = Bus()
        with pytest.raises(projects.RequestError) as raised:
            await projects.delete_project("missing", bus, SOURCE, tmp_path)
        assert raised.value.status == projects.HTTP_404
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-running", "running")
        await _schedule(pool, project)
        await pool.execute("INSERT INTO manager_task_templates (manager_id, key, name, project_id) "
                           "VALUES ('manager', 'example', 'Example', $1)", project["id"])
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path, bus)
        assert raised.value.status == projects.HTTP_409 and "used by template" in str(raised.value)
        assert bus.sent == []
        assert await tasks.get_task("task-running", pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 1
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 1
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.delete", {"project_id": project["id"]}, client=True)
            assert status == 409 and answer["status"] == "error", answer
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("Delete", ("looper_projects", {"action": "delete", "project": "Example"}))
        assert answer["status"] == "error" and chat.turns[0].blocks == [], answer

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("other", ["task", "schedule", None])
def test_scoped_manager_refuses_other_owners_without_counts_and_deletes_its_own(monkeypatch, tmp_path, other):
    from tests.test_scenario_manager_tools_db import REVIEW, _review

    async def run(pool):
        chat, _ = await _review(pool, tmp_path, tools="all", task_scope="own")
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-own", manager_id=REVIEW)
        await _schedule(pool, project, manager_id=REVIEW)
        if other == "task":
            await _task(pool, project, "task-other", "running")
        elif other == "schedule":
            sid = await _schedule(pool, project)
            await pool.execute("UPDATE schedules SET name='Other schedule' WHERE id=$1", sid)
        (answer,) = await chat.say("Delete", ("looper_projects", {"action": "delete", "project": "Example"}))
        if other:
            assert answer["status"] == "error", answer
            assert answer["error"]["message"] == (
                "project 'Example' is still used outside this Manager: it cannot be deleted from here")
            assert chat.turns[0].blocks == [] and chat.sent() == []
            assert await tasks.get_task("task-own", pool=pool)
            assert await pool.fetchval("SELECT count(*) FROM schedules") == (2 if other == "schedule" else 1)
        else:
            assert answer["status"] == "needs_confirmation", answer
            assert "1 个定时任务" in chat.turns[0].blocks[0]
            (done,) = await chat.say("Confirm", ("looper_confirm", {"confirmation_id": answer["confirmation_id"]}))
            assert done == {"status": "ok", "result": {"deleted": "Example"}}, done
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
            assert await db_projects.get_project(project["id"], pool=pool) is None

    run_db(run, monkeypatch, timeout=30)


def test_task_added_before_project_row_delete_returns_a_retryable_conflict(monkeypatch, tmp_path):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-old")
        delete_row = db_projects.delete_project
        async def inserted(project_id, **kwargs):
            assert await tasks.get_task("task-old", pool=pool) is None
            await _task(pool, project, "task-new")
            return await delete_row(project_id, **kwargs)
        monkeypatch.setattr(db_projects, "delete_project", inserted)
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path)
        assert raised.value.status == projects.HTTP_409
        assert str(raised.value) == (
            "a task or schedule was added to project 'Example' while it was being deleted: delete it again")
        assert await db_projects.get_project(project["id"], pool=pool)
        assert await tasks.get_task("task-new", pool=pool)

    run_db(run, monkeypatch, timeout=30)


def test_two_deletes_of_one_project_return_conflict_without_waiting_for_the_first(monkeypatch, tmp_path):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-done")
        entered, release = asyncio.Event(), asyncio.Event()
        finish = task_workspaces.finish
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await finish(*args, **kwargs)
        monkeypatch.setattr(task_workspaces, "finish", held)
        first = asyncio.create_task(_delete(project, tmp_path))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(projects.RequestError) as raised:
                await asyncio.wait_for(_delete(project, tmp_path), 1)
            assert raised.value.status == projects.HTTP_409
            assert str(raised.value) == "project 'Example' is already being deleted"
            release.set()
            assert await asyncio.wait_for(first, 5) == {"deleted": project["id"]}
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("states,has_schedule", [([], False), (["completed"], False), ([], True),
                                                 (["running", "completed"], True)])
def test_v1_and_manager_confirmations_include_only_nonzero_counts(monkeypatch, tmp_path, states, has_schedule):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        for index, state in enumerate(states):
            await _task(pool, project, f"task-{index}", state)
        if has_schedule:
            await _schedule(pool, project)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.delete", {"project_id": project["id"]}, client=True)
            assert status == 202, answer
            v1 = answer["impact"]
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("Delete", ("looper_projects", {"action": "delete", "project": "Example"}))
        assert answer["status"] == "needs_confirmation", answer
        assert chat.turns[0].blocks == ["需要你确认：\n" + v1 + "\n回复「确认」就执行；回复别的就不执行。"]
        if not states and not has_schedule:
            assert v1 == EMPTY_IMPACT
        else:
            assert "它没有任务和定时任务" not in v1
            assert ("1 个定时任务（含运行记录）" in v1) is has_schedule
            assert ("停止它正在运行的 1 个任务" in v1) is ("running" in states)
            assert (f"删除它的全部 {len(states)} 个任务（步骤工作区、任务目录、会话和记录；结果分支保留）" in v1) is bool(states)
            assert "目录里的文件不受影响。" in v1
        assert await db_projects.get_project(project["id"], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM tasks") == len(states)

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind", ["task", "schedule"])
def test_scoped_delete_keeps_another_managers_work_added_after_preflight(monkeypatch, tmp_path, kind):
    from tests.test_managers_schema_db import _manager

    async def run(pool):
        manager = await _manager(pool, "review", tools="all", task_scope="own")
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-own", manager_id=manager["id"])
        module, name = (tasks, "project_tasks") if kind == "task" else (db_schedules, "list_schedules")
        listing = getattr(module, name)
        inserted, foreign_schedule = False, None

        async def insert_after_preflight(project_id, *args, **kwargs):
            nonlocal inserted, foreign_schedule
            if not inserted:
                inserted = True
                assert project_id == project["id"]
                if kind == "task":
                    await _task(pool, project, "task-foreign", "running")
                else:
                    foreign_schedule = await _schedule(pool, project)
            return await listing(project_id, *args, **kwargs)

        monkeypatch.setattr(module, name, insert_after_preflight)
        bus = Bus()
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path, bus, manager_id=manager["id"])
        assert inserted and raised.value.status == projects.HTTP_409
        assert str(raised.value) == (
            "a task or schedule was added to project 'Example' while it was being deleted: delete it again")
        assert bus.of("task_stop") == []
        assert await tasks.get_task("task-own", pool=pool) is None
        assert await db_projects.get_project(project["id"], pool=pool)
        if kind == "task":
            assert (await tasks.get_task("task-foreign", pool=pool))["status"] == "running"
        else:
            assert await db_schedules.get_schedule(foreign_schedule, pool=pool)
            assert await pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", foreign_schedule) == 1

    run_db(run, monkeypatch, timeout=15)
