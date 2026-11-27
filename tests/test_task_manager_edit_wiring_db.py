"""A direct session's PATH starts with the private looper-edit directory; an orchestrated one is untouched."""
import os
import sys

import pytest

from looper.agents import task_manager_edit, task_manager_safety
from looper.agents.tiers import task_manager as tm
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, submit_and_wait


def _install(monkeypatch, tmp_path, native, *, script=True):
    """The real ``edit_bin`` with an interpreter whose directory holds (or lacks) the console script."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    if script:
        (bin_dir / "looper-edit").write_text("#!/bin/sh\n")
        (bin_dir / "looper-edit").chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    cache = tmp_path / "cache"
    (tmp_path / "sandbox-tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "sandbox-tmp"))  # pytest's tmp_path is under the system TMPDIR
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(task_manager_edit, "edit_bin", native.real_edit_bin)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))  # after the scripted CLI exists
    return bin_dir, cache / "looper" / "task-manager-bin"


def test_direct_launch_path_starts_with_the_private_edit_directory_also_after_a_restart(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    bin_dir, private = _install(monkeypatch, tmp_path, native)

    async def check(db):
        task_id, _, directory = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            [launch] = native.records("launch")
            assert launch["path"].split(os.pathsep)[0] == str(private)
            assert (private / "looper-edit").is_symlink()
            assert os.readlink(private / "looper-edit") == str(bin_dir / "looper-edit")
            assert oct(private.stat().st_mode & 0o777) == "0o700"
            # A changed instruction file restarts and resumes the session: the restart carries the PATH too.
            (directory / "AGENTS.md").write_text("A new rule.\n")
            await submit_and_wait(pool, task_id, message_id="edited")
            relaunch = native.records("launch")[1]
            assert "--resume" in relaunch["command"]
            assert relaunch["path"].split(os.pathsep)[0] == str(private)
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_orchestrated_launch_path_is_what_environment_gives_without_edit_dir(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    _install(monkeypatch, tmp_path, native)

    async def check(db):
        task_id, _, _ = await native.create(db, status="pending")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            _, _, launch, _ = await agent.prepare()
            expected = task_manager_safety.environment(launch, task_id)["PATH"]
            assert not (tmp_path / "cache").exists()
            await agent.resident.stop()
            await submit_and_wait(pool, task_id)
            assert native.records("launch")[-1]["path"] == expected
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_missing_console_script_fails_the_turn_before_any_process_starts(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    _install(monkeypatch, tmp_path, native, script=False)

    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            with pytest.raises(RuntimeError, match=r"looper-edit is not installed next to .*; reinstall Looper"):
                await agent.prepare()
            assert not agent.resident.started
            assert native.records("launch") == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_edit_directory_under_the_task_directory_fails_the_turn(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    _install(monkeypatch, tmp_path, native)

    async def check(db):
        task_id, _, directory = await native.create(db)
        monkeypatch.setenv("XDG_CACHE_HOME", str(directory / "cache"))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            with pytest.raises(RuntimeError, match="set XDG_CACHE_HOME"):
                await agent.prepare()
            assert not agent.resident.started
            assert native.records("launch") == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_read_deny_is_set_before_the_edit_directory_is_checked(monkeypatch, tmp_path):
    """``edit_bin`` receives card 1's read deny list, so the command (which sets it) must be built first."""
    native = NativeTaskManager(monkeypatch, tmp_path)
    seen = []

    def spy(environment, *, forbidden):
        seen.append(tuple(forbidden))
        return native.edit_dir

    async def check(db):
        task_id, _, directory = await native.create(db)
        monkeypatch.setattr(task_manager_edit, "edit_bin", spy)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            await agent.prepare()
            assert agent.read_deny and seen
            assert all(set(agent.read_deny) <= set(forbidden) for forbidden in seen)
            assert str(directory) in seen[0]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)
