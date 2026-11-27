"""A direct task Manager starts only in a checked, unshared in-place directory."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from looper.agents import task_manager_dirs, task_manager_safety as safety
from looper.agents.tiers import task_manager as tm
from looper.db import tasks
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    (path / "README.md").write_text("Synthetic repository\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-q", "--no-gpg-sign", "-m", "Initial"],
        check=True, capture_output=True)
    return path


def _count(reason: str) -> int:
    return safety.refusal_counts[(reason, "launch", "unknown")]


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


def test_worktree_direct_session_is_refused_and_orchestrated_still_starts(monkeypatch, tmp_path, native):
    async def check(db):
        repository = _repo(tmp_path / "repository")
        task_id, _, _ = await native.create(db, workspace_mode="worktree", path=repository, git=False)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            before = _count("worktree_unsupported")
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="direct", sink=sink)
            assert result.state == "refused" and result.reason is tm.RefuseReason.WORKTREE_UNSUPPORTED
            await sink.done()
            assert ("refused", {"reason": result.reason}) in sink.events
            assert "own worktree" in sink.finals[0][0]
            assert native.records("launch") == []
            assert _count("worktree_unsupported") == before + 1
            assert not pool.agent(task_id).resident.started

            await tasks.update_task_status(task_id, "running", pool=db)
            started = await submit_and_wait(pool, task_id, message_id="orchestrated")
            assert native.records("launch")
            assert any(kind == "turn_summary" and payload["mode"] == "orchestrated" for kind, payload in started.events)
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_second_direct_session_on_a_directory_waits_for_the_lock(monkeypatch, tmp_path, native):
    async def check(db):
        repository = _repo(tmp_path / "repository")
        runtime = tmp_path / "runtime"
        first, _, _ = await native.create(db, "task-one", path=repository, git=False)
        second, _, _ = await native.create(db, "task-two", path=repository, git=False)
        pool = tm.TaskManagerPool(runtime, native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, first)
            assert pool.agent(first).resident.started
            before = _count("workspace_overlaps_session")
            sink = Sink()
            result = await pool.submit(second, "reply", message_id="overlap", sink=sink)
            assert result.state == "refused" and result.reason is tm.RefuseReason.WORKSPACE_OVERLAPS_SESSION
            await sink.done()
            assert ("refused", {"reason": result.reason}) in sink.events
            place = os.path.realpath(repository)
            assert first in sink.finals[0][0] and place in sink.finals[0][0]
            assert len(native.records("launch")) == 1
            assert _count("workspace_overlaps_session") == before + 1
            with pytest.raises(task_manager_dirs.LockHeld) as held:
                task_manager_dirs.DirectoryLock.acquire(runtime, str(repository), "probe")
            assert held.value.holder == first

            await pool.agent(first).resident.stop()
            probe = task_manager_dirs.DirectoryLock.acquire(runtime, str(repository), "probe")
            probe.release()
            await submit_and_wait(pool, second, message_id="after")
            assert len(native.records("launch")) == 2
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_subdirectory_of_a_repository_is_not_a_direct_workspace(monkeypatch, tmp_path, native):
    async def check(db):
        repository = _repo(tmp_path / "repository")
        child = repository / "nested"
        child.mkdir()
        task_id, _, _ = await native.create(db, path=child, git=False)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            before = _count("workspace_not_repo_root")
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="nested", sink=sink)
            assert result.state == "refused" and result.reason is tm.RefuseReason.WORKSPACE_NOT_REPO_ROOT
            await sink.done()
            assert ("refused", {"reason": result.reason}) in sink.events
            assert "subdirectory" in sink.finals[0][0] and os.path.realpath(repository) in sink.finals[0][0]
            assert native.records("launch") == []
            assert _count("workspace_not_repo_root") == before + 1
            assert not pool.agent(task_id).resident.started
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
