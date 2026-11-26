"""Reopening a task workspace on PostgreSQL, in a real git repository."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from looper.agents import step_workspaces as ws
from looper.agents import task_conversations_settings as settings
from looper.agents import task_workspaces as tw
from looper.agents.tiers.task import TaskRouter
from looper.db import projects, tasks
from looper.db import task_worktrees as db_worktrees
from tests.db_support import run_db


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


async def _removed_worktree(pool, tmp_path: Path, task_id: str, *, filename: str = "left.txt",
                            text: str = "left\n"):
    """A completed worktree task: ``filename`` is committed and the worktree row is removed."""
    repo = _repo(tmp_path / task_id / "repo")
    runtime = tmp_path / task_id / "runtime"
    owner = await projects.create_project(name=task_id, work_dir=str(repo), pool=pool)
    await tasks.create_task(
        task_id=task_id, requirement="Ship it", project_id=owner["id"], status="init",
        work_dir=str(repo), workspace_dir=str(repo), workspace_mode="worktree", pool=pool)
    task = await tasks.get_task(task_id)
    await tw.prepare(task, runtime_root=runtime, links=(), note=_note)
    row = await db_worktrees.get(task_id)
    (Path(row["path"]) / filename).write_text(text)
    done = await TaskRouter(runtime_root=runtime).complete_direct(task_id)
    assert done.changed is True and done.warning is None
    return repo, runtime


async def _columns(pool, task_id: str):
    task = dict(await pool.fetchrow(
        "SELECT status, work_dir, updated_at, completed_at FROM tasks WHERE id=$1", task_id))
    tree = dict(await pool.fetchrow("SELECT * FROM task_worktrees WHERE task_id=$1", task_id))
    return task, tree


def test_reopen_rebuilds_from_branch(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        repo, runtime = await _removed_worktree(pool, tmp_path, "rebuild")
        before = await db_worktrees.get("rebuild")
        assert before["state"] == "removed"
        assert not Path(before["path"]).exists()
        task = await tasks.get_task("rebuild")
        opened = await tw.reopen(task, runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.WRITE and opened.reason is None
        assert opened.work_dir == before["path"]
        assert (Path(opened.work_dir) / "left.txt").read_text() == "left\n"
        after = await db_worktrees.get("rebuild")
        assert after["state"] == "active"
        assert (await tasks.get_task("rebuild"))["status"] == "completed"
        assert git(repo, "branch", "--list", tw.task_branch("rebuild"))

    run_db(check, monkeypatch)


def test_reopen_branch_gone(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        repo, runtime = await _removed_worktree(pool, tmp_path, "gone")
        branch = tw.task_branch("gone")
        git(repo, "branch", "-D", branch)
        before_task, before_tree = await _columns(pool, "gone")
        opened = await tw.reopen(await tasks.get_task("gone"), runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.READ_ONLY
        assert opened.reason == "branch_gone"
        assert opened.work_dir == str(repo)
        assert git(repo, "branch", "--list", branch) == ""
        assert not (runtime / "task-worktrees" / "gone").exists()
        assert await _columns(pool, "gone") == (before_task, before_tree)

    run_db(check, monkeypatch)


def test_reopen_branch_checked_out(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        repo, runtime = await _removed_worktree(pool, tmp_path, "held")
        branch = tw.task_branch("held")
        other = tmp_path / "held-elsewhere"
        git(repo, "worktree", "add", "-q", str(other), branch)
        before_task, before_tree = await _columns(pool, "held")
        opened = await tw.reopen(await tasks.get_task("held"), runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.READ_ONLY
        assert opened.reason == "branch_checked_out"
        assert opened.work_dir == str(repo)
        assert branch in git(repo, "branch", "--list", branch)
        assert git(other, "symbolic-ref", "--short", "HEAD") == branch
        assert not (runtime / "task-worktrees" / "held").exists()
        assert await _columns(pool, "held") == (before_task, before_tree)

    run_db(check, monkeypatch)


def test_reopen_branch_deleted_after_precheck(monkeypatch, tmp_path) -> None:
    """The branch disappearing after the pre-check is read-only, and no replacement branch is created."""

    async def check(pool) -> None:
        task_id = "deleted-late"
        repo, runtime = await _removed_worktree(pool, tmp_path, task_id)
        branch = tw.task_branch(task_id)
        before_task, before_tree = await _columns(pool, task_id)
        original = ws.branch_exists
        armed = {"yes": True}

        async def exists(repo_dir: str, name: str) -> bool:
            found = await original(repo_dir, name)
            if armed["yes"] and name == branch and found:
                armed["yes"] = False
                git(repo, "branch", "-D", branch)
                return True
            return found

        monkeypatch.setattr(ws, "branch_exists", exists)
        opened = await tw.reopen(await tasks.get_task(task_id), runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.READ_ONLY
        assert opened.reason == "branch_gone"
        assert opened.work_dir == str(repo)
        assert git(repo, "branch", "--list", branch) == ""
        assert not (runtime / "task-worktrees" / task_id).exists()
        assert await _columns(pool, task_id) == (before_task, before_tree)

    run_db(check, monkeypatch)


def test_reopen_branch_checked_out_after_precheck(monkeypatch, tmp_path) -> None:
    """A branch checked out elsewhere after the pre-check is read-only, not an exception and not writable."""

    async def check(pool) -> None:
        task_id = "checked-late"
        repo, runtime = await _removed_worktree(pool, tmp_path, task_id)
        branch = tw.task_branch(task_id)
        other = tmp_path / "checked-elsewhere"
        before_task, before_tree = await _columns(pool, task_id)
        original = ws.branch_checked_out
        armed = {"yes": True}

        async def checked(repo_dir: str, name: str) -> bool:
            held = await original(repo_dir, name)
            if armed["yes"] and name == branch and not held:
                armed["yes"] = False
                git(repo, "worktree", "add", "-q", str(other), branch)
                return False
            return held

        monkeypatch.setattr(ws, "branch_checked_out", checked)
        opened = await tw.reopen(await tasks.get_task(task_id), runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.READ_ONLY
        assert opened.reason == "branch_checked_out"
        assert opened.work_dir == str(repo)
        assert git(other, "symbolic-ref", "--short", "HEAD") == branch
        assert not (runtime / "task-worktrees" / task_id).exists()
        assert await _columns(pool, task_id) == (before_task, before_tree)

    run_db(check, monkeypatch)


def test_reopen_in_place_missing(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        place = tmp_path / "a" / "b"
        place.mkdir(parents=True)
        owner = await projects.create_project(name="In place", work_dir=str(place), pool=pool)
        await tasks.create_task(
            task_id="inplace", requirement="Stay here", project_id=owner["id"], status="init",
            work_dir=str(place), pool=pool)
        opened = await tw.reopen(await tasks.get_task("inplace"), runtime_root=tmp_path / "runtime",
                                 links=(), note=_note)
        assert opened.mode == tw.ReopenMode.WRITE and opened.reason is None
        assert opened.work_dir == str(place)
        shutil.rmtree(tmp_path / "a")
        before_task, = await _task_only(pool, "inplace")
        opened = await tw.reopen(await tasks.get_task("inplace"), runtime_root=tmp_path / "runtime",
                                 links=(), note=_note)
        assert opened.mode == tw.ReopenMode.READ_ONLY
        assert opened.reason == "workspace_missing"
        assert opened.work_dir == str(tmp_path)
        assert not (tmp_path / "a").exists()
        after_task, = await _task_only(pool, "inplace")
        assert after_task == before_task

    run_db(check, monkeypatch)


async def _task_only(pool, task_id: str):
    row = await pool.fetchrow(
        "SELECT status, work_dir, updated_at, completed_at FROM tasks WHERE id=$1", task_id)
    return (dict(row),)


def test_sweep_after_rebuild(monkeypatch, tmp_path) -> None:
    async def check(pool) -> None:
        repo, runtime = await _removed_worktree(pool, tmp_path, "sweep")
        branch = tw.task_branch("sweep")
        task = await tasks.get_task("sweep")
        opened = await tw.reopen(task, runtime_root=runtime, links=(), note=_note)
        assert opened.mode == tw.ReopenMode.WRITE
        (Path(opened.work_dir) / "later.txt").write_text("later\n")
        assert await tw.sweep(runtime) == 1
        assert not Path(opened.work_dir).exists()
        assert git(repo, "show", f"{branch}:later.txt") == "later"
        assert git(repo, "show", f"{branch}:left.txt") == "left"
        assert (await db_worktrees.get("sweep"))["state"] == "removed"
        assert (await tasks.get_task("sweep"))["status"] == "completed"
        again = await tw.reopen(await tasks.get_task("sweep"), runtime_root=runtime, links=(), note=_note)
        assert again.mode == tw.ReopenMode.WRITE and again.work_dir == opened.work_dir
        assert (Path(again.work_dir) / "left.txt").read_text() == "left\n"
        assert (Path(again.work_dir) / "later.txt").read_text() == "later\n"

    run_db(check, monkeypatch)
