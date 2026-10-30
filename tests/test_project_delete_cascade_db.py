"""Project cascade deletion, retries, refusal ordering and confirmation on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize("mode", ["continue", "start_over"])
def test_restart_during_project_delete_row_cleanup_starts_nothing(monkeypatch, tmp_path, mode):
    from looper.agents import step_workspaces
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import TaskRouter

    async def run(pool):
        directory = tmp_path / "project"
        directory.mkdir()
        project = await db_projects.create_project(name="Example", work_dir=str(directory), pool=pool)
        await _task(pool, project, "task-stopped", "stopped", work_dir=str(directory))
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        monkeypatch.setattr(PlannerAgent, "stop", AsyncMock())
        locked, release, read = asyncio.Event(), asyncio.Event(), asyncio.Event()
        remove, get = step_workspaces.remove_task, tasks.get_task
        restart_cleanups = []

        async def held_remove(task_id, **kwargs):
            if asyncio.current_task() is deleting:
                locked.set()  # delete_task already holds SELECT FOR UPDATE here.
                await release.wait()
            else:
                restart_cleanups.append(task_id)
            return await remove(task_id, **kwargs)

        async def observed_get(task_id, **kwargs):
            row = await get(task_id, **kwargs)
            if asyncio.current_task() is restarting:
                read.set()
            return row

        monkeypatch.setattr(step_workspaces, "remove_task", held_remove)
        monkeypatch.setattr(tasks, "get_task", observed_get)
        restarting = None
        deleting = asyncio.create_task(_delete(project, tmp_path, bus, task_router=router))
        try:
            await asyncio.wait_for(locked.wait(), 5)
            restarting = asyncio.create_task(router._handle_restart(MessageEnvelope.create(
                source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
                payload={"task_id": "task-stopped", "mode": mode})))
            await asyncio.wait_for(read.wait(), 5)
            # Let a broken restart reach the locked UPDATE; release cleanup even on the failing baseline.
            await asyncio.wait({restarting}, timeout=0.1)
            release.set()
            assert await asyncio.wait_for(deleting, 5) == {"deleted": project["id"]}
            await asyncio.wait_for(restarting, 5)
            PlannerAgent.start.assert_not_awaited()
            assert restart_cleanups == []
            assert not router.has_planner("task-stopped")
            assert bus.get_queue(planner_address("task-stopped").agent_id).empty()
            assert list(directory.iterdir()) == []
            assert await get("task-stopped", pool=pool) is None
        finally:
            release.set()
            for job in (deleting, restarting):
                if job is not None:
                    job.cancel()
            await asyncio.gather(deleting, *([restarting] if restarting else []), return_exceptions=True)
            await router.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("move_at", ["snapshot", "row_lock"])
def test_project_delete_keeps_a_task_moved_after_its_snapshot(monkeypatch, tmp_path, move_at):
    async def run(pool):
        repo = _repo(tmp_path / "repo")
        project = await db_projects.create_project(name="Example", pool=pool)
        destination = await db_projects.create_project(name="Destination", pool=pool)
        await _task(pool, project, "task-moved", "stopped", work_dir=str(repo), workspace_dir=str(repo),
                    workspace_mode="worktree")
        await _task(pool, project, "task-deleted")

        async def note(*args):
            pass

        checkout = Path(await task_workspaces.prepare(await tasks.get_task("task-moved", pool=pool),
                                                       runtime_root=tmp_path, links=(), note=note))
        (checkout / "result.txt").write_text("Keep this checkout.\n")
        await plans.save("task-moved", steps=[{"id": "task-moved-s1", "index": 0, "key": "s1",
                                              "instruction": "Build it"}], pool=pool)
        original_list, original_get = tasks.project_tasks, tasks.get_task
        moved = False

        async def move():
            nonlocal moved
            await pool.execute("UPDATE tasks SET project_id = $2 WHERE id = $1", "task-moved", destination["id"])
            moved = True

        async def snapshot(*args, **kwargs):
            rows = await original_list(*args, **kwargs)
            if not moved and move_at == "snapshot":
                await move()
            return rows

        async def before_lock(task_id, **kwargs):
            if task_id == "task-moved" and kwargs.get("for_update") and not moved and move_at == "row_lock":
                await move()
            return await original_get(task_id, **kwargs)

        monkeypatch.setattr(tasks, "project_tasks", snapshot)
        monkeypatch.setattr(tasks, "get_task", before_lock)
        assert await _delete(project, tmp_path) == {"deleted": project["id"]}
        assert moved
        assert (await original_get("task-moved", pool=pool))["project_id"] == destination["id"]
        assert await pool.fetchval("SELECT count(*) FROM task_steps WHERE task_id = 'task-moved'") == 1
        assert await pool.fetchval("SELECT count(*) FROM task_worktrees WHERE task_id = 'task-moved'") == 1
        assert (checkout / "result.txt").read_text() == "Keep this checkout.\n"
        assert await original_get("task-deleted", pool=pool) is None
        assert await db_projects.get_project(project["id"], pool=pool) is None

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("attach_at", ["preflight", "final_delete"])
def test_template_attached_during_project_delete_gets_template_refusal(monkeypatch, tmp_path, attach_at):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-done")
        sid = await _schedule(pool, project)
        operation = projects.deletable_project if attach_at == "preflight" else db_projects.delete_project

        async def attach(*args, **kwargs):
            if attach_at == "preflight":
                result = await operation(*args, **kwargs)
            await pool.execute("INSERT INTO manager_task_templates (manager_id, key, name, project_id) "
                               "VALUES ('manager', 'example', 'Example template', $1)", project["id"])
            return result if attach_at == "preflight" else await operation(*args, **kwargs)

        monkeypatch.setattr(projects if attach_at == "preflight" else db_projects,
                            "deletable_project" if attach_at == "preflight" else "delete_project", attach)
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path)
        assert raised.value.status == projects.HTTP_409
        assert "used by template 'Example template'" in str(raised.value)
        assert "change the template first" in str(raised.value)
        assert await db_projects.get_project(project["id"], pool=pool) is not None
        if attach_at == "preflight":
            assert await tasks.get_task("task-done", pool=pool) is not None
            assert await db_schedules.get_schedule(sid, pool=pool) is not None

    run_db(run, monkeypatch, timeout=20)


def test_gate_refused_by_project_delete_finishes_and_can_fire_again(monkeypatch, tmp_path):
    from looper.agents import scheduler as scheduler_module
    from looper.agents.schedule_gate import GateResult

    async def run(pool):
        project = await db_projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
        sid = await _schedule(pool, project)
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(*args, **kwargs):
            entered.set()
            await release.wait()
            return GateResult(0, "Read this report", "")

        monkeypatch.setattr(scheduler_module, "run_gate", gate)
        bus = Bus()
        scheduler = scheduler_module.Scheduler(bus=bus)
        try:
            run_id = await scheduler.run_now(sid)
            await asyncio.wait_for(entered.wait(), 5)
            async with scheduler_module.quiesce_project(project["id"]):
                release.set()
                await asyncio.wait_for(scheduler.idle(), 5)
                ended = await db_schedules.get_run(run_id, pool=pool)
                assert ended["status"] == "gate_failed"
                assert ended["finished_at"] is not None
                assert "project delet" in ended["error"]
                assert (await db_schedules.get_schedule(sid, pool=pool))["consecutive_failures"] == 1
                assert bus.of("task_start") == []
            next_id = await scheduler.run_now(sid)
            await asyncio.wait_for(scheduler.idle(), 5)
            following = await db_schedules.get_run(next_id, pool=pool)
            assert following["status"] == "running"
            assert bus.of("task_start") == [following["task_id"]]
        finally:
            release.set()
            await scheduler.stop()

    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("trigger", ["tick", "manual", "queued", "gate", "retry"])
def test_project_delete_blocks_every_schedule_start_before_removing_its_first_schedule(monkeypatch, tmp_path, trigger):
    from looper.agents import scheduler as scheduler_module
    from looper.agents.schedule_gate import GateResult

    async def run(pool):
        project = await db_projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
        bus = Bus()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 5)
        scheduler = scheduler_module.Scheduler(bus=bus, clock=lambda: now)
        first = await _schedule(pool, project)
        second = (await schedules.create_schedule(project["id"], {
            "name": "Later due during deletion", "cron": "*/5 * * * *", "timezone": "UTC",
            "requirement": "Read reports", "overlap": "queue",
            "gate_command": "example" if trigger == "gate" else None,
        }))["schedule"]["schedule_id"]
        gate_started, gate_release = asyncio.Event(), asyncio.Event()

        async def gate(*args, **kwargs):
            gate_started.set()
            await gate_release.wait()
            return GateResult(0, "Read this report", "")

        monkeypatch.setattr(scheduler_module, "run_gate", gate)
        if trigger in {"queued", "gate", "retry"}:
            run_id = await scheduler.run_now(second)
            initial = await db_schedules.get_run(run_id, pool=pool)
            if trigger == "gate":
                await asyncio.wait_for(gate_started.wait(), 5)
            elif trigger == "queued":
                queued = await scheduler.run_now(second)
                assert (await db_schedules.get_run(queued, pool=pool))["status"] == "queued"
                await pool.execute("UPDATE tasks SET status = 'completed' WHERE id = $1", initial["task_id"])
            else:
                await pool.execute("UPDATE schedule_runs SET fired_at = $2 WHERE id = $1", run_id,
                                   now - timedelta(minutes=2))
        bus.sent.clear()
        if trigger == "tick":
            await pool.execute("UPDATE schedules SET enabled = true, next_fire_at = $2 WHERE id = ANY($1::text[])",
                               [first, second], now)
        # A different project's due schedule is a positive control: the tick must still fire it.
        control = await db_projects.create_project(name="Control", work_dir=str(tmp_path), pool=pool)
        control_sid = (await schedules.create_schedule(control["id"], {
            "name": "Control", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read control",
        }))["schedule"]["schedule_id"]
        await pool.execute("UPDATE schedules SET enabled = true, next_fire_at = $2 WHERE id = $1", control_sid, now)
        delete_schedule = schedules.delete_schedule
        checked = False

        async def delete_and_fire(schedule_id):
            nonlocal checked
            await delete_schedule(schedule_id)
            if schedule_id != first:
                return
            checked = True
            assert await db_schedules.get_schedule(second, pool=pool)
            if trigger == "manual":
                await scheduler.run_now(second)
            elif trigger == "gate":
                gate_release.set()
                await asyncio.wait_for(scheduler.idle(), 5)
            await scheduler.tick()
            started = [await tasks.get_task(tid, pool=pool) for tid in bus.of("task_start")]
            assert started and all(task["project_id"] == control["id"] for task in started)
            if trigger in {"tick", "manual", "gate"}:
                assert await tasks.project_tasks(project["id"], pool=pool) == []
            else:
                assert len(await tasks.project_tasks(project["id"], pool=pool)) == 1

        monkeypatch.setattr(schedules, "delete_schedule", delete_and_fire)
        try:
            assert await _delete(project, tmp_path, bus) == {"deleted": project["id"]}
            assert checked
            assert await db_projects.get_project(project["id"], pool=pool) is None
            assert await tasks.project_tasks(project["id"], pool=pool) == []
        finally:
            gate_release.set()
            await scheduler.stop()

    run_db(run, monkeypatch, timeout=25)


@pytest.mark.parametrize("stage", ["create", "publish"])
def test_project_delete_drains_inflight_firing_before_schedule_or_task_cleanup(monkeypatch, tmp_path, stage):
    from looper.agents.scheduler import Scheduler

    async def run(pool):
        project = await db_projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
        sid = (await schedules.create_schedule(project["id"], {
            "name": "In flight", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read reports",
        }))["schedule"]["schedule_id"]
        bus = Bus()
        scheduler = Scheduler(bus=bus)
        entered, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = scheduler._create_task if stage == "create" else bus.publish

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(scheduler if stage == "create" else bus,
                            "_create_task" if stage == "create" else "publish", held)
        preflight, listing = projects.deletable_project, db_schedules.list_schedules
        cleanup = []

        async def preflight_checked(*args, **kwargs):
            result = await preflight(*args, **kwargs)
            asyncio.get_running_loop().call_soon(checked.set)
            return result

        async def observe_cleanup(*args, **kwargs):
            cleanup.append(True)
            return await listing(*args, **kwargs)

        monkeypatch.setattr(projects, "deletable_project", preflight_checked)
        monkeypatch.setattr(db_schedules, "list_schedules", observe_cleanup)
        firing = asyncio.create_task(scheduler.run_now(sid))
        deleting = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            deleting = asyncio.create_task(_delete(project, tmp_path, bus))
            await asyncio.wait_for(checked.wait(), 5)
            assert not cleanup and not deleting.done()
            release.set()
            await asyncio.wait_for(firing, 5)
            assert await asyncio.wait_for(deleting, 5) == {"deleted": project["id"]}
            sent = list(bus.sent)
            await scheduler.tick()
            assert bus.sent == sent
            assert await db_projects.get_project(project["id"], pool=pool) is None
            for table in ("schedules", "schedule_runs", "tasks"):
                assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
        finally:
            release.set()
            await asyncio.gather(firing, *([deleting] if deleting else []), return_exceptions=True)
            await scheduler.stop()

    run_db(run, monkeypatch, timeout=25)


@pytest.mark.parametrize("stage", ["claim", "workspace"])
def test_project_delete_waits_for_router_start_to_finish(monkeypatch, tmp_path, stage):
    from looper.agents.scheduler import Scheduler
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import TaskRouter

    async def run(pool):
        repo = _repo(tmp_path / "repo")
        project = await db_projects.create_project(name="Example", work_dir=str(repo),
                                                   execution={"task_workspaces": "worktree"}, pool=pool)
        sid = (await schedules.create_schedule(project["id"], {
            "name": "Starting", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read reports",
        }))["schedule"]["schedule_id"]
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        scheduler = Scheduler(bus=bus)
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        monkeypatch.setattr(PlannerAgent, "stop", AsyncMock())
        entered, release, deleting_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = tasks.start_ready_task if stage == "claim" else router._prepare_workspace

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(tasks if stage == "claim" else router,
                            "start_ready_task" if stage == "claim" else "_prepare_workspace", held)
        preflight = projects.deletable_project

        async def checked(*args, **kwargs):
            result = await preflight(*args, **kwargs)
            asyncio.get_running_loop().call_soon(deleting_entered.set)
            return result

        monkeypatch.setattr(projects, "deletable_project", checked)
        cleanup_started = asyncio.Event()
        list_schedules = db_schedules.list_schedules

        async def observed_cleanup(*args, **kwargs):
            cleanup_started.set()
            return await list_schedules(*args, **kwargs)

        monkeypatch.setattr(db_schedules, "list_schedules", observed_cleanup)
        await router.start(bus)
        deleting = None
        try:
            run_id = await scheduler.run_now(sid)
            task_id = (await db_schedules.get_run(run_id, pool=pool))["task_id"]
            await asyncio.wait_for(entered.wait(), 5)
            deleting = asyncio.create_task(_delete(project, tmp_path, bus, task_router=router))
            await asyncio.wait_for(deleting_entered.wait(), 5)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(cleanup_started.wait(), 0.2)
            assert await db_schedules.get_schedule(sid, pool=pool) is not None
            assert await tasks.get_task(task_id, pool=pool) is not None
            assert not deleting.done()
            release.set()
            assert await asyncio.wait_for(deleting, 10) == {"deleted": project["id"]}
            assert await tasks.get_task(task_id, pool=pool) is None
            assert not router.has_planner(task_id)
            assert PlannerAgent.start.await_count == PlannerAgent.stop.await_count == 1
            assert git(repo, "rev-parse", "--verify", f"refs/heads/{task_workspaces.task_branch(task_id)}")
        finally:
            release.set()
            if deleting is not None:
                deleting.cancel()
                await asyncio.gather(deleting, return_exceptions=True)
            await router.stop()
            await scheduler.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("start", ["queued", "concurrent"])
def test_start_racing_task_cleanup_preserves_worktree_until_stopped(monkeypatch, tmp_path, start):
    from looper.agents import step_workspaces
    from looper.agents.scheduler import Scheduler
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import Task, TaskRouter

    async def run(pool):
        repo = _repo(tmp_path / "repo")
        project = await db_projects.create_project(name="Example", work_dir=str(repo),
                                                   execution={"task_workspaces": "worktree"}, pool=pool)
        sid = (await schedules.create_schedule(project["id"], {
            "name": "Queued start", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read reports",
        }))["schedule"]["schedule_id"]
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        scheduler = Scheduler(bus=bus)
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        stopping, stop_release = asyncio.Event(), asyncio.Event()

        async def stop():
            stopping.set()
            await stop_release.wait()

        monkeypatch.setattr(PlannerAgent, "stop", AsyncMock(side_effect=stop))
        # Leave TASK_START queued behind an earlier Router handler.
        busy, start_release = asyncio.Event(), asyncio.Event()
        original_stop = router._handle_stop

        async def busy_stop(envelope):
            if envelope.payload["task_id"] == "earlier-handler":
                busy.set()
                await start_release.wait()
            else:
                await original_stop(envelope)

        monkeypatch.setattr(router, "_handle_stop", busy_stop)
        handled = asyncio.Event()
        original_start = router._handle_start

        async def observed_start(envelope):
            await original_start(envelope)
            handled.set()

        monkeypatch.setattr(router, "_handle_start", observed_start)
        await router.start(bus)
        deleting = None
        stale_read, cleanup_release = asyncio.Event(), asyncio.Event()
        try:
            await bus.publish(MessageEnvelope.create(
                source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_STOP,
                payload={"task_id": "earlier-handler"}))
            await asyncio.wait_for(busy.wait(), 5)
            run_id = await scheduler.run_now(sid)
            task_id = (await db_schedules.get_run(run_id, pool=pool))["task_id"]
            original_get = tasks.get_task

            async def stale_get(tid, **kwargs):
                row = await original_get(tid, **kwargs)
                if tid == task_id and asyncio.current_task() is deleting and not stale_read.is_set():
                    stale_read.set()
                    await cleanup_release.wait()
                return row

            monkeypatch.setattr(tasks, "get_task", stale_get)
            remove_steps, finish = step_workspaces.remove_task, task_workspaces.finish
            cleaned = []

            async def checked_cleanup(operation, tid, **kwargs):
                # Both cleanup operations must wait for the Planner to stop, even after a stale ready read.
                assert not router.has_planner(tid)
                assert (await original_get(tid))["status"] not in tasks.ACTIVE_STATUSES
                cleaned.append(operation)
                return await operation(tid, **kwargs)

            async def checked_steps(tid, **kwargs):
                return await checked_cleanup(remove_steps, tid, **kwargs)

            async def checked_finish(tid, **kwargs):
                return await checked_cleanup(finish, tid, **kwargs)

            monkeypatch.setattr(step_workspaces, "remove_task", checked_steps)
            monkeypatch.setattr(task_workspaces, "finish", checked_finish)
            deleting = asyncio.create_task(_delete(project, tmp_path, bus, task_router=router))
            await asyncio.wait_for(stale_read.wait(), 5)
            if start == "concurrent":
                # Another start path can claim the row after the delete read its ready snapshot.
                row = await tasks.start_ready_task(task_id)
                task = Task.from_row(task_id, row)
                router._tasks[task_id] = task
                await router._start(task, MessageEnvelope.create(
                    source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_REQUEST,
                    payload={"task_id": task_id}))
            start_release.set()
            await asyncio.wait_for(handled.wait(), 5)
            if start == "queued":
                assert not router.has_planner(task_id)
                assert (await original_get(task_id))["status"] == tasks.READY
            else:
                assert router.has_planner(task_id)
                assert (await original_get(task_id))["status"] in tasks.ACTIVE_STATUSES
            cleanup_release.set()
            if start == "concurrent":
                await asyncio.wait_for(stopping.wait(), 5)
                assert cleaned == [] and not deleting.done()
                stop_release.set()
            assert await asyncio.wait_for(deleting, 5) == {"deleted": project["id"]}
            assert cleaned == [remove_steps, finish]
            assert await original_get(task_id) is None
            if start == "concurrent":
                assert git(repo, "rev-parse", "--verify", f"refs/heads/{task_workspaces.task_branch(task_id)}")
        finally:
            start_release.set()
            cleanup_release.set()
            stop_release.set()
            if deleting is not None:
                deleting.cancel()
                await asyncio.gather(deleting, return_exceptions=True)
            await router.stop()
            await scheduler.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=20)


def test_task_delete_holds_row_lock_from_first_filesystem_cleanup_until_row_removal(monkeypatch, tmp_path):
    import asyncpg
    from looper.agents import step_workspaces

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-ready", tasks.READY)
        entered, release = asyncio.Event(), asyncio.Event()
        remove = step_workspaces.remove_task

        async def held_remove(task_id, **kwargs):
            entered.set()
            await release.wait()
            return await remove(task_id, **kwargs)

        monkeypatch.setattr(step_workspaces, "remove_task", held_remove)
        deleting = asyncio.create_task(projects.delete_task("task-ready", tmp_path))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with pool.acquire() as conn:
                await conn.execute("SET lock_timeout = '200ms'")
                try:
                    with pytest.raises(asyncpg.LockNotAvailableError):
                        await tasks.start_ready_task("task-ready", pool=conn)
                finally:
                    await conn.execute("RESET lock_timeout")
            assert (await tasks.get_task("task-ready", pool=pool))["status"] == tasks.READY
            release.set()
            assert await asyncio.wait_for(deleting, 5) == {"deleted": "task-ready"}
            assert await tasks.start_ready_task("task-ready", pool=pool) is None
        finally:
            release.set()
            deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)

    run_db(run, monkeypatch, timeout=15)


def test_task_delete_reuses_row_lock_connection_with_a_single_connection_pool(monkeypatch, tmp_path):
    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-ready", tasks.READY)
        assert await asyncio.wait_for(projects.delete_task("task-ready", tmp_path), 5) == {"deleted": "task-ready"}
        assert await tasks.get_task("task-ready", pool=pool) is None

    run_db(run, monkeypatch, timeout=60, max_size=1)


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_project_delete_releases_its_schedule_barrier(monkeypatch, tmp_path, cancel):
    from looper.agents.scheduler import Scheduler

    async def run(pool):
        project = await db_projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
        sid = (await schedules.create_schedule(project["id"], {
            "name": "Retryable", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read reports",
        }))["schedule"]["schedule_id"]
        bus = Bus()
        scheduler = Scheduler(bus=bus)
        entered, release = asyncio.Event(), asyncio.Event()

        async def fail_cleanup(schedule_id):
            entered.set()
            await release.wait()
            raise projects.RequestError(projects.HTTP_409, "cleanup unavailable")

        monkeypatch.setattr(schedules, "delete_schedule", fail_cleanup)
        deleting = asyncio.create_task(_delete(project, tmp_path, bus))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await scheduler.run_now(sid) is None
            assert bus.of("task_start") == []
            if cancel:
                deleting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await deleting
            else:
                release.set()
                with pytest.raises(projects.RequestError, match="cleanup unavailable"):
                    await deleting
            run_id = await scheduler.run_now(sid)
            run = await db_schedules.get_run(run_id, pool=pool)
            assert bus.of("task_start") == [run["task_id"]]
        finally:
            deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)
            await scheduler.stop()

    run_db(run, monkeypatch, timeout=20)


def test_start_retry_with_a_snapshot_from_before_project_deletion_starts_nothing(monkeypatch, tmp_path):
    from looper.agents.scheduler import Scheduler

    async def run(pool):
        project = await db_projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
        sid = (await schedules.create_schedule(project["id"], {
            "name": "Recovery", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Read reports",
        }))["schedule"]["schedule_id"]
        bus = Bus()
        scheduler = Scheduler(bus=bus)
        try:
            await scheduler.run_now(sid)
            (snapshot,) = await db_schedules.running_runs(pool=pool)
            assert bus.of("task_start") == [snapshot["task_id"]]
            bus.sent.clear()
            await _delete(project, tmp_path, bus)
            await scheduler._settle(snapshot, snapshot["fired_at"] + timedelta(minutes=2))
            assert bus.of("task_start") == []
            assert await tasks.get_task(snapshot["task_id"], pool=pool) is None
        finally:
            await scheduler.stop()

    run_db(run, monkeypatch, timeout=20)


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
        await _task(pool, project, "task-done")
        await _schedule(pool, project)
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        planner = PlannerAgent(task_id="task-running", runtime_root=tmp_path)
        session = _Session(hold=False)
        session.started = True
        planner._claude = session
        stopping = asyncio.Event()
        original_stop = planner.stop
        async def slow_stop():
            stopping.set()
            await asyncio.sleep(2)
            await original_stop()
        monkeypatch.setattr(planner, "stop", slow_stop)
        seen = []
        def observe_stop(envelope):
            seen.append(envelope)
        bus.subscribe_topic(MessageType.TASK_STOP.value, observe_stop)
        delete_task = tasks.delete_task
        deleted = []

        async def checked_delete(task_id, **kwargs):
            assert not router.has_planner("task-running")
            assert not session.started
            deleted.append(task_id)
            return await delete_task(task_id, **kwargs)

        monkeypatch.setattr(tasks, "delete_task", checked_delete)
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
            await asyncio.sleep(1.1)  # The deletion has polled again while the Planner still exits.
            assert not deleting.done()
            assert (await tasks.get_task("task-running", pool=pool))["status"] == (
                "stopped" if end_kind == "stop" else end_kind)
            assert router.has_planner("task-running")
            assert deleted == []
            assert await tasks.get_task("task-done", pool=pool) is not None
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
            assert [e.payload["task_id"] for e in seen] == (["task-running"] if end_kind == "stop" else [])
            assert session.started  # Cleanup is still held; the row must survive until it ends.
            assert await asyncio.wait_for(deleting, 5) == {"deleted": project["id"]}
            assert set(deleted) == {"task-running", "task-done"}
            assert "task-running" not in router._tasks and not router.has_planner("task-running")
            assert not planner._running and not session.started
            assert await tasks.get_task("task-running", pool=pool) is None
        finally:
            if deleting is not None:
                if not deleting.done():
                    deleting.cancel()
                await asyncio.gather(deleting, return_exceptions=True)
            await router.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=20)


def test_user_stop_is_not_resumed_after_crash_during_planner_stop(monkeypatch, tmp_path):
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import Task, TaskRouter

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-running", "running")
        row = await tasks.get_task("task-running", pool=pool)
        await plans.save("task-running", plan={"plan_id": "plan-running", "status": "executing"}, pool=pool)
        assert await plans.resumable_task_ids(tasks.ACTIVE_STATUSES, pool=pool) == ["task-running"]
        bus, router = MessageBus(runtime_root=tmp_path), TaskRouter(runtime_root=tmp_path)
        planner = PlannerAgent(task_id="task-running", runtime_root=tmp_path)
        stopping, release = asyncio.Event(), asyncio.Event()

        async def held_stop():
            stopping.set()
            await release.wait()

        monkeypatch.setattr(planner, "stop", held_stop)
        await router.start(bus)
        router._tasks["task-running"] = Task.from_row("task-running", row)
        router._planners["task-running"] = planner
        try:
            await bus.publish(MessageEnvelope.create(
                source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_STOP,
                payload={"task_id": "task-running"}))
            await asyncio.wait_for(stopping.wait(), 5)
            router._loop_task.cancel()  # The process exits before the Planner's stop can finish.
            await asyncio.gather(router._loop_task, return_exceptions=True)
            saved = await tasks.get_task("task-running", pool=pool)
            assert saved["status"] == "stopped"
            assert "The task was stopped before it finished." in saved["result_summary"]
            # This is the query serve uses on restart; the live plan must not revive a user's stop.
            assert await plans.resumable_task_ids(tasks.ACTIVE_STATUSES, pool=pool) == []
        finally:
            release.set()
            await router.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=15)


@pytest.mark.parametrize("planner_pending", [False, True])
def test_stop_timeout_removes_schedules_but_keeps_all_tasks_and_retry_finishes(monkeypatch, tmp_path, planner_pending):
    monkeypatch.setattr(projects, "PROJECT_DELETE_STOP_WAIT_SECONDS", 1, raising=False)

    async def run(pool):
        project = await db_projects.create_project(name="Example", pool=pool)
        await _task(pool, project, "task-running", "stopped" if planner_pending else "running")
        await _task(pool, project, "task-stopping", "stopping")
        await _task(pool, project, "task-done")
        await _schedule(pool, project)
        bus = Bus()  # A fake runtime that receives the stop and does not stop.
        planners = {"task-running"} if planner_pending else set()
        router = SimpleNamespace(has_planner=planners.__contains__, forget_task=planners.discard)
        with pytest.raises(projects.RequestError) as raised:
            await _delete(project, tmp_path, bus, task_router=router)
        assert raised.value.status == projects.HTTP_409
        message = str(raised.value)
        assert "2 task(s) of project 'Example' are still stopping" in message
        assert "task-running" in message and "task-stopping" in message
        assert message.endswith("delete the project again to finish")
        assert bus.of("task_stop") == ([] if planner_pending else ["task-running"])
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
        assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 3
        assert await db_projects.get_project(project["id"], pool=pool)
        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE status = ANY($1::text[])", list(tasks.ACTIVE_STATUSES))
        planners.clear()
        assert await _delete(project, tmp_path, bus, task_router=router) == {"deleted": project["id"]}
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
