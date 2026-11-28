"""A direct launch's private temporary directory: one value for the process and the readback, gone with the session."""
import os

import pytest

from looper.agents import task_manager_edit, task_manager_readback, task_manager_tmpdir
from looper.agents.tiers import task_manager as tm
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def test_each_launch_gets_its_own_directory_which_is_removed_when_its_session_ends(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", "/tmp/inherited-value")  # never what a direct session gets
    seen = []

    def spy(environment, *, forbidden):
        seen.append(tuple(forbidden))
        return native.edit_dir

    async def check(db):
        task_id, _, directory = await native.create(db)
        monkeypatch.setattr(task_manager_edit, "edit_bin", spy)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            agent = pool.agent(task_id)
            [first] = native.records("launch")
            assert first["tmpdir"] == agent.tmpdir.path and first["tmpdir_exists"]
            assert first["tmpdir"].startswith(f"/tmp/lpr-{os.getuid()}/")
            # The edit directory is checked against the sandbox's write set, the private root included.
            assert agent.tmpdir.write_root in seen[-1]
            assert task_manager_readback.shared_temp_root(os.getuid()) not in seen[-1]
            (directory / "AGENTS.md").write_text("A new rule.\n")  # restarts the session
            await submit_and_wait(pool, task_id, message_id="edited")
            second = native.records("launch")[1]
            assert second["tmpdir"] != first["tmpdir"] and second["tmpdir_exists"]
            assert not os.path.exists(first["tmpdir"]) and os.path.isdir(second["tmpdir"])
        finally:
            await pool.shutdown()
        assert not os.path.exists(native.records("launch")[-1]["tmpdir"])
    run_db(check, monkeypatch, timeout=60)


def test_a_directory_that_cannot_be_made_refuses_the_launch_with_its_reason(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    def fail():
        raise task_manager_tmpdir.Unusable("synthetic: not usable")

    async def check(db):
        task_id, _, _ = await native.create(db)
        monkeypatch.setattr(task_manager_tmpdir, "create", fail)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="message-one", sink=sink)
            await sink.done()
            assert result.state == "refused" and result.reason is tm.RefuseReason.PRIVATE_TMPDIR_UNUSABLE
            assert "synthetic: not usable" in sink.finals[0][0]
            assert native.records("launch") == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_an_orchestrated_launch_gets_no_private_directory(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)

    async def check(db):
        task_id, _, _ = await native.create(db, status="pending")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            assert native.records("launch")[-1]["tmpdir"] is None and pool.agent(task_id).tmpdir is None
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)
