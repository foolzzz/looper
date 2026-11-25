"""Direct-mode completion on PostgreSQL: status, `/done`, and a worktree finish."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import task_conversations_settings as settings
from looper.agents import task_workspaces as tw
from looper.agents.broadcast import Broadcaster
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.projects import HTTP_404, HTTP_409, RequestError
from looper.agents.tiers.planner import PlannerAgent
from looper.agents.tiers.task import RestartRefusal, TaskRouter
from looper.db import activity as db_activity
from looper.db import plans, projects, tasks
from looper.db import step_workspaces as db_step_workspaces
from looper.db.tasks import (
    ACTIVE_STATUSES,
    STATUS_GROUPS,
    InteractionMode,
    TaskGroup,
    interaction_mode,
)
from tests.db_support import run_db, v1_api

_DIRECT = "Completed by the operator in direct mode"
_DIRECT_GROUPS = {
    TaskGroup.DRAFT, TaskGroup.READY, TaskGroup.ATTENTION, TaskGroup.COMPLETED, TaskGroup.CANCELLED,
}
_ORCHESTRATED_GROUPS = {TaskGroup.NEEDS_YOU, TaskGroup.IN_PROGRESS}


def git(cwd: Path | str, *args: str) -> str:
    """A git command of the user's (no hooks, no signing)."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout.rstrip()


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "_enabled", False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "_enabled", True)


async def _project(pool, work_dir: Path):
    return await projects.create_project(name="Direct project", work_dir=str(work_dir), pool=pool)


async def _task(pool, project_id: str, task_id: str, status: str, work_dir: Path, *,
                workspace_mode: str | None = None) -> None:
    await tasks.create_task(
        task_id=task_id, requirement="Do the work", project_id=project_id, status=status,
        work_dir=str(work_dir), workspace_dir=str(work_dir), workspace_mode=workspace_mode, pool=pool)


async def _note(kind: str, text: str) -> None:
    return None


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "README").write_text("base\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


@pytest.mark.parametrize("status", list(STATUS_GROUPS))
def test_interaction_mode_by_status(status: str) -> None:
    group = STATUS_GROUPS[status]
    assert group in _DIRECT_GROUPS | _ORCHESTRATED_GROUPS
    assert set(TaskGroup) == _DIRECT_GROUPS | _ORCHESTRATED_GROUPS | {TaskGroup.OTHER}
    expected = InteractionMode.DIRECT if group in _DIRECT_GROUPS else InteractionMode.ORCHESTRATED
    assert interaction_mode(status) == expected
    if status == "draft":
        assert interaction_mode("unlisted") == InteractionMode.DIRECT
        assert interaction_mode(None) == InteractionMode.DIRECT


def test_done_from_each_direct_status(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        owner = await _project(pool, tmp_path)
        ended: list[tuple[str, str]] = []

        async def on_end(task_id: str, status: str) -> None:
            ended.append((task_id, status))

        class Bus:
            def __init__(self) -> None:
                self.messages: list = []

            async def publish(self, envelope) -> None:
                self.messages.append(envelope)

        bus = Bus()
        broadcaster = Broadcaster()
        router = TaskRouter(runtime_root=tmp_path, broadcaster=broadcaster, on_task_end=on_end)
        router._bus = bus
        with pytest.raises(RequestError) as caught:
            await router.complete_direct("missing-direct-task")
        assert caught.value.status == HTTP_404

        for status in ("draft", "init", "failed", "stopped", "cancelled"):
            task_id = f"direct-{status}"
            await _task(pool, owner["id"], task_id, status, tmp_path)
            if status == "failed":
                await pool.execute(
                    "UPDATE tasks SET result_summary=$2, error=$3 WHERE id=$1",
                    task_id, "kept-summary", "kept-error")
            queue = broadcaster.subscribe(task_id)
            done = await router.complete_direct(task_id)
            assert done.status == "completed" and done.changed is True
            assert done.warning is None and done.branch_line is None
            row = await tasks.get_task(task_id)
            assert row["status"] == "completed" and row["completed_at"] is not None
            if status == "failed":
                assert row["result_summary"] == "kept-summary" and row["error"] == "kept-error"
            activity = await db_activity.list_activity(task_id)
            assert [(item["worker"], item["kind"], item["summary"]) for item in activity] == [
                ("operator", "phase", _DIRECT)]
            assert queue.get_nowait() == {"kind": "phase", "summary": _DIRECT, "worker": "operator"}
            assert queue.empty()
        assert ended == []
        assert [message.type for message in bus.messages] == []
        assert MessageType.GOAL_STATUS not in [message.type for message in bus.messages]

    run_db(check, monkeypatch)


def test_done_refused_while_orchestrated(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch)

    async def check(pool) -> None:
        owner = await _project(pool, tmp_path)
        async with v1_api(tmp_path, pool) as call:
            for status in ACTIVE_STATUSES:
                task_id = f"orch-{status}"
                await _task(pool, owner["id"], task_id, status, tmp_path)
                before = dict(await pool.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id))
                code, body = await call("tasks.done", {"task_id": task_id})
                assert code == 409, body
                assert body["error"]["code"] == "conflict"
                assert body["error"]["message"] == (
                    f"the background is working on task {task_id}: stop it or wait for it to end first")
                after = dict(await pool.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id))
                assert after == before
                assert await pool.fetchval("SELECT count(*) FROM activity WHERE task_id=$1", task_id) == 0

    run_db(check, monkeypatch)


def test_done_idempotent(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch)

    async def check(pool) -> None:
        owner = await _project(pool, tmp_path)
        await _task(pool, owner["id"], "twice", "init", tmp_path)
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.done", {"task_id": "twice"})
            assert code == 200, body
            assert body["result"] == {
                "task_id": "twice", "status": "completed", "changed": True, "warning": None}
            code, body = await call("tasks.done", {"task_id": "twice"})
            assert code == 200, body
            assert body["result"]["changed"] is False
            assert body["result"]["status"] == "completed"
            assert body["result"]["warning"] is None
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE task_id='twice'") == 1
        assert (await tasks.get_task("twice"))["status"] == "completed"

    run_db(check, monkeypatch)


def test_done_finishes_worktree(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch)

    async def finish(pool, task_id: str, repo: Path, runtime: Path) -> dict:
        owner = await projects.create_project(name=task_id, work_dir=str(repo), pool=pool)
        await _task(pool, owner["id"], task_id, "init", repo, workspace_mode="worktree")
        task = await tasks.get_task(task_id)
        await tw.prepare(task, runtime_root=runtime, links=(), note=_note)
        row = await pool.fetchrow("SELECT * FROM task_worktrees WHERE task_id=$1", task_id)
        work = Path(row["path"])
        (work / "left.txt").write_text("left\n")
        return {"work": work, "branch": tw.task_branch(task_id)}

    async def check(pool) -> None:
        runtime = tmp_path / "runtime"
        ok_repo = _repo(tmp_path / "ok")
        made = await finish(pool, "wt-ok", ok_repo, runtime)
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.done", {"task_id": "wt-ok"})
            assert code == 200, body
            assert body["result"]["changed"] is True and body["result"]["warning"] is None
            assert body["result"]["status"] == "completed"
            assert not made["work"].exists()
            assert git(ok_repo, "show", f"{made['branch']}:left.txt") == "left"
            assert "looper: commit what task" in git(ok_repo, "log", "-1", "--format=%s", made["branch"])
            stored = await pool.fetchrow("SELECT state FROM task_worktrees WHERE task_id='wt-ok'")
            assert stored["state"] == "removed"
            activity = await db_activity.list_activity("wt-ok")
            assert [(item["worker"], item["kind"]) for item in activity] == [
                ("planner", "phase"), ("operator", "phase")]
            assert activity[-1]["summary"] == _DIRECT

            lock_repo = _repo(tmp_path / "lock")
            locked = await finish(pool, "wt-lock", lock_repo, runtime)
            lock = Path(git(locked["work"], "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))
            lock.write_text("")
            code, body = await call("tasks.done", {"task_id": "wt-lock"})
            assert code == 200, body
            assert body["result"]["status"] == "completed" and body["result"]["changed"] is True
            assert body["result"]["warning"]
            assert locked["work"].is_dir()
            assert "left.txt" in git(locked["work"], "status", "--porcelain", "--untracked-files=all")
            show = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "show", f"{locked['branch']}:left.txt"],
                cwd=lock_repo, capture_output=True, text=True)
            assert show.returncode != 0
            stored = await pool.fetchrow("SELECT state FROM task_worktrees WHERE task_id='wt-lock'")
            assert stored["state"] == "active"
            assert (await tasks.get_task("wt-lock"))["status"] == "completed"
            activity = await db_activity.list_activity("wt-lock")
            assert [(item["worker"], item["kind"]) for item in activity] == [
                ("planner", "error"), ("operator", "phase")]
            assert activity[0]["summary"].startswith("Warning:")
            assert activity[1]["summary"] == _DIRECT

    run_db(check, monkeypatch)


class _Bus:
    async def drain_queue(self, agent_id: str) -> list:
        return []

    async def publish(self, envelope) -> None:
        return None


async def _lock_waiting(pool) -> bool:
    """Whether another session waits on this schema's task row (a start's UPDATE, or a second /done).

    A row-lock waiter shows up as an ungranted transactionid lock, not an ungranted relation lock. The holder's
    granted lock on ``tasks`` keeps the check on this schema.
    """
    return bool(await pool.fetchval(
        """
        SELECT EXISTS (
          SELECT 1
          FROM pg_locks waiter
          JOIN pg_locks holder
            ON holder.locktype = 'transactionid'
           AND holder.transactionid = waiter.transactionid
           AND holder.granted AND holder.mode = 'ExclusiveLock'
          JOIN pg_locks rel
            ON rel.pid = holder.pid AND rel.granted
           AND rel.relation = to_regclass('tasks')
          WHERE NOT waiter.granted AND waiter.locktype = 'transactionid'
        )
        """
    ))


async def _until(ready, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await ready():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the race barrier")
        await asyncio.sleep(0.02)


async def _worktree(pool, tmp_path: Path, task_id: str) -> dict:
    repo = _repo(tmp_path / task_id / "repo")
    runtime = tmp_path / task_id / "runtime"
    owner = await _project(pool, repo)
    await _task(pool, owner["id"], task_id, "init", repo, workspace_mode="worktree")
    task = await tasks.get_task(task_id)
    await tw.prepare(task, runtime_root=runtime, links=(), note=_note)
    row = await pool.fetchrow("SELECT path FROM task_worktrees WHERE task_id=$1", task_id)
    work = Path(row["path"])
    (work / "left.txt").write_text("left\n")
    return {"repo": repo, "runtime": runtime, "work": work}


def test_done_and_start_one_wins(monkeypatch, tmp_path) -> None:
    """A start that arrives during /done's worktree finish cannot keep the task and lose the worktree."""

    async def check(pool) -> None:
        made = await _worktree(pool, tmp_path, "race-start")
        task_id = "race-start"
        claim = {"done": False, "row": None}
        entered, release = asyncio.Event(), asyncio.Event()
        original_finish = tw.finish
        original_start = tasks.start_ready_task

        async def paused_finish(task_id_: str, *, completed: bool = False, deleted: bool = False, pool=None):
            if task_id_ == task_id:
                entered.set()
                await release.wait()
            return await original_finish(task_id_, completed=completed, deleted=deleted, pool=pool)

        async def watched_start(task_id_: str, *, pool=None):
            row = await original_start(task_id_, pool=pool)
            if task_id_ == task_id:
                claim["row"] = row
                claim["done"] = True
            return row

        monkeypatch.setattr(tw, "finish", paused_finish)
        monkeypatch.setattr(tasks, "start_ready_task", watched_start)
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        router = TaskRouter(runtime_root=made["runtime"])
        router._bus = _Bus()
        done_task = asyncio.create_task(router.complete_direct(task_id))
        await asyncio.wait_for(entered.wait(), 5)
        start_task = asyncio.create_task(router._handle_start(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_START, payload={"task_id": task_id})))

        async def start_settled() -> bool:
            return claim["done"] or await _lock_waiting(pool)

        try:
            await _until(start_settled)
            claimed_while_finishing = claim["done"] and claim["row"] is not None
        finally:
            release.set()
        done = await done_task
        await start_task
        status = (await tasks.get_task(task_id))["status"]
        started = status in ACTIVE_STATUSES or router.has_planner(task_id)
        if started:
            assert made["work"].exists(), "a started task's workspace was deleted"
        done_won = bool(done.changed and status == "completed" and not router.has_planner(task_id))
        start_won = bool(router.has_planner(task_id) and status in ACTIVE_STATUSES and made["work"].exists())
        assert not claimed_while_finishing
        assert int(done_won) + int(start_won) == 1
        if done.changed:
            phases = await pool.fetchval(
                "SELECT count(*) FROM activity WHERE task_id=$1 AND worker='operator' AND kind='phase'", task_id)
            assert phases == 1

    run_db(check, monkeypatch, timeout=30)


def test_concurrent_done_writes_one_phase(monkeypatch, tmp_path) -> None:
    """Two /done calls share the task row: one changed=True and one operator phase."""

    async def check(pool) -> None:
        await _worktree(pool, tmp_path, "race-done")
        task_id = "race-done"
        entered = {"n": 0}
        release = asyncio.Event()
        original_finish = tw.finish

        async def paused_finish(task_id_: str, *, completed: bool = False, deleted: bool = False, pool=None):
            if task_id_ == task_id:
                entered["n"] += 1
                await release.wait()
            return await original_finish(task_id_, completed=completed, deleted=deleted, pool=pool)

        monkeypatch.setattr(tw, "finish", paused_finish)
        router = TaskRouter(runtime_root=tmp_path / task_id / "runtime")
        first = asyncio.create_task(router.complete_direct(task_id))
        second = asyncio.create_task(router.complete_direct(task_id))

        async def both_or_blocked() -> bool:
            return entered["n"] >= 2 or (entered["n"] >= 1 and await _lock_waiting(pool))

        try:
            await _until(both_or_blocked)
            both_entered = entered["n"] >= 2
        finally:
            release.set()
        results = await asyncio.gather(first, second)
        phases = await pool.fetchval(
            "SELECT count(*) FROM activity WHERE task_id=$1 AND worker='operator' AND kind='phase'", task_id)
        assert not both_entered
        assert sum(result.changed for result in results) == 1
        assert phases == 1
        assert (await tasks.get_task(task_id))["status"] == "completed"

    run_db(check, monkeypatch, timeout=30)


async def _plan_shape(pool, task_id: str) -> tuple:
    plan = await pool.fetchrow("SELECT plan_id, status FROM task_plans WHERE task_id=$1", task_id)
    steps = await pool.fetch(
        "SELECT id, status, instruction FROM task_steps WHERE task_id=$1 ORDER BY step_index", task_id)
    run_no = await pool.fetchval("SELECT run_no FROM tasks WHERE id=$1", task_id)
    return (
        None if plan is None else (plan["plan_id"], plan["status"]),
        [(row["id"], row["status"], row["instruction"]) for row in steps],
        run_no,
    )


async def _stopped_restart_task(pool, tmp_path: Path, task_id: str) -> dict:
    """A stopped worktree task with a plan, a step worktree and a kept run session."""
    made = await _worktree(pool, tmp_path, task_id)
    await pool.execute("UPDATE tasks SET status='stopped', run_no=1 WHERE id=$1", task_id)
    step_id = f"{task_id}-step"
    await plans.save(
        task_id, plan={"plan_id": f"plan-{task_id}", "status": "failed"},
        steps=[{"id": step_id, "index": 0, "instruction": "keep this step", "status": "completed"}],
        pool=pool)
    step_dir = tmp_path / task_id / "step-work"
    step_dir.mkdir(parents=True)
    (step_dir / "marker.txt").write_text("step\n")
    base = git(made["repo"], "rev-parse", "HEAD")
    workspace_id = await db_step_workspaces.next_id(pool=pool)
    await db_step_workspaces.save({
        "id": workspace_id, "task_id": task_id, "step_id": step_id, "repo_dir": str(made["repo"]),
        "subdir": "", "target_branch": "main", "path": str(step_dir),
        "branch": f"looper/{task_id}/keep-{workspace_id}", "base_commit": base, "checkpoint": False,
        "result_commit": None, "merge_commit": None, "state": "active", "merge_error": None,
        "detail": "", "removed_at": None, "links": [], "merge_files": [],
    }, pool=pool)
    session = made["runtime"] / ".local" / "codex" / "run-sessions" / task_id / "run1"
    session.mkdir(parents=True)
    (session / "note.txt").write_text("session\n")
    made.update(step_dir=step_dir, session=session, plan=await _plan_shape(pool, task_id))
    return made


def _markers(made: dict) -> tuple[bool, bool, bool]:
    work = made["work"]
    step = made["step_dir"] / "marker.txt"
    session = made["session"] / "note.txt"
    return (
        work.is_dir() and (work / "left.txt").is_file() and (work / "left.txt").read_text() == "left\n",
        step.is_file() and step.read_text() == "step\n",
        session.is_file() and session.read_text() == "session\n",
    )


def _restart_envelope(task_id: str, mode: str):
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS,
        message_type=MessageType.TASK_RESTART, payload={"task_id": task_id, "mode": mode})


def _one_winner(done, done_error, restart_result, *, status: str, router: TaskRouter, task_id: str,
                plan_now, made: dict, work_kept: bool, step_kept: bool, session_kept: bool, mode: str) -> None:
    """Exactly one of /done and restart took effect, and the loser left the other's state alone."""
    done_won = bool(
        done is not None and done.changed and done_error is None and status == "completed"
        and not router.has_planner(task_id) and not work_kept
        and plan_now == made["plan"] and step_kept and session_kept)
    continue_kept = plan_now[:2] == made["plan"][:2] and step_kept and session_kept and plan_now[2] == made["plan"][2] + 1
    started_over = plan_now[:2] == (None, []) and not step_kept and not session_kept and plan_now[2] == made["plan"][2] + 1
    restart_won = bool(
        restart_result is None and router.has_planner(task_id) and status in ACTIVE_STATUSES and work_kept
        and done_error is not None and done_error.status == HTTP_409
        and (continue_kept if mode == "continue" else started_over))
    assert int(done_won) + int(restart_won) == 1
    if done_won:
        assert restart_result is RestartRefusal.COMPLETED


@pytest.mark.parametrize("mode", ["continue", "start_over"])
def test_done_and_restart_one_wins(monkeypatch, tmp_path, mode: str) -> None:
    """A restart that arrives during /done's worktree finish waits for that row.

    It does not read the pre-commit status, clean the plan or step workspace, or
    turn a completed task back into pending. Exactly one side takes effect.
    """
    from looper.agents import step_workspaces
    from looper.agents.tiers import task as task_tier

    async def check(pool) -> None:
        task_id = f"race-done-{mode}"
        made = await _stopped_restart_task(pool, tmp_path, task_id)
        entered, release = asyncio.Event(), asyncio.Event()
        original_finish = tw.finish
        original_remove = step_workspaces.remove_task
        original_sessions = task_tier.remove_run_sessions
        cleaned = {"step": False, "session": False}

        async def paused_finish(task_id_: str, *, completed: bool = False, deleted: bool = False, pool=None):
            if task_id_ == task_id:
                entered.set()
                await release.wait()
            return await original_finish(task_id_, completed=completed, deleted=deleted, pool=pool)

        async def watched_remove(task_id_: str, *, pool=None):
            if task_id_ == task_id and not release.is_set():
                cleaned["step"] = True
            return await original_remove(task_id_, pool=pool)

        def watched_sessions(runtime_root: Path, task_id_: str) -> None:
            if task_id_ == task_id and not release.is_set():
                cleaned["session"] = True
            original_sessions(runtime_root, task_id_)

        monkeypatch.setattr(tw, "finish", paused_finish)
        monkeypatch.setattr(step_workspaces, "remove_task", watched_remove)
        monkeypatch.setattr(task_tier, "remove_run_sessions", watched_sessions)
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        router = TaskRouter(runtime_root=made["runtime"])
        router._bus = _Bus()
        done_task = asyncio.create_task(router.complete_direct(task_id))
        await asyncio.wait_for(entered.wait(), 5)
        restart_task = asyncio.create_task(router._handle_restart(_restart_envelope(task_id, mode)))

        try:
            await _until(lambda: _lock_waiting(pool))
            held_plan = await _plan_shape(pool, task_id)
            held_markers = _markers(made)
            held_cleaned = dict(cleaned)
        finally:
            release.set()
        done_error = None
        try:
            done = await done_task
        except RequestError as exc:
            done, done_error = None, exc
        restart_result = await restart_task
        status = (await tasks.get_task(task_id))["status"]
        plan_now = await _plan_shape(pool, task_id)
        work_kept, step_kept, session_kept = _markers(made)
        assert held_plan == made["plan"]
        assert held_markers == (True, True, True)
        assert held_cleaned == {"step": False, "session": False}
        _one_winner(done, done_error, restart_result, status=status, router=router, task_id=task_id,
                    plan_now=plan_now, made=made, work_kept=work_kept, step_kept=step_kept,
                    session_kept=session_kept, mode=mode)

    run_db(check, monkeypatch, timeout=30, max_size=4)


@pytest.mark.parametrize("mode", ["continue", "start_over"])
def test_restart_and_done_one_wins(monkeypatch, tmp_path, mode: str) -> None:
    """A /done that arrives during restart's claim waits for that row.

    It does not finish the worktree or change the plan while restart holds the
    task. Exactly one side takes effect.
    """

    async def check(pool) -> None:
        task_id = f"race-restart-{mode}"
        made = await _stopped_restart_task(pool, tmp_path, task_id)
        entered, release = asyncio.Event(), asyncio.Event()
        finish_entered, finish_release = asyncio.Event(), asyncio.Event()
        original_finish = tw.finish
        original_reset = tasks.reset_task_for_restart

        async def paused_reset(task_id_: str, **kwargs):
            if task_id_ == task_id:
                entered.set()
                await release.wait()
            return await original_reset(task_id_, **kwargs)

        async def watched_finish(task_id_: str, *, completed: bool = False, deleted: bool = False, pool=None):
            if task_id_ == task_id:
                finish_entered.set()
                await finish_release.wait()
            return await original_finish(task_id_, completed=completed, deleted=deleted, pool=pool)

        monkeypatch.setattr(tasks, "reset_task_for_restart", paused_reset)
        monkeypatch.setattr(tw, "finish", watched_finish)
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        router = TaskRouter(runtime_root=made["runtime"])
        router._bus = _Bus()
        restart_task = asyncio.create_task(router._handle_restart(_restart_envelope(task_id, mode)))
        await asyncio.wait_for(entered.wait(), 5)
        done_task = asyncio.create_task(router.complete_direct(task_id))

        async def done_settled() -> bool:
            return finish_entered.is_set() or await _lock_waiting(pool)

        try:
            await _until(done_settled)
            blocked = await _lock_waiting(pool) and not finish_entered.is_set()
            held_work = _markers(made)[0]
        finally:
            release.set()
            finish_release.set()
        done_error = None
        try:
            done = await done_task
        except RequestError as exc:
            done, done_error = None, exc
        restart_result = await restart_task
        status = (await tasks.get_task(task_id))["status"]
        plan_now = await _plan_shape(pool, task_id)
        work_kept, step_kept, session_kept = _markers(made)
        assert blocked and held_work
        _one_winner(done, done_error, restart_result, status=status, router=router, task_id=task_id,
                    plan_now=plan_now, made=made, work_kept=work_kept, step_kept=step_kept,
                    session_kept=session_kept, mode=mode)

    run_db(check, monkeypatch, timeout=30, max_size=4)


def test_done_switch_off(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    async def complete_direct(self, task_id: str):
        calls.append(task_id)
        raise AssertionError("complete_direct ran while work mode is off")

    monkeypatch.setattr(TaskRouter, "complete_direct", complete_direct, raising=False)

    async def check(pool) -> None:
        owner = await _project(pool, tmp_path)
        await _task(pool, owner["id"], "off", "draft", tmp_path)
        before = dict(await pool.fetchrow("SELECT * FROM tasks WHERE id='off'"))
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.done", {"task_id": "off"})
        assert code == 409, body
        assert body["error"]["code"] == "conflict"
        assert body["error"]["message"] == "work mode is off (tasks.conversations.enabled)"
        assert calls == []
        assert dict(await pool.fetchrow("SELECT * FROM tasks WHERE id='off'")) == before
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE task_id='off'") == 0

    run_db(check, monkeypatch)
