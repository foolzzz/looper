"""Slow filesystem cleanup must leave the event loop responsive."""

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import kept_run, projects, room, step_workspaces as ws, task_workspaces
from looper.agents.runtimes import cursor_agent


async def _responsive(operation):
    gaps = []
    done = False

    async def heartbeat():
        last = time.monotonic()
        while not done:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await operation
    finally:
        done = True
        await pulse
    print(f"cleanup max loop gap: {max(gaps):.3f}s")
    assert max(gaps) < 0.2


@pytest.mark.parametrize("site", ["workspace", "forget", "orphan", "cursor", "keeper"])
def test_cleanup_keeps_loop_responsive(tmp_path, monkeypatch, site):
    directory = tmp_path / "run"
    directory.mkdir()
    real_remove = shutil.rmtree
    calls = []

    def slow_remove(path, *args, **kwargs):
        calls.append(path)
        time.sleep(0.5)
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", slow_remove)
    monkeypatch.setattr(ws, "_git", AsyncMock())

    async def run():
        if site == "workspace":
            workspace = ws.StepWorkspace(1, "task", "step", str(tmp_path / "missing"), "", "main",
                                         str(directory), "step", "head")
            await _responsive(ws.remove(workspace))
        elif site == "forget":
            await _responsive(ws.forget_worktree(str(tmp_path), directory))
        elif site == "orphan":
            await _responsive(ws.remove_orphan(directory))
        elif site == "cursor":
            temporary = directory / "tmp"
            monkeypatch.setattr(cursor_agent, "run_tmpdir", lambda _: temporary)
            temporary.mkdir(parents=True)
            command = ["env", f"CURSOR_CONFIG_DIR={directory / 'config'}", "agent", f"TMPDIR={temporary}"]
            await _responsive(cursor_agent.CursorAgentRuntime().after_run(None, command))
            directory.rmdir()
            assert not temporary.exists()
        else:
            directory.rmdir()
            handle = SimpleNamespace(run_dir=directory, manifest={}, _grant=None, _runtime_root=tmp_path,
                                     _cwd=tmp_path, command=["unused"], _check_launch=lambda: None,
                                     _launch=SimpleNamespace(environment=lambda: {}), end=AsyncMock(),
                                     state=kept_run.KeptRunState.STARTING, _owns_dir=False)
            handle.release = MethodType(kept_run.KeptRun.release, handle)
            monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("spawn failed")))

            async def failed_launch():
                with pytest.raises(kept_run.RunStartError, match="spawn failed"):
                    await kept_run.KeptRun.launch(handle)

            await _responsive(failed_launch())
        assert calls
        assert not directory.exists()

    asyncio.run(run())


@pytest.mark.parametrize("commit_fails", [False, True])
def test_delete_sessions_after_commit_without_blocking(tmp_path, monkeypatch, commit_fails):
    directory = room.run_session_dir(tmp_path, "codex", "task", "run")
    directory.mkdir(parents=True)
    transaction_open = False
    calls = []
    real_remove = shutil.rmtree

    @asynccontextmanager
    async def transaction():
        nonlocal transaction_open
        transaction_open = True
        try:
            yield object()
            if commit_fails:
                raise RuntimeError("commit failed")
        finally:
            transaction_open = False

    def slow_remove(path, *args, **kwargs):
        assert not transaction_open
        calls.append(path)
        time.sleep(0.5)
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", slow_remove)
    monkeypatch.setattr(projects.db_connection, "transaction", transaction)
    monkeypatch.setattr(projects.db_tasks, "get_task", AsyncMock(return_value={"status": "stopped"}))
    monkeypatch.setattr(projects.db_tasks, "delete_task", AsyncMock())
    monkeypatch.setattr(ws, "remove_task", AsyncMock())
    monkeypatch.setattr(task_workspaces, "finish", AsyncMock())

    async def run():
        if commit_fails:
            with pytest.raises(RuntimeError, match="commit failed"):
                await projects.delete_task("task", tmp_path)
            assert directory.exists() and not calls
        else:
            await _responsive(projects.delete_task("task", tmp_path))
            assert calls and not directory.exists()

    asyncio.run(run())
