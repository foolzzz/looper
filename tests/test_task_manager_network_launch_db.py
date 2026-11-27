"""The project's network allowlist reaches the direct session's launch settings and signature."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looper.agents.tiers import task_manager as tm
from looper.db import task_manager_sandbox_policy as policy
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, submit_and_wait


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


def _launch_settings(record) -> tuple[str, dict]:
    command = record["command"]
    file = command[command.index("--settings") + 1]
    return file, json.loads(Path(file).read_text())


def test_the_list_reaches_the_settings_and_a_change_resumes_the_same_session_with_the_new_one(
        monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await submit_and_wait(pool, task_id, "first", message_id="m1")
            [first] = native.records("launch")
            first_file, first_settings = _launch_settings(first)
            assert first_settings["sandbox"]["network"]["allowedDomains"] == []  # the default: nothing is allowed
            assert "--resume" not in first["command"]
            session = pool.agent(task_id).session_id
            await policy.update(project_id, ["registry.npmjs.org", "*.pypi.org"], actor="operator")
            await submit_and_wait(pool, task_id, "second", message_id="m2")
            first, second = native.records("launch")
            second_file, second_settings = _launch_settings(second)
            assert second_settings["sandbox"]["network"]["allowedDomains"] == ["registry.npmjs.org", "*.pypi.org"]
            assert second_file != first_file  # the file is named by its content, so the signature changed
            assert second["command"][second["command"].index("--resume") + 1] == session
            await submit_and_wait(pool, task_id, "third", message_id="m3")
            assert len(native.records("launch")) == 2  # an unchanged list does not restart the process
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_an_orchestrated_session_carries_no_sandbox_settings_and_reads_no_policy(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db, status="pending")
        await db.execute("INSERT INTO task_manager_sandbox_policies (project_id, network_allowed_domains, revision, "
                         "updated_by) VALUES ($1, '[\"10.0.0.1\"]', 1, 'x')", project_id)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)
        try:
            await pool.agent(task_id).prepare()  # a stored entry that would refuse a direct launch does not matter
            assert pool.agent(task_id).allowed_domains == ()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
