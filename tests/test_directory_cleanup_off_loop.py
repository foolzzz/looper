"""Slow directory removal must not delay unrelated event-loop work."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import keeper, projects, room, step_workspaces, task_workspaces
from looper.agents.kept_run import KeptRun
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.runtimes.channel import RunStartError
from looper.agents.runtimes.cursor_agent import CursorAgentRuntime
from tests.runtime_model_helpers import builtin_launch


async def _responsive(operation):
    gaps = []
    running = True

    async def tick():
        previous = time.monotonic()
        while running:
            await asyncio.sleep(0.01)
            current = time.monotonic()
            gaps.append(current - previous)
            previous = current

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    try:
        await operation
    finally:
        running = False
        await ticker
    assert gaps and max(gaps) < 0.2, gaps


@pytest.fixture
def slow_removal(monkeypatch):
    original = shutil.rmtree
    calls = []

    def remove(path, *args, **kwargs):
        calls.append(Path(path))
        time.sleep(0.5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove)
    return calls


@pytest.mark.parametrize("operation", ["remove", "forget_worktree", "remove_orphan"])
def test_step_directory_cleanup_keeps_loop_responsive(tmp_path, monkeypatch, slow_removal, operation):
    path = tmp_path / "worktree"
    path.mkdir()
    monkeypatch.setattr(step_workspaces, "_git", AsyncMock())
    workspace = step_workspaces.StepWorkspace(
        id=1, task_id="task", step_id="step", repo_dir=str(tmp_path / "missing"), subdir="",
        target_branch="main", path=str(path), branch="looper/task/step", base_commit="a" * 40,
    )
    if operation == "remove":
        call = step_workspaces.remove(workspace)
    elif operation == "forget_worktree":
        call = step_workspaces.forget_worktree(str(tmp_path), path)
    else:
        call = step_workspaces.remove_orphan(path)
    asyncio.run(_responsive(call))
    assert slow_removal == [path]
    assert not path.exists()


def test_cursor_run_cleanup_keeps_loop_responsive(tmp_path, monkeypatch, slow_removal):
    from looper.agents.runtimes import cursor_agent

    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(cursor_agent, "run_tmpdir", lambda directory: temporary)
    command = ["env", f"CURSOR_CONFIG_DIR={tmp_path / 'config'}", "CURSOR_DATA_DIR=data", f"TMPDIR={temporary}"]
    asyncio.run(_responsive(CursorAgentRuntime().after_run(builtin_launch(), command)))
    assert slow_removal == [temporary]
    assert not temporary.exists()


def test_failed_keeper_launch_cleanup_keeps_loop_responsive(tmp_path, monkeypatch, slow_removal):
    directory = tmp_path / "run"
    handle = KeptRun(CLAUDE_CODE, ["absent-runtime"], launch=builtin_launch(), cwd=tmp_path,
                     session_dir=None, label="failed launch", run_dir=directory,
                     runtime_root=tmp_path, epoch=1, on_stale=lambda reason: None)

    async def run():
        monkeypatch.setattr(keeper, "boot_argv", lambda *args: ["absent-runtime"])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("cannot start")))
        monkeypatch.setattr(handle, "end", AsyncMock())
        with pytest.raises(RunStartError, match="cannot start"):
            await handle.launch()
        assert not directory.exists()
        assert not handle._owns_dir

    asyncio.run(_responsive(run()))
    assert slow_removal == [tmp_path / "run"]


@pytest.mark.parametrize("commit_failure", [False, True])
def test_task_session_cleanup_follows_commit(tmp_path, monkeypatch, slow_removal, commit_failure):
    directory = room.run_session_dir(tmp_path, "codex", "task", "run").parent
    directory.mkdir(parents=True)
    committed = False
    task = {"id": "task", "status": "stopped"}
    monkeypatch.setattr(projects.db_tasks, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(projects.db_tasks, "delete_task", AsyncMock())
    monkeypatch.setattr(step_workspaces, "remove_task", AsyncMock())
    monkeypatch.setattr(task_workspaces, "finish", AsyncMock())

    @asynccontextmanager
    async def transaction():
        nonlocal committed
        yield object()
        if commit_failure:
            raise RuntimeError("commit failed")
        committed = True

    monkeypatch.setattr(projects.db_connection, "transaction", transaction)
    remove = shutil.rmtree

    def checked_remove(*args, **kwargs):
        assert committed, "sessions were deleted before the database committed"
        return remove(*args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", checked_remove)

    async def run():
        if commit_failure:
            with pytest.raises(RuntimeError, match="commit failed"):
                await projects.delete_task("task", tmp_path)
            assert directory.exists()
        else:
            assert await projects.delete_task("task", tmp_path) == {"deleted": "task"}
            assert not directory.exists()

    asyncio.run(_responsive(run()))
    assert slow_removal == ([] if commit_failure else [directory])


def test_startup_session_sweep_keeps_loop_responsive(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from looper import db, ops_config
    from looper.agents import operator_notes, profile_registry, serve
    from looper.db import room as db_room
    from tests.db_support import DSN, run_db

    class Swept(Exception):
        pass

    async def run(pool):
        directory = room.run_session_dir(tmp_path, "codex", "ended-task", "run").parent
        directory.mkdir(parents=True)
        gaps = []
        ticking = True
        ticker = None

        async def tick():
            previous = time.monotonic()
            while ticking:
                await asyncio.sleep(0.01)
                current = time.monotonic()
                gaps.append(current - previous)
                previous = current

        async def keeping_sessions(**kwargs):
            nonlocal ticker
            ticker = asyncio.create_task(tick())
            await asyncio.sleep(0)
            return []

        original_measure = serve._StartupSteps.measure

        @contextmanager
        def measure(startup, key):
            with original_measure(startup, key):
                yield
            if key == "run_sessions":
                raise Swept

        original_remove = shutil.rmtree
        removed = []

        def slow_remove(path, *args, **kwargs):
            removed.append(Path(path))
            time.sleep(0.5)
            return original_remove(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", slow_remove)
        monkeypatch.setattr(db_room, "tasks_keeping_sessions", keeping_sessions)
        monkeypatch.setattr(serve._StartupSteps, "measure", measure)
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
                            serve.self_check.Check("database", "Database", "ok", "ready")))
        # Recording is unrelated to reclaiming sessions and must not launch a recorder.
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('llmrec.enabled', 'false') "
                           "ON CONFLICT (key) DO UPDATE SET value = 'false'")
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                                   port=0, operator_notes_root=str(tmp_path), keeper_mode=ops_config.KeeperMode.OFF)
        try:
            with pytest.raises(Swept):
                await serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path, skip_backup=True)
        finally:
            ticking = False
            if ticker is not None:
                await ticker
        assert removed == [directory]
        assert not directory.exists()
        assert gaps and max(gaps) < 0.2, gaps

    run_db(run, monkeypatch, timeout=45)
