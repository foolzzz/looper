"""The verification eval scores the repair and review path, not just delivered files."""
from __future__ import annotations

from pathlib import Path
import copy
from dataclasses import replace
import json

import pytest

from evals.prompt_regression import runner
from evals.prompt_regression.tasks import TASKS
from tests.db_support import DSN


def test_workflow_verification_case_requires_reviewed_repair():
    task = next(task for task in TASKS if task.id == "workflow_verify_loop")
    assert task.flow_check is not None
    steps = [
        {"id": "v1", "role_key": "verify_staging", "retries": 0,
         "result": {"status": "failed"}, "depends_on": ["prepare"]},
        {"id": "repair", "kind": "work", "status": "verified", "role_key": "dev", "depends_on": []},
        {"id": "v2", "role_key": "verify_staging", "retries": 0,
         "result": {"status": "completed"}, "depends_on": ["repair"]},
    ]
    records = [
        {"agent_id": "reviewer-1", "metadata": {"step_id": "repair", "outcome": "passed"}},
        {"agent_id": "worker-first", "metadata": {"step_id": "v1"}, "finished_at": "2026-01-01T00:00:01Z"},
        {"agent_id": "worker-repair", "metadata": {"step_id": "repair"}, "created_at": "2026-01-01T00:00:02Z"},
    ]
    assert task.flow_check({"steps": steps}, records, {}) is None
    assert task.flow_check({"steps": steps}, records[1:], {}) is not None
    records[-1]["created_at"] = "2026-01-01T00:00:00Z"
    assert task.flow_check({"steps": steps}, records, {}) is not None
    records[-1]["created_at"] = "2026-01-01T00:00:02Z"
    steps[-1]["depends_on"] = ["prepare"]
    assert task.flow_check({"steps": steps}, records, {}) is not None
    steps[-1]["depends_on"] = ["repair"]
    steps[0]["retries"] = 1
    assert task.flow_check({"steps": steps}, records, {}) is not None


@pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL database")
@pytest.mark.parametrize("changes_code", [False, True])
def test_workflow_verification_eval_real_serve_path(tmp_path: Path, monkeypatch, changes_code: bool):
    """Real server and CLI processes; the scripted CLI is not a real model evaluation."""
    task = next(task for task in TASKS if task.id == "workflow_verify_loop")
    activity = []
    read_activity = runner._all_activity

    def capture_activity(api, task_id):
        rows = read_activity(api, task_id)
        activity.extend(rows)
        return rows

    monkeypatch.setattr(runner, "_all_activity", capture_activity)
    if changes_code:
        fake = copy.deepcopy(task.fake)
        # The CLI really writes an untracked file but falsely reports no changes and success.
        fake["attempts"]["s2"] = [{"files": {"unexpected.txt": "changed\n"}, "raw": json.dumps({
            "status": "completed", "changed_files": {}, "deleted_files": [], "summary": "Verification complete."})}]
        original_check = task.flow_check

        def check_guard(detail, records, events):
            failed = next(step for step in detail["steps"] if step["step_key"] == "s2")["result"]
            assert failed["failure"] == "verification_changed_code"
            assert failed["verification"]["git"] is True
            assert failed["verification"]["status_sha256"] != failed["verification"]["status_sha256_after"]
            return original_check(detail, records, events)

        task = replace(task, fake=fake, flow_check=check_guard)
    result, _ = runner.run(mode="fake", model="haiku", tasks=(task,), admin_url=DSN,
                           out_dir=tmp_path / "runs", reps=1, task_timeout=60)
    assert not result.get("aborted"), result
    assert result["tasks"][0]["success"], result["tasks"][0]
    if changes_code:
        assert any("Verification changed the code" in str(row.get("summary")) for row in activity), activity
