"""Direct-mode completion on PostgreSQL: status, `/done`, and a worktree finish."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from looper.agents import task_conversations_settings as settings
from looper.agents import task_workspaces as tw
from looper.agents.broadcast import Broadcaster
from looper.agents.message import MessageType
from looper.agents.projects import HTTP_404, RequestError
from looper.agents.tiers.task import TaskRouter
from looper.db import activity as db_activity
from looper.db import projects, tasks
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
