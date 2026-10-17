"""The prompt regression eval (``evals/prompt_regression``): its own contract, and the task set in fake mode.

The fake-mode run needs PostgreSQL: it runs only when LOOPER_TEST_DATABASE_URL
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
from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError
from looper.agents.tiers.planner_protocol import TurnKind, parse_plan
from looper.agents.tiers.worker import judge_result

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


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


def test_flow_problems_check_the_least_and_the_most() -> None:
    expect = {"min_steps": 2, "max_plan_changes": 0}
    assert metrics.flow_problems(expect, {"steps": 2, "plan_changes": 0}) == []
    assert metrics.flow_problems(expect, {"steps": 1, "plan_changes": 1}) == ["steps 1 < 2", "plan_changes 1 > 0"]


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

    patch = {"event": {"type": "assistant", "message": {"content": [  # a Codex patch, naming each file it changes
        {"type": "tool_use", "name": "apply_patch", "input": {"changes": [
            {"path": str(tmp_path / "e.txt"), "kind": "add"}, {"path": str(tmp_path / "f.txt"), "kind": "update"}]}}]}}}
    def omp(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:  # OMP's tools, by their own arguments
        return {"event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": tool_input}]}}}

    events = [tool("Write", str(tmp_path / "a.txt")), tool("Read", str(tmp_path / "b.txt")),
              tool("Edit", "sub/c.txt"), tool("Write", "/elsewhere/secret/d.txt"), patch,
              omp("write", {"path": "g.txt", "content": "x"}), omp("write", {"path": "agent://Helper"}),
              omp("edit", {"input": "[h.txt#A1B2]\nPUT 1:\n+x\n"}), omp("read", {"path": "i.txt"})]
    assert metrics.edit_tool_paths(events, tmp_path) == [
        "a.txt", "sub/c.txt", "<outside the working directory>/d.txt", "e.txt", "f.txt", "g.txt", "h.txt"]


def test_changed_files_are_the_planners_only_while_no_step_ran() -> None:
    windows = {"planner": [(100.0, 110.0), (130.0, 140.0)], "worker": [(110.05, 129.0)],
               "reviewer": [(141.0, 150.0)]}
    # Written as a Planner turn starts, which the measured clock offset may put up to the tolerance before it.
    changes = {"in-plan-turn.txt": 105.0, "at-plan-turn-start.txt": 129.95, "before-plan-turn.txt": 129.85,
               "by-worker.txt": 120.0, "near-worker-start.txt": 109.98, "after-all.txt": 160.0}
    assert metrics.planner_changed_files(changes, windows) == ["at-plan-turn-start.txt", "in-plan-turn.txt"]
    # The database clock runs 50 s ahead of the local one: the windows move back by 50 s.
    shifted = {name: [(a + 50, b + 50) for a, b in spans] for name, spans in windows.items()}
    assert metrics.planner_changed_files(changes, shifted, clock_offset=50.0) == [
        "at-plan-turn-start.txt", "in-plan-turn.txt"]


@pytest.mark.parametrize(("depends_on", "pairs"), [
    ({"s1": [], "s2": [], "s3": ["s1", "s2"], "s4": ["s3"]}, 1),  # s1 and s2 only
    ({"s1": [], "s2": ["s1"], "s3": ["s2"]}, 0),  # a chain: s3 needs s1 through s2
    ({"s1": [], "s2": [], "s3": []}, 3),
], ids=["diamond", "chain", "flat"])
def test_independent_pairs_count_steps_with_no_dependency_path(depends_on: dict[str, list[str]], pairs: int) -> None:
    steps = [{"id": step_id, "depends_on": needs} for step_id, needs in depends_on.items()]
    assert metrics.independent_pairs(steps) == pairs


@pytest.mark.parametrize(("depends_on", "pairs"), [
    ({"api": [], "server": ["api"], "client": ["api"], "check": ["server", "client"]}, 1),
    ({"api": [], "server": ["api"], "client": ["api"]}, 0),  # no step brings the two together
    ({"server": [], "client": [], "check": ["server", "client"]}, 0),  # no interface they share
], ids=["interface first", "no integration", "no interface"])
def test_bracketed_pairs_need_a_shared_step_before_and_a_step_after(depends_on: dict[str, list[str]],
                                                                    pairs: int) -> None:
    steps = [{"id": step_id, "depends_on": needs} for step_id, needs in depends_on.items()]
    assert metrics.bracketed_pairs(steps) == pairs


def test_a_final_summary_needs_every_field_heading() -> None:
    full = "## Delivered\na\n\n## How to use\nb\n\n## Verification\nc\n\n## Open issues\nnone"
    assert metrics.final_summary({"result_summary": full}) == 1
    assert metrics.final_summary({"result_summary": full.replace("## Verification\nc\n\n", "")}) == 0
    assert metrics.final_summary({"result_summary": "calc is built"}) == 0


def test_most_at_once_counts_overlapping_runs_but_not_touching_ones() -> None:
    assert metrics.most_at_once([(0.0, 10.0), (5.0, 12.0), (11.0, 20.0)]) == 2
    assert metrics.most_at_once([(0.0, 10.0), (10.0, 20.0)]) == 1
    assert metrics.most_at_once([]) == 0


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
    records[2].update(input_tokens=100, output_tokens=20, cache_read_tokens=3000, cache_creation_tokens=None)
    records[4].update(input_tokens=50, output_tokens=5, cache_read_tokens=None, cache_creation_tokens=400)
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
    # Tokens are summed whether or not a run's cost is known.
    assert result["tokens"] == {"input": 150, "output": 25, "cache_read": 3000, "cache_creation": 400}


# --- Comparison -----------------------------------------------------------------------------------------


def _run(**tasks: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "real", "model": "haiku", "git": {"commit": "abcdef1234567", "dirty": False},
            "tasks": [{"id": task_id, **values} for task_id, values in tasks.items()]}


_OK = {"success": True, "steps": 1, "retries": 0, "reviews": 0, "contract_violations": {"total": 0},
       "planner_modified_files": 0, "cost_usd": 0.02}


def test_comparison_marks_what_changed_and_sums_the_run() -> None:
    base = _run(single_step=_OK, chat_correction=_OK)
    tokens = {"input": 1200, "output": 2500, "cache_read": 30000, "cache_creation": 800}
    new = _run(single_step={**_OK, "cost_usd": 0.03, "tokens": tokens},
               chat_correction={**_OK, "success": False, "contract_violations": {"total": 2},
                                "problems": ["color.txt holds 'red', expected 'blue'"]})
    new["git"]["dirty"] = True
    table = compare.markdown(new, base)
    lines = table.splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 → real/haiku @ abcdef123+local changes"
    # A run from before tokens were read has none: only the new side shows them.
    assert "| single_step | pass | 1 | – | 0 | 0 | 0 | 0 | 32k / 2.5k | 0.0200 → **0.0300** |" in lines
    assert "| chat_correction | pass → **FAIL** | 1 | – | 0 | 0 | 0 → **2** | 0 | – | 0.0200 |" in lines
    assert ("| **total** | 2/2 → **1/2** | 2 | – | 0 | 0 | 0 → **2** | 0 | – → **32k / 2.5k** | 0.0400 → **0.0500** |"
            in lines)
    assert "- chat_correction: color.txt holds 'red', expected 'blue'" in lines


def test_comparison_without_a_base_and_with_skipped_tasks() -> None:
    new = _run(single_step=_OK, chat_correction={"skipped": "the spending cap was reached", "cost_usd": 0.0})
    new.update(stopped_by_cap=True, caps={"total_cap_usd": 0.8})
    lines = compare.markdown(new).splitlines()
    assert lines[0].endswith("(no earlier run to compare with)")
    assert ("| chat_correction | skipped | skipped | skipped | skipped | skipped | skipped | skipped | skipped "
            "| skipped |" in lines)
    assert "| **total** | 1/1 | 1 | – | 0 | 0 | 0 | 0 | – | 0.0200 |" in lines
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


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
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
    assert flow == {"single_step": (1, 0, 0, 0), "dependent_steps": (2, 0, 0, 0), "many_steps": (9, 0, 0, 0),
                    "parallel_parts": (3, 0, 0, 0),
                    "interface_first": (4, 0, 0, 0), "reviewed_step": (1, 0, 1, 0),
                    "retry_after_failure": (1, 1, 0, 0), "chat_correction": (1, 0, 0, 1), "misbehaving": (1, 1, 0, 0),
                    # The change to the second step replaced it: s1, the dropped s2, and its replacement.
                    "supplement_replan": (3, 0, 0, 1), "requirement_edit": (3, 0, 0, 1),
                    "supplement_no_change": (2, 0, 0, 1), "profile_choice": (1, 0, 0, 0),
                    # The security review role reviews its step after the built-in Reviewer.
                    "roles_tester_docs": (3, 0, 0, 0), "roles_security_review": (1, 0, 2, 0),
                    "roles_advisory_review": (1, 0, 2, 0), "roles_not_needed": (1, 0, 0, 0)}
    # Only the parts that need nothing of each other run side by side; interface_first's sit between its interface
    # step and its integration step.
    shape = {t: (by_id[t]["independent_pairs"], by_id[t]["bracketed_pairs"], by_id[t]["parallel_workers"])
             for t in by_id}
    assert shape == {"single_step": (0, 0, 1), "dependent_steps": (0, 0, 1), "many_steps": (0, 0, 1),
                     "parallel_parts": (1, 0, 2),
                     "interface_first": (1, 1, 2), "reviewed_step": (0, 0, 1), "retry_after_failure": (0, 0, 1),
                     "chat_correction": (0, 0, 1), "misbehaving": (0, 0, 1),
                     # The dropped second step and its replacement both depend on s1 alone.
                     "supplement_replan": (1, 0, 1), "requirement_edit": (1, 0, 1),
                     "supplement_no_change": (0, 0, 1), "profile_choice": (0, 0, 1),
                     "roles_tester_docs": (1, 0, 2), "roles_security_review": (0, 0, 1),
                     "roles_advisory_review": (0, 0, 1), "roles_not_needed": (0, 0, 1)}
    assert by_id["chat_correction"]["planner_turns"]["chat"] == 1
    # Only the task whose project lets its Planner choose a profile runs a Worker on the one it chose.
    assert {t: by_id[t]["planner_profile_runs"] for t in by_id if by_id[t]["planner_profile_runs"]} == {
        "profile_choice": 1}
    # Only the tasks whose Planner defined roles run steps as them: each run on a version it defined, with the
    # boundaries before its contract, and the reviewed step accepted only after both reviews passed.
    # An advisory review role's failing review does not keep its step from being accepted.
    roles = {t: tuple(by_id[t][k] for k in ("roles_defined", "role_runs", "role_profile_runs", "role_reviews",
                                             "role_prompt_mismatches", "role_boundary_misses", "gate_breaches",
                                             "advisory_role_fails"))
             for t in by_id if by_id[t]["roles_defined"] or by_id[t]["role_runs"]}
    assert roles == {"roles_tester_docs": (2, 2, 1, 0, 0, 0, 0, 0), "roles_security_review": (1, 1, 0, 1, 0, 0, 0, 0),
                     "roles_advisory_review": (1, 1, 0, 1, 0, 0, 0, 1)}
    # Only the two changes that make the second step wrong change the plan.
    assert {t: by_id[t]["plan_changes"] for t in by_id if by_id[t]["plan_changes"]} == {
        "supplement_replan": 1, "requirement_edit": 1}
    # The fake Planner's decisions carry progress notes, and its completing accept the final summary.
    assert by_id["dependent_steps"]["progress_notes"] == 2 and by_id["dependent_steps"]["final_summary"] == 1
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


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_the_managers_dialogs_pass_in_fake_mode_and_a_wrong_call_fails(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    # The stand-in creates a task though the user named no project: the check sees it.
    eager = dialogs.Dialog("eager", (dialogs.Say("帮我写个脚本。", dialogs._asked_for_the_project, (dialogs._CREATE,)),))
    result, path = dialogs.run(mode="fake", dialogs=(*dialogs.DIALOGS, eager), admin_url=str(DSN), out_dir=tmp_path,
                               agent="claude-code/opus")
    by_id = {dialog["id"]: dialog for dialog in result["dialogs"]}
    for dialog in dialogs.DIALOGS:
        assert by_id[dialog.id]["success"], (dialog.id, by_id[dialog.id]["problems"])
    assert [turn["calls"] for turn in by_id["cancel_confirmation"]["turns"]] == [
        ["looper_task_control:needs_confirmation"], [], ["looper_task_control:needs_confirmation"],
        ["looper_task_control:ok"]]
    # The key the user pasted: the call passing it is refused, the one without it registers the provider.
    assert by_id["provider_key"]["turns"][0]["calls"] == ["looper_providers:error (forbidden)", "looper_providers:ok"]
    assert by_id["config_tools"]["turns"][-2]["calls"] == ["looper_projects:needs_confirmation"]
    assert [turn["calls"] for turn in by_id["schedules"]["turns"]] == [
        ["looper_schedules:ok"], ["looper_schedules:ok"], ["looper_schedules:needs_confirmation"],
        ["looper_schedules:ok"]]
    assert by_id["eager"]["problems"] == ["a task was created though the user named no project"]
    assert "| cancel_confirmation | pass | 4 |" in dialogs.markdown(result)
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("reply", "raw"), [
    ("任务已创建，还没有开始（Ready — not started）：\n- 任务：hello", False),
    ('好的。{"action": "task_start", "task_id": "task-1"}', True),
    ('```json\n{"status": "ok"}\n```', True),
    ('[{"status": "ok", "result": {}}]', True),
])
def test_a_dialog_reply_must_not_show_raw_json(reply: str, raw: bool) -> None:
    from evals.prompt_regression.dialogs import _raw_json

    assert (_raw_json(reply) is not None) is raw
