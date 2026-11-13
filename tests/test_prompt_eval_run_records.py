"""Task scoring reads full run records after the summary-only list settles."""

import dataclasses
import json

import pytest

from evals.prompt_regression import runner
from evals.prompt_regression.tasks import TASKS
from looper.agents.tiers.planner_protocol import ADJUDICATED_ACCEPT


@pytest.mark.parametrize("task_id,missing", [
    ("chat_correction", None),
    ("supplement_replan", None),
    ("requirement_edit", None),
    ("supplement_no_change", None),
    ("chat_correction", "input_text"),
    ("panel_merge", None),
    ("panel_merge", "output_text"),
    ("panel_adjudicate", None),
    ("panel_adjudicate", "input_text"),
    ("panel_adjudicate", "output_text"),
    ("chat_correction", "unavailable"),
])
def test_task_scoring_loads_run_bodies(tmp_path, task_id, missing):
    task = next(task for task in TASKS if task.id == task_id)
    panel = task_id.startswith("panel_")
    # Exercise the real metrics and panel checks independently of delivery expectations.
    task = dataclasses.replace(task, check=lambda _: None,
                               expect={} if panel else {"min_chat_turns": 1})
    judgment = {"member": "review_a", "finding": 0, "level": "suggestion", "reason": "Out of scope."}
    reply = ({"decision": "accept", "adjudications": [judgment]} if task_id == "panel_adjudicate"
             else {"decision": "retry", "feedback": "Fix left.txt and right.txt."})
    records = [
        {"id": 1, "agent_id": "worker-one", "status": "completed", "input_text": "Work",
         "output_text": "Done", "metadata": {"step_id": "s1"}},
        {"id": 2, "agent_id": "planner-one", "status": "completed",
         "input_text": f"[DECISION] {ADJUDICATED_ACCEPT}" if panel else "[CHAT] Update the task.",
         "output_text": json.dumps(reply), "metadata": {}},
    ]
    if missing in {"input_text", "output_text"}:
        records[1][missing] = ""
    detail = {"status": "completed", "cost": {"cost_usd": 0.2}, "steps": [
        {"id": "s1", "accept_when": {"all_of": ["review_a", "review_b"]}},
        {"id": "r1", "kind": "review", "reviews": "s1", "role_key": "review_a"},
        {"id": "r2", "kind": "review", "reviews": "s1", "role_key": "review_b"},
    ], "journal": [{"kind": "review_adjudicated", "summary": json.dumps(judgment)}]}

    class Api:
        def __init__(self):
            self.list_calls = 0
            self.loaded = []
            self.cancelled = False

        def post(self, path, body):
            result = {}
            if path == "/api/v1/tasks/create":
                result = {"task_id": "t1", "project_id": "p1"}
            elif path == "/api/v1/tasks/detail":
                result = detail
            elif path == "/api/v1/runs/list":
                self.list_calls += 1
                result = {"records": [{key: value for key, value in record.items()
                                       if key not in {"input_text", "output_text"}}
                                      for record in reversed(records)]}
                if self.list_calls == 1:
                    result["records"][0]["status"] = "running"
            elif path == "/api/v1/runs/get":
                assert self.list_calls == 2
                self.loaded.append(body["record_id"])
                if missing == "unavailable":
                    raise runner.EvalError("run detail unavailable")
                result = {"record": records[body["record_id"] - 1]}
            elif path == "/api/v1/runs/events":
                result = {"events": []}
            elif path == "/api/v1/tasks/activity":
                result = {"activity": []}
            elif path == "/api/v1/ops/task_control/cancel":
                self.cancelled = True
            else:
                assert path in {"/api/v1/ops/task_control/start", "/api/v1/workflows/put"}
            return {"status": "ok", "result": result}

    api = Api()
    if missing == "unavailable":
        with pytest.raises(runner.TaskError, match="run detail unavailable") as raised:
            runner.run_task(api, task, tmp_path, timeout=1, poll=0)
        assert raised.value.cost_usd == pytest.approx(0.2)
        assert api.cancelled
        return
    result = runner.run_task(api, task, tmp_path, timeout=1, poll=0)
    assert result["outcome"] == ("pass" if missing is None else "fail"), result["problems"]
    assert api.loaded == [1, 2]
    if not panel:
        assert result["chat_turns"] == int(missing is None)
