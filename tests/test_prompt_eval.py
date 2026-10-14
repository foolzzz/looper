"""The prompt regression eval (``evals/prompt_regression``): its own contract, and the task set in fake mode.

The fake-mode run needs PostgreSQL: it runs only when LOOPX_TEST_DATABASE_URL
points at a server where it may create (and then drop) a database of its own.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import compare, metrics, runner
from evals.prompt_regression.tasks import TASKS, EvalTask
from loopx.agents.tiers.planner import PlannerAgent, PlannerTurnError
from loopx.agents.tiers.planner_protocol import TurnKind, parse_plan
from loopx.agents.tiers.worker import judge_result

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")


# --- Contract violations, read where the agent system rejects a reply ------------------------------------


def _planner_activity(tmp_path: Path, replies: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    """The activity the real ``PlannerAgent._ask`` logs for these replies, and the error it raised, if any."""
    planner = PlannerAgent(task_id="task-0123456789ab", runtime_root=tmp_path)
    activity: list[dict[str, Any]] = []
    answers = iter(replies)

    async def turn(_message: str) -> str:
        return next(answers)

    async def log(kind: str, summary: str, **_: Any) -> None:
        activity.append({"kind": kind, "summary": summary})

    planner._turn = turn  # type: ignore[method-assign]
    planner._activity = log  # type: ignore[method-assign]
    try:
        asyncio.run(planner._ask(TurnKind.PLAN, "[PLAN] plan it", parse_plan))
    except PlannerTurnError as exc:
        # Every caller logs the error once, as an "error" activity that quotes it.
        activity.append({"kind": "error", "summary": f"Task failed: Planning failed: {exc}"})
        return activity, str(exc)
    return activity, None


_PLAN = '```json\n{"steps": [{"id": "s1", "instruction": "do it", "acceptance_criteria": ["done"], ' \
        '"depends_on": [], "review": false}]}\n```'


@pytest.mark.parametrize(("replies", "expected"), [
    ([_PLAN], 0),
    (["no json block", _PLAN], 1),
    (["no json block", "still none"], 2),
])
def test_planner_violations_count_the_replies_its_parser_rejected(
    tmp_path: Path, replies: list[str], expected: int,
) -> None:
    activity, _ = _planner_activity(tmp_path, replies)
    activity.append({"kind": "phase", "summary": "Plan created: 1 step(s)"})
    assert metrics.planner_violations(activity) == expected


@pytest.mark.parametrize(("text", "is_verify"), [
    ('Done.\n{"status": "completed", "changed_files": {}, "deleted_files": [], "summary": "ok"}', False),
    ('{"status": "failed", "changed_files": {}, "deleted_files": [], "summary": "could not"}', False),
    ("I did it, the file is there.", False),
    ('{"status": "done", "changed_files": {}, "deleted_files": []}', False),
    ('{"status": "completed", "changed_files": ["a.txt"], "deleted_files": []}', False),
    ('{"passed": true, "findings": [], "summary": "ok"}', True),
    ('{"passed": false, "findings": ["x"], "summary": "no"}', True),
    ("Looks good to me.", True),
    ('{"passed": "yes"}', True),
])
def test_report_violation_agrees_with_judge_result(tmp_path: Path, text: str, is_verify: bool) -> None:
    judged = judge_result({"type": "result", "subtype": "success", "result": text}, work_dir=tmp_path,
                          is_verify=is_verify)
    format_failures = ("Worker did not end with the JSON status report", "Reviewer did not end with the JSON verdict",
                       "Worker reported an unknown status", "Worker report has malformed")
    rejected = any(str(judged.get("summary") or judged.get("error") or "").startswith(prefix)
                   for prefix in format_failures)
    assert metrics.report_violation(text, is_verify=is_verify) is rejected


# --- The Planner's own file changes ---------------------------------------------------------------------


def test_edit_tool_paths_are_relative_and_never_local_paths(tmp_path: Path) -> None:
    def tool(name: str, path: str) -> dict[str, Any]:
        return {"event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": {"file_path": path}}]}}}

    events = [tool("Write", str(tmp_path / "a.txt")), tool("Read", str(tmp_path / "b.txt")),
              tool("Edit", "sub/c.txt"), tool("Write", "/elsewhere/secret/d.txt")]
    assert metrics.edit_tool_paths(events, tmp_path) == [
        "a.txt", "sub/c.txt", "<outside the working directory>/d.txt"]


def test_changed_files_are_the_planners_only_while_no_step_ran() -> None:
    windows = {"planner": [(100.0, 110.0), (130.0, 140.0)], "worker": [(110.05, 129.0)],
               "reviewer": [(141.0, 150.0)]}
    changes = {"in-plan-turn.txt": 105.0, "by-worker.txt": 120.0, "near-worker-start.txt": 109.98,
               "after-all.txt": 160.0}
    assert metrics.planner_changed_files(changes, windows) == ["in-plan-turn.txt"]
    # The database clock runs 50 s ahead of the local one: the windows move back by 50 s.
    shifted = {name: [(a + 50, b + 50) for a, b in spans] for name, spans in windows.items()}
    assert metrics.planner_changed_files(changes, shifted, clock_offset=50.0) == ["in-plan-turn.txt"]


# --- One task's metrics ---------------------------------------------------------------------------------


def _record(record_id: int, agent_id: str, *, input_text: str = "", step_id: str | None = None,
            start: str = "2026-10-13T10:00:00+00:00", end: str = "2026-10-13T10:00:05+00:00") -> dict[str, Any]:
    return {"id": record_id, "agent_id": agent_id, "input_text": input_text, "status": "completed",
            "metadata": {"step_id": step_id} if step_id else {}, "created_at": start, "finished_at": end}


def _result(text: str) -> list[dict[str, Any]]:
    return [{"seq": 1, "event": {"type": "result", "subtype": "success", "result": text}}]


def test_task_metrics_read_the_flow_cost_and_violations(tmp_path: Path) -> None:
    good = '{"status": "completed", "changed_files": {}, "deleted_files": [], "summary": "ok"}'
    records = [
        _record(1, "planner-task-1", input_text="[PLAN] Plan the task"),
        _record(2, "planner-task-1", input_text="[PLAN] Your last reply broke the reply contract: x"),
        _record(3, "worker-a", step_id="step-1"),
        _record(4, "planner-task-1", input_text="[DECISION] Step s1"),
        _record(5, "worker-b", step_id="step-1"),
        _record(6, "planner-task-1", input_text="[CHAT] A message from the user."),
        _record(7, "reviewer-c"),
    ]
    events = {3: _result("no report"), 5: _result(good), 7: _result('{"passed": true}')}
    detail = {"status": "completed", "steps": [{"id": "step-1"}],
              "cost": {"cost_usd": 0.0123, "runs": 7, "by_role": {"planner": {"cost_usd": 0.004, "runs": 4}}}}
    activity = [{"kind": "error", "summary": "Planner reply broke the PLAN contract (the reply has no ```json "
                                             "fenced block); asking again"}]
    result = metrics.task_metrics(
        detail=detail, activity=activity, records=records, events=events, changes={}, work_dir=tmp_path,
        check_problem=None, expect={"min_retries": 1, "min_reviews": 2}, now=0.0,
    )
    assert result["success"] is False
    assert result["problems"] == ["reviews 1 < 2"]
    assert (result["steps"], result["retries"], result["reviews"], result["chat_turns"]) == (1, 1, 1, 1)
    assert result["planner_turns"] == {"plan": 1, "repair": 1, "decision": 1, "chat": 1}
    assert result["contract_violations"] == {"planner": 1, "worker": 1, "reviewer": 0, "total": 2}
    assert (result["cost_usd"], result["cost_by_role"], result["planner_modified_files"]) == (
        0.0123, {"planner": 0.004}, 0)


# --- Comparison -----------------------------------------------------------------------------------------


def _run(**tasks: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "real", "model": "haiku", "git": {"commit": "abcdef1234567", "dirty": False},
            "tasks": [{"id": task_id, **values} for task_id, values in tasks.items()]}


_OK = {"success": True, "steps": 1, "retries": 0, "reviews": 0, "contract_violations": {"total": 0},
       "planner_modified_files": 0, "cost_usd": 0.02}


def test_comparison_marks_what_changed_and_sums_the_run() -> None:
    base = _run(single_step=_OK, chat_correction=_OK)
    new = _run(single_step={**_OK, "cost_usd": 0.03},
               chat_correction={**_OK, "success": False, "contract_violations": {"total": 2},
                                "problems": ["color.txt holds 'red', expected 'blue'"]})
    new["git"]["dirty"] = True
    table = compare.markdown(new, base)
    lines = table.splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 → real/haiku @ abcdef123+local changes"
    assert "| single_step | pass | 1 | 0 | 0 | 0 | 0 | 0.0200 → **0.0300** |" in lines
    assert "| chat_correction | pass → **FAIL** | 1 | 0 | 0 | 0 → **2** | 0 | 0.0200 |" in lines
    assert "| **total** | 2/2 → **1/2** | 2 | 0 | 0 | 0 → **2** | 0 | 0.0400 → **0.0500** |" in lines
    assert "- chat_correction: color.txt holds 'red', expected 'blue'" in lines


def test_comparison_without_a_base_and_with_skipped_tasks() -> None:
    new = _run(single_step=_OK, chat_correction={"skipped": "the spending cap was reached", "cost_usd": 0.0})
    new.update(stopped_by_cap=True, caps={"total_cap_usd": 0.8})
    lines = compare.markdown(new).splitlines()
    assert lines[0].endswith("(no earlier run to compare with)")
    assert "| chat_correction | skipped | skipped | skipped | skipped | skipped | skipped | skipped |" in lines
    assert "| **total** | 1/1 | 1 | 0 | 0 | 0 | 0 | 0.0200 |" in lines
    assert "- stopped at the spending cap (0.8 USD)" in lines


# --- The spending cap -----------------------------------------------------------------------------------


def _task(task_id: str) -> EvalTask:
    return EvalTask(id=task_id, requirement="r", check=lambda _work_dir: None)


def test_the_run_stops_at_the_total_cap() -> None:
    seen: list[tuple[str, float | None]] = []

    def run_one(task: EvalTask, cap_left: float | None) -> dict[str, Any]:
        seen.append((task.id, cap_left))
        return {"id": task.id, "cost_usd": 0.3}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=0.5)
    assert [(t, round(c, 2)) for t, c in seen] == [("a", 0.5), ("b", 0.2)]
    assert stopped is True
    assert [r.get("skipped") for r in results] == [None, None, "the spending cap was reached",
                                                    "the spending cap was reached"]
    _, stopped = runner.run_suite(tuple(_task(t) for t in "ab"), run_one, total_cap_usd=None)
    assert stopped is False


class _CostlyApi:
    """A task whose cost grows 0.1 per poll; it is cancelled when asked and then reports so."""

    def __init__(self) -> None:
        self.cost = 0.0
        self.posts: list[tuple[str, Any]] = []
        self.cancelled = False

    def post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        if path == "/api/tasks/cancel":
            self.cancelled = True
        return {"task_id": "task-1"} if path == "/api/tasks/create" else {}

    def get(self, path: str, **_query: Any) -> Any:
        if path == "/api/tasks/task-1":
            if not self.cancelled:
                self.cost += 0.1
            status = "cancelled" if self.cancelled else "running"
            return {"status": status, "phase": "executing", "steps": [], "cost": {"cost_usd": self.cost}}
        if path == "/api/llm-records":
            return {"records": []}
        return {"activity": [], "events": []}


def test_a_task_is_cancelled_when_it_reaches_the_cap_left(tmp_path: Path) -> None:
    api = _CostlyApi()
    result = runner.run_task(api, _task("a"), tmp_path / "work", timeout=60, poll=0, cap_left=0.25)  # type: ignore[arg-type]
    assert [path for path, _ in api.posts] == ["/api/tasks/create", "/api/tasks/start", "/api/tasks/cancel"]
    assert result["stopped_by_cap"] is True and result["success"] is False
    assert "cancelled at the spending cap" in result["problems"]
    assert result["cost_usd"] == pytest.approx(0.3)


def test_real_claude_gets_the_per_run_budget(tmp_path: Path) -> None:
    script = runner.install_claude(tmp_path, mode="real", max_budget_usd=0.25, real_claude="/opt/claude dir/claude")
    assert script.read_text().splitlines()[-1] == "exec '/opt/claude dir/claude' \"$@\" --max-budget-usd 0.25"
    with pytest.raises(runner.EvalError):
        runner.install_claude(tmp_path, mode="real", max_budget_usd=0.25, real_claude=None)


# --- Fake mode: the task set through a real server -----------------------------------------------------


_MISBEHAVING = EvalTask(
    id="misbehaving",
    requirement="Create done.txt holding done.",
    check=lambda work_dir: None if (work_dir / "done.txt").is_file() else "done.txt is missing",
    fake={
        "plan": [{"id": "s1", "instruction": "Create done.txt.", "acceptance_criteria": ["done.txt exists"],
                  "depends_on": [], "review": False}],
        "break_first_plan": 1,
        "planner_writes": {"planner-notes.txt": "the Planner wrote this\n"},
        "attempts": {"s1": [{"raw": "Finished, no report."},
                            {"status": "completed", "files": {"done.txt": "done\n"}}]},
    },
)


@pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")
def test_fake_mode_runs_every_task_and_sees_misbehaviour(tmp_path: Path) -> None:
    result, path = runner.run(mode="fake", tasks=(*TASKS, _MISBEHAVING), admin_url=str(DSN), out_dir=tmp_path,
                              agent="claude-code/opus")
    by_id = {task["id"]: task for task in result["tasks"]}
    assert result["agent"] == "claude-code/opus"  # every role ran the agent named

    for task in TASKS:
        assert by_id[task.id]["success"], (task.id, by_id[task.id]["problems"])
        assert by_id[task.id]["contract_violations"]["total"] == 0
        assert by_id[task.id]["planner_modified_files"] == 0
    flow = {t: (by_id[t]["steps"], by_id[t]["retries"], by_id[t]["reviews"], by_id[t]["chat_turns"])
            for t in by_id}
    assert flow == {"single_step": (1, 0, 0, 0), "dependent_steps": (2, 0, 0, 0), "reviewed_step": (1, 0, 1, 0),
                    "retry_after_failure": (1, 1, 0, 0), "chat_correction": (1, 0, 0, 1),
                    "misbehaving": (1, 1, 0, 0)}
    assert by_id["chat_correction"]["planner_turns"]["chat"] == 1
    assert all(task["cost_usd"] > 0 for task in result["tasks"])

    bad = by_id["misbehaving"]
    assert bad["contract_violations"] == {"planner": 1, "worker": 1, "reviewer": 0, "total": 2}
    assert bad["planner_edit_tool_paths"] == ["planner-notes.txt"]
    assert bad["planner_changed_files"] == ["planner-notes.txt"]
    assert bad["planner_modified_files"] == 1

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == result and written["schema"] == runner.SCHEMA
    assert str(Path.home()) not in path.read_text(encoding="utf-8")
    assert str(tmp_path) not in path.read_text(encoding="utf-8")
