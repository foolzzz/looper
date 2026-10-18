"""The prompt regression eval (``evals/prompt_regression``): its own contract, and the task set in fake mode.

The fake-mode run needs PostgreSQL: it runs only when LOOPER_TEST_DATABASE_URL
points at a server where it may create (and then drop) a database of its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import compare, metrics, runner
from evals.prompt_regression import tasks as task_list
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


@pytest.mark.parametrize(("steps", "dependents"), [
    # An interface step every platform step (each as its role) depends on, and an integration after them.
    ({"api": ([], None), "be": (["api"], "backend_rd"), "web": (["api"], "fe_rd"), "and": (["api"], "android_rd"),
      "check": (["be", "web", "and"], None)}, 3),
    # The back end pins the interface itself: the clients depend on it, the back end on nothing.
    ({"be": ([], "backend_rd"), "web": (["be"], "fe_rd"), "and": (["be"], "android_rd")}, 2),
    ({"be": ([], "backend_rd"), "web": ([], "fe_rd")}, 0),  # nothing shared
    ({"api": ([], None), "be": (["api"], None), "web": (["api"], None)}, 0),  # no step runs as a role
], ids=["interface step", "back end first", "flat", "no roles"])
def test_interface_dependents_count_the_role_steps_built_on_one_step(steps: dict[str, Any], dependents: int) -> None:
    rows = [{"id": step_id, "depends_on": needs, "role_key": role, "status": "verified"}
            for step_id, (needs, role) in steps.items()]
    assert metrics.interface_dependents(rows) == dependents
    assert metrics.interface_dependents([{**row, "status": "skipped"} for row in rows]) == 0


def test_a_discipline_counts_once_it_has_a_role_of_its_own_that_names_its_conventions() -> None:
    web = metrics.Discipline("web", ("web", "fe"), ("fetch", "dom", "es module"))
    ios = metrics.Discipline("ios", ("ios",), ("swift", "codable", "struct"))

    def role(role_id: str, prompt: str, kind: str = "work") -> dict[str, str]:
        return {"id": role_id, "kind": kind, "role_prompt": prompt}

    strong = [role("fe_rd", "An ES module that uses fetch."), role("ios_rd", "Swift: a Codable struct.")]
    assert metrics.discipline_roles(strong, (web, ios)) == 2
    assert metrics.discipline_roles([role("fe_rd", "Write the web client.")], (web, ios)) == 0  # no convention
    assert metrics.discipline_roles([role("fe_rd", "Uses fetch.")], (web,)) == 0  # one is not enough
    assert metrics.discipline_roles([role("fe_ios_rd", "fetch, the DOM; Swift, Codable")], (web, ios)) == 0
    assert metrics.discipline_roles([role("fe_review", "fetch and the DOM", "review")], (web,)) == 0
    assert metrics.discipline_roles(strong, ()) == 0


def test_the_multi_platform_check_reads_the_interface_the_parts_must_agree_on(tmp_path: Path) -> None:
    from evals.prompt_regression.tasks import TASKS

    task = next(t for t in TASKS if t.id == "roles_multi_platform")
    for name, content in {**task.files, **{n: c for a in task.fake["attempts"].values() for n, c in a[0]["files"].items()}
                          }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content, encoding="utf-8")
    assert task.check(tmp_path) is None
    interface = tmp_path / "api" / "tip_api.json"
    interface.write_text('{"path": "/api/tip", "query": "day", "fields": {"day": "int", "tip": "string"}}')
    assert task.check(tmp_path) is None  # the fields with their types
    # A contract without its path, its query parameter or its fields fails, in either form of the fields.
    lacking = "api/tip_api.json does not give a path, a query parameter and the response fields"
    for fields in (["day", "tip"], {"day": "int", "tip": "string"}):
        for drop in ("path", "query", "fields"):
            contract = {key: value for key, value in {"path": "/api/tip", "query": "day", "fields": fields}.items()
                        if key != drop}
            interface.write_text(json.dumps(contract))
            assert task.check(tmp_path) == lacking, (fields, drop)
        for empty in ("path", "query", "fields"):
            interface.write_text(json.dumps({"path": "/api/tip", "query": "day", "fields": fields,
                                             empty: type({"path": "", "query": "", "fields": fields}[empty])()}))
            assert task.check(tmp_path) == lacking, (fields, empty)
    interface.write_text('{"path": "/api/tip", "query": "day", "fields": ["day", "tip", "author"]}')
    assert task.check(tmp_path) == "handle(/api/tip?day=3) answered 200 with ['day', 'tip']"
    interface.write_text('{"path": "/api/tip", "query": "d", "fields": ["day", "tip"]}')
    assert "answered 404" in task.check(tmp_path)


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
    records[2].update(input_tokens=100, output_tokens=20, cache_read_tokens=3000, cache_creation_tokens=None,
                      status="failed", model="claude-haiku")
    records[4].update(input_tokens=50, output_tokens=5, cache_read_tokens=None, cache_creation_tokens=400,
                      model="claude-haiku")
    events = {3: _result("no report"), 5: _result(good), 7: _result('{"passed": true}')}
    detail = {"status": "completed", "steps": [{"id": "step-1"}],
              "cost": {"cost_usd": 0.0123, "runs": 7, "by_role": {"planner": {"cost_usd": 0.004, "runs": 4}}}}
    activity = [{"kind": "error", "summary": "Planner reply broke the PLAN contract (the reply has no ```json "
                                             "fenced block); asking again"}]
    result = metrics.task_metrics(
        detail=detail, activity=activity, records=records, events=events, changes={}, work_dir=tmp_path,
        check_problem=None, expect={"min_retries": 1, "min_reviews": 2, "max_contract_violations": 1}, now=0.0,
    )
    assert result["success"] is False
    assert result["problems"] == ["reviews 1 < 2", "contract_violations 2 > 1"]
    assert (result["steps"], result["retries"], result["reviews"], result["chat_turns"]) == (1, 1, 1, 1)
    assert result["planner_turns"] == {"plan": 1, "repair": 1, "decision": 1, "chat": 1}
    assert result["contract_violations"] == {"planner": 1, "worker": 1, "reviewer": 0, "total": 2}
    assert (result["cost_usd"], result["cost_by_role"], result["planner_modified_files"]) == (
        0.0123, {"planner": 0.004}, 0)
    # Tokens are summed whether or not a run's cost is known.
    assert result["tokens"] == {"input": 150, "output": 25, "cache_read": 3000, "cache_creation": 400}
    # The runs that ended in an error, by their record's status, and the models each role's runs reported.
    assert result["run_errors"] == 1
    assert result["models"] == {"planner": [], "worker": ["claude-haiku"], "reviewer": []}


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
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123+local changes (1 rep)"
    # A run from before tokens were read has none: only the new side shows them.
    # A run from before gates were counted has none either.
    assert ("| single_step | 1/1 | 1.0 | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – → **32k / 2.5k** | 0.0200 → **0.0300** |"
            in lines)
    assert "| chat_correction | 1/1 → **0/1** | 1.0 | – | 0.0 | 0.0 | – | 0.0 → **2.0** | 0.0 | – | 0.0200 |" in lines
    assert ("| **total** | 2/2 (100%, 34–100%) → **1/2 (50%, 9–91%)** | 2.0 | – | 0.0 | 0.0 | – | 0.0 → **2.0** "
            "| 0.0 | – → **32k / 2.5k** | 0.0400 → **0.0500** |" in lines)
    assert "- pass rate changed: chat_correction 1/1 → 0/1" in lines
    assert "- the base and the new run ran each case once: a pass or fail per case is a single sample" in lines
    assert "- chat_correction: color.txt holds 'red', expected 'blue'" in lines


def test_comparison_without_a_base_and_with_skipped_tasks() -> None:
    new = _run(single_step=_OK, chat_correction={"skipped": "the spending cap was reached", "cost_usd": 0.0})
    new.update(stopped_by_cap=True, caps={"total_cap_usd": 0.8})
    lines = compare.markdown(new).splitlines()
    assert lines[0].endswith("(no earlier run to compare with)")
    assert ("| chat_correction | skipped | skipped | skipped | skipped | skipped | skipped | skipped | skipped "
            "| skipped | skipped |" in lines)
    assert "| **total** | 1/1 (100%, 21–100%) | 1.0 | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    assert "- stopped at the spending cap (0.8 USD)" in lines


def _reps(task_id: str, *rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": task_id, "rep": rep, **row} for rep, row in enumerate(rows, start=1)]


_PASS = {**_OK, "outcome": "pass"}
_FAIL = {**_OK, "outcome": "fail", "success": False, "steps": 2, "cost_usd": 0.04, "problems": ["sum.txt is missing"]}
_TIMEOUT = {"outcome": "error", "success": False, "error_class": "timeout", "error": "cancelled after 900 s",
            "steps": 7, "cost_usd": 0.5}


def test_comparison_of_repeated_runs_counts_the_scored_reps_and_keeps_errors_apart() -> None:
    # A base of one rep per task (schema v1: no rep, no outcome; a task cancelled at its timeout is an error).
    base = _run(single_step=_OK, dependent_steps={**_OK, "success": False, "timed_out": True},
                chat_correction=_OK)
    new = {**_run(), "tasks": [*_reps("single_step", _PASS, _PASS, _PASS),
                               *_reps("dependent_steps", _PASS, _FAIL, _TIMEOUT),
                               *_reps("interface_first", _PASS, {"skipped": "the spending cap was reached"},
                                      {"outcome": "error", "success": False, "error_class": "harness",
                                       "error": "EvalError: GET /api/tasks/t answered 500"})]}
    lines = compare.markdown(new, base).splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123 (3 reps)"
    assert "| single_step | 1/1 → **3/3** | 1.0 | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The mean is over the scored reps: the pass and the fail, not the error.
    assert ("| dependent_steps | 0/0 · 1 err → **1/2 · 1 err** | – → **1.5** | – | – → **0.0** | – → **0.0** "
            "| – | – → **0.0** | – → **0.0** | – | – → **0.0300** |" in lines)
    assert "| interface_first | 1/1 · 1 err | 1.0 | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The total adds up the tasks' means (one pass through the set), over the tasks both runs have; the new run's
    # own total follows, as it has a task the base lacks.
    assert ("| **total (2 shared tasks)** | 1/1 (100%, 21–100%) · 1 timeout → **4/5 (80%, 38–96%) · 1 timeout** "
            "| 1.0 → **2.5** | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – | 0.0200 → **0.0500** |" in lines)
    assert ("| **total (all 3 tasks of the new run)** | 5/6 (83%, 44–97%) · 1 harness, 1 timeout | 3.5 | – | 0.0 "
            "| 0.0 | – | 0.0 | 0.0 | – | 0.0700 |" in lines)
    assert "- pass rate changed: dependent_steps 0/0 → 1/2" in lines
    assert "- the base ran each case once: a pass or fail per case is a single sample" in lines
    assert "- no baseline: interface_first" in lines
    assert "- in the base, not in the new run: chat_correction" in lines
    assert "- dependent_steps (rep 2): sum.txt is missing" in lines
    assert "- dependent_steps (rep 3): error (timeout): cancelled after 900 s" in lines
    assert "- interface_first (rep 3): error (harness): EvalError: GET /api/tasks/t answered 500" in lines


def test_against_a_base_of_more_tasks_the_total_is_over_the_shared_ones() -> None:
    # A base of seven tasks, six passed; the new run has one of them, twice.
    base = _run(**{f"task_{i}": _OK if i < 6 else {**_OK, "success": False} for i in range(7)})
    base["tasks"][0]["id"] = "single_step"
    new = {**_run(), "tasks": _reps("single_step", _PASS, {**_PASS, "cost_usd": 0.04})}
    lines = compare.markdown(new, base).splitlines()
    assert ("| **total (1 shared task)** | 1/1 (100%, 21–100%) → **2/2 (100%, 34–100%)** | 1.0 | – | 0.0 | 0.0 | – "
            "| 0.0 | 0.0 | – | 0.0200 → **0.0300** |" in lines)
    assert not any(line.startswith("| **total (all") for line in lines)  # the new run has no task of its own
    assert "- in the base, not in the new run: task_1, task_2, task_3, task_4, task_5, task_6" in lines


def test_against_a_base_of_other_tasks_there_is_no_shared_total() -> None:
    base = _run(dependent_steps=_OK, chat_correction=_OK)
    new = {**_run(), "tasks": _reps("single_step", _PASS, _PASS)}
    lines = compare.markdown(new, base).splitlines()
    assert not any(line.startswith("| **total (0 shared") for line in lines)
    assert ("| **total (all 1 task of the new run)** | 2/2 (100%, 34–100%) | 1.0 | – | 0.0 | 0.0 | – | 0.0 | 0.0 | – "
            "| 0.0200 |" in lines)
    assert "- no baseline: single_step" in lines
    assert "- in the base, not in the new run: dependent_steps, chat_correction" in lines


def test_comparison_of_dialogs_shows_success_and_cost() -> None:
    run = {"mode": "fake", "agent": "claude-code/opus", "manager_agent": "claude-code/opus",
           "git": {"commit": "abcdef1234567", "dirty": False},
           "dialogs": _reps("status", {"outcome": "pass", "success": True, "cost_usd": 0.001},
                            {"outcome": "error", "success": False, "error_class": "timeout",
                             "error": "no reply to 'hi' within 60 s", "cost_usd": 0.002})}
    old = {**run, "dialogs": [{"id": "status", "success": True, "cost_usd": 0.003}]}  # schema v1
    lines = compare.markdown(run, old).splitlines()
    assert lines[0] == "Manager dialogs: fake/claude-code/opus @ abcdef123 (1 rep) → fake/claude-code/opus @ abcdef123 (2 reps)"
    assert "| dialog | success | cost (USD) |" in lines
    assert "| status | 1/1 → **1/1 · 1 err** | 0.0030 → **0.0010** |" in lines
    assert "- status (rep 2): error (timeout): no reply to 'hi' within 60 s" in lines


@pytest.mark.parametrize(("passes", "scored", "interval"), [
    (8, 10, (0.4902, 0.9433)),
    (41, 48, (0.7283, 0.9275)),
    (0, 10, (0.0, 3.8415 / 13.8415)),  # 0 of n: from 0 to z² / (n + z²)
    (10, 10, (10 / 13.8415, 1.0)),  # n of n: from n / (n + z²) to 1
    (0, 0, None),
])
def test_the_wilson_interval(passes: int, scored: int, interval: tuple[float, float] | None) -> None:
    found = compare.wilson(passes, scored)
    assert found == pytest.approx(interval, abs=1e-4) if interval else found is None


@pytest.mark.parametrize(("row", "outcome", "error_class"), [
    ({"outcome": "pass", "success": True}, "pass", None),
    ({"outcome": "error", "success": False, "error_class": "harness"}, "error", "harness"),
    ({"success": True}, "pass", None),  # schema v1
    ({"success": False}, "fail", None),
    ({"success": False, "timed_out": True}, "error", "timeout"),
    ({"success": False, "stopped_by_cap": True}, "error", "spending_cap"),
    ({"skipped": "the spending cap was reached"}, "skipped", None),
])
def test_a_rows_outcome_is_read_from_its_typed_fields(row: dict[str, Any], outcome: str,
                                                       error_class: str | None) -> None:
    assert (compare.outcome(row), compare.error_class(row)) == (outcome, error_class)


@pytest.mark.parametrize(("rows", "aborted", "code"), [
    ([_PASS, _PASS], None, 0),
    ([_PASS, _FAIL], None, 1),
    ([_PASS, _TIMEOUT], None, 1),
    ([_PASS, {"skipped": "the spending cap was reached"}], None, 1),
    ([_PASS], "KeyboardInterrupt", 2),
])
def test_the_exit_code_is_zero_only_when_every_row_ran_and_passed(rows: list[dict[str, Any]], aborted: str | None,
                                                                  code: int) -> None:
    assert cli.exit_code({"tasks": [{"id": "t", **row} for row in rows], "aborted": aborted}) == code


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_reps_must_be_a_whole_number_of_at_least_one(value: str) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["run", "--reps", value])
    assert cli._parser().parse_args(["dialogs", "--reps", "3"]).reps == 3


def test_a_task_set_is_resolved_against_the_tasks_there_are(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = [task.id for task in runner.select_tasks(["planner"])]
    assert planner == [task.id for task in TASKS if task.id in runner.TASK_SETS["planner"]]
    assert "dependent_steps" in planner and "single_step" not in planner
    # Mixed with ids, in the task set's order, each once.
    mixed = [task.id for task in runner.select_tasks(["planner", "single_step", "dependent_steps"])]
    assert mixed == ["single_step", *planner]
    # A task the set names joins it once it exists.
    later = EvalTask(id="roles_multi_platform", requirement="r", check=lambda _work_dir: None)
    monkeypatch.setattr(task_list, "TASKS", (*TASKS, later))
    assert [task.id for task in runner.select_tasks(["planner"])] == [*planner, "roles_multi_platform"]
    with pytest.raises(ValueError, match="unknown task id"):
        runner.select_tasks(["planner", "no_such_task"])
    monkeypatch.setitem(runner.TASK_SETS, "empty", ("no_such_task",))
    with pytest.raises(ValueError, match="none of its tasks"):
        runner.select_tasks(["empty"])


class _Poster:
    """The API as ``answer_gates`` uses it: what it posted."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, Any]] = []

    def post(self, path: str, body: Any) -> dict[str, Any]:
        self.posted.append((path, body))
        return {}


def test_the_scripted_user_ends_a_gate_that_asks_again_after_its_script_ran_out() -> None:
    task = next(t for t in TASKS if t.id == "gate_ambiguous")
    api, answered = _Poster(), {}
    waiting = {"gates": [{"step": "g1", "waiting_on": "user", "opened_at": None}]}
    for _ in range(4):
        runner.answer_gates(api, task, "t1", waiting, answered, now=0.0)
    assert api.posted == [("/api/tasks/t1/gates/g1/messages", {"text": task.gate_script[0]}),
                          ("/api/tasks/t1/gates/g1/end", {})]
    # A script that ends the discussion itself gets no second end.
    ends = next(t for t in TASKS if t.id == "gate_end_early")
    api, answered = _Poster(), {}
    for _ in range(3):
        runner.answer_gates(api, ends, "t1", waiting, answered, now=0.0)
    assert api.posted == [("/api/tasks/t1/gates/g1/end", {})]
    # Nothing while the gate waits for the Planner.
    api = _Poster()
    runner.answer_gates(api, task, "t1", {"gates": [{"step": "g1", "waiting_on": "planner"}]}, {}, now=0.0)
    assert api.posted == []


def test_a_gate_that_asks_after_its_script_ran_out_fails_a_task_that_bounds_it(tmp_path: Path) -> None:
    task = next(t for t in TASKS if t.id == "gate_ambiguous")
    assert task.expect["max_gate_asks_beyond_script"] == 0

    def result(asks: int) -> dict[str, Any]:
        return metrics.task_metrics(detail={"status": "completed", "steps": []}, activity=[], records=[], events={},
                                    changes={}, work_dir=tmp_path, check_problem=None,
                                    expect={"max_gate_asks_beyond_script": 0}, gate_asks_beyond_script=asks, now=0.0)

    assert result(0)["success"] and result(0)["gate_asks_beyond_script"] == 0
    assert result(1)["problems"] == ["gate_asks_beyond_script 1 > 0"]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_gate_task_has_gates_on_whatever_user_gates_says(tmp_path: Path) -> None:
    tasks = runner.select_tasks(["gate_ambiguous"])
    assert {"gate_ambiguous", "gate_not_needed", "gate_parallel_branch", "gate_discoverable"} <= \
        set(runner.TASK_SETS["planner"])
    result, _ = runner.run(mode="fake", tasks=tasks, admin_url=str(DSN), out_dir=tmp_path, user_gates="off")
    (row,) = result["tasks"]
    assert row["outcome"] == "pass" and row["gates_opened"] == 1, row["problems"]


def test_the_base_is_the_committed_baseline_unless_another_is_named(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    baselines, runs = tmp_path / "baselines", tmp_path / "runs"
    baselines.mkdir()
    runs.mkdir()
    monkeypatch.setattr(runner, "BASELINES_DIR", baselines)

    def write(path: Path, **data: Any) -> Path:
        path.write_text(json.dumps({"mode": "real", **data}), encoding="utf-8")
        return path

    committed = write(baselines / "real-haiku.json", model="haiku", tasks=[], git={"commit": "c0ffee"})
    write(baselines / "dialogs-real-opus.json", model="haiku", manager_model="opus", dialogs=[])
    earlier = write(runs / "20261016T100000Z-real.json", model="haiku", tasks=[])
    write(runs / "20261016T110000Z-dialogs-real.json", model="haiku", dialogs=[])  # newer, but of the dialogs
    write(runs / "20261016T120000Z-real.json", model="opus", tasks=[])  # newer, but of another model
    current = runs / "20261016T130000Z-real.json"

    def base(choice: str | None, **kwargs: Any) -> tuple[Any, str]:
        return runner.comparison_base(choice, out_dir=runs, current=current, mode="real", **kwargs)

    assert base(None, model="haiku") == (json.loads(committed.read_text()), committed.name)
    assert base("previous", model="haiku") == (json.loads(earlier.read_text()), earlier.name)
    assert base(str(earlier), model="opus")[0] == json.loads(earlier.read_text())
    assert base(None, model="opus") == (None, "no committed baseline real-opus.json")
    assert base("previous", model="sonnet")[0] is None
    # Dialogs are compared by the Manager's model.
    assert base(None, model="opus", dialogs=True)[1] == "dialogs-real-opus.json"
    assert base("previous", model="haiku", dialogs=True)[1] == "20261016T110000Z-dialogs-real.json"


# --- The spending cap -----------------------------------------------------------------------------------


def _task(task_id: str) -> EvalTask:
    return EvalTask(id=task_id, requirement="r", check=lambda _work_dir: None)


def test_the_run_stops_at_the_total_cap() -> None:
    seen: list[tuple[str, int, float | None]] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        seen.append((task.id, rep, cap_left))
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.3}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=0.5)
    assert [(t, r, round(c, 2)) for t, r, c in seen] == [("a", 1, 0.5), ("b", 1, 0.2)]
    assert stopped is True
    assert [r.get("skipped") for r in results] == [None, None, "the spending cap was reached",
                                                    "the spending cap was reached"]
    _, stopped = runner.run_suite(tuple(_task(t) for t in "ab"), run_one, total_cap_usd=None)
    assert stopped is False
    # The cap covers every rep: rep 1 of each task, then rep 2, until it is reached.
    seen.clear()
    results, stopped = runner.run_suite(tuple(_task(t) for t in "ab"), run_one, total_cap_usd=1.0, reps=3)
    assert [(t, r) for t, r, _ in seen] == [("a", 1), ("b", 1), ("a", 2), ("b", 2)]
    assert [(r["id"], r["rep"], bool(r.get("skipped"))) for r in results] == [
        ("a", 1, False), ("b", 1, False), ("a", 2, False), ("b", 2, False), ("a", 3, True), ("b", 3, True)]


def test_a_case_that_raises_is_a_harness_error_and_the_run_goes_on() -> None:
    def run_one(task: EvalTask, rep: int, _cap_left: float | None) -> dict[str, Any]:
        if (task.id, rep) == ("a", 1):
            raise runner.TaskError("EvalError: GET /api/tasks/t answered 500", cost_usd=0.4)
        if (task.id, rep) == ("b", 1):
            raise ConnectionRefusedError("the server is gone")
        return {"id": task.id, "outcome": "pass", "success": True, "cost_usd": 0.1}

    results, stopped = runner.run_suite((_task("a"), _task("b")), run_one, total_cap_usd=0.5, reps=2)
    assert [(r["id"], r["rep"], r.get("outcome"), r.get("error_class"), r["cost_usd"]) for r in results] == [
        ("a", 1, "error", "harness", 0.4), ("b", 1, "error", "harness", 0.0), ("a", 2, "pass", None, 0.1),
        ("b", 2, None, None, 0.0)]  # the error's cost counts toward the cap
    assert results[1]["error"] == "ConnectionRefusedError: the server is gone" and results[1]["success"] is False
    assert results[3]["skipped"] and stopped is True


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
    # Cancelled at the cap: an error, not the model's failure.
    assert (result["outcome"], result["error_class"], result["error"]) == (
        "error", "spending_cap", "cancelled at the spending cap")
    assert "cancelled at the spending cap" in result["problems"]
    assert result["cost_usd"] == pytest.approx(0.3)


def test_a_task_the_harness_cannot_follow_is_cancelled_with_its_cost(tmp_path: Path) -> None:
    class _Failing(_CostlyApi):
        def get(self, path: str, **query: Any) -> Any:
            if path == "/api/llm-records":
                raise runner.EvalError("GET /api/llm-records answered 500: boom")
            return super().get(path, **query)

    api = _Failing()
    api.cancelled = True  # the task ended: the harness fails reading it back
    with pytest.raises(runner.TaskError) as raised:
        runner.run_task(api, _task("a"), tmp_path / "work", timeout=60, poll=0)  # type: ignore[arg-type]
    assert str(raised.value) == "EvalError: GET /api/llm-records answered 500: boom"
    assert raised.value.cost_usd == pytest.approx(0.0)
    assert [path for path, _ in api.posts] == ["/api/tasks/create", "/api/tasks/start", "/api/tasks/cancel"]


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
                    "roles_advisory_review": (1, 0, 2, 0), "roles_two_reviews": (1, 0, 3, 0),
                    "roles_multi_platform": (6, 0, 0, 0),
                    "roles_not_needed": (1, 0, 0, 0), "roles_template_fits": (1, 0, 2, 0),
                    "roles_template_unfit": (1, 0, 0, 0),
                    # A gate is a step; the steps its closing turn added follow it.
                    "gate_ambiguous": (2, 0, 0, 0), "gate_not_needed": (1, 0, 0, 0), "gate_end_early": (2, 0, 0, 0),
                    "gate_parallel_branch": (3, 0, 0, 0), "gate_discoverable": (2, 0, 0, 0)}
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
                     "roles_advisory_review": (0, 0, 1), "roles_two_reviews": (0, 0, 1), "roles_not_needed": (0, 0, 1),
                     "roles_template_fits": (0, 0, 1), "roles_template_unfit": (0, 0, 1),
                     # Its four platform steps run side by side between the interface step and the check.
                     "roles_multi_platform": (6, 6, 4),
                     "gate_ambiguous": (0, 0, 1), "gate_not_needed": (0, 0, 1), "gate_end_early": (0, 0, 1),
                     # The clear part runs beside the gate and then beside the step the gate's answer planned.
                     "gate_parallel_branch": (2, 0, 2), "gate_discoverable": (0, 0, 1)}
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
                     "roles_advisory_review": (1, 1, 0, 1, 0, 0, 0, 1),
                     "roles_two_reviews": (2, 2, 0, 2, 0, 0, 0, 0),
                     "roles_multi_platform": (4, 4, 0, 0, 0, 0, 0, 0),
                     "roles_template_fits": (1, 1, 0, 1, 0, 0, 0, 0)}
    # Only the two-reviews task names more than one review role on a step.
    assert {t: by_id[t]["most_review_roles"] for t in by_id if by_id[t]["most_review_roles"]} == {
        "roles_security_review": 1, "roles_advisory_review": 1, "roles_template_fits": 1, "roles_two_reviews": 2}
    # Only the task whose step fits a project role template copies it.
    assert {t: by_id[t]["template_roles"] for t in by_id if by_id[t]["template_roles"]} == {"roles_template_fits": 1}
    # Only the multi-platform task's disciplines each get a role that names their conventions, and its four
    # platform steps are built on its interface step (the tester's and the doc writer's on the module).
    assert {t: by_id[t]["discipline_roles"] for t in by_id if by_id[t]["discipline_roles"]} == {
        "roles_multi_platform": 4}
    assert {t: by_id[t]["interface_dependents"] for t in by_id if by_id[t]["interface_dependents"]} == {
        "roles_tester_docs": 2, "roles_multi_platform": 4}
    # Only the gate tasks open gates. The scripted user answers (or ends the discussion), the gate passes on what was
    # settled (or assumed), and the step it planned reads the conclusion; the step that finds the port cancels the
    # gate that waits on it before it opens, so no gate asked.
    gates = {t: tuple(by_id[t][k] for k in ("gates_opened", "gate_turns", "gates_passed", "gates_ended_by_user",
                                             "gates_cancelled", "assumptions_stated", "gate_parallel_runs",
                                             "conclusion_reads"))
             for t in by_id if by_id[t]["gates_opened"]}
    assert gates == {"gate_ambiguous": (1, 1, 1, 0, 0, 0, 0, 1), "gate_end_early": (1, 1, 1, 1, 0, 1, 0, 1),
                     "gate_parallel_branch": (1, 1, 1, 0, 0, 0, 1, 1)}
    assert by_id["gate_discoverable"]["gates_opened"] == 0
    assert all(by_id[t]["gate_asks_beyond_script"] == 0 for t in by_id)
    assert by_id["gate_ambiguous"]["planner_turns"]["gate"] == 1
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
    assert written["aborted"] is None and {row["rep"] for row in written["tasks"]} == {1}
    assert str(Path.home()) not in path.read_text(encoding="utf-8")
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_repeats_each_task_rep_major_in_a_directory_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["run", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--tasks", "single_step,dependent_steps", "--reps", "2"])
    written = json.loads(next(tmp_path.glob("*-fake.json")).read_text(encoding="utf-8"))
    rows = written["tasks"]
    assert code == 0 and written["reps"] == 2
    assert [(r["id"], r["rep"], r["outcome"]) for r in rows] == [
        ("single_step", 1, "pass"), ("dependent_steps", 1, "pass"), ("single_step", 2, "pass"),
        ("dependent_steps", 2, "pass")]
    assert [r["work_dir"] for r in rows] == [f"<eval-root>/work/{r['id']}/{r['rep']}" for r in rows]
    shown = capsys.readouterr().out
    assert "| single_step | 2/2 | 1.0 |" in shown and "| dependent_steps | 2/2 | 2.0 |" in shown
    assert "Base: none (no committed baseline evals/prompt_regression/baselines/fake.json)" in shown


_STALLED = EvalTask(
    id="stalled",
    requirement="Create late.txt holding late.",
    check=lambda work_dir: None if (work_dir / "late.txt").is_file() else "late.txt is missing",
    fake={"plan": [{"id": "s1", "instruction": "Create late.txt.", "acceptance_criteria": ["late.txt exists"],
                    "depends_on": [], "review": False}],
          "attempts": {"s1": [{"status": "completed", "seconds": 120, "files": {"late.txt": "late\n"}}]}},
)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_task_past_its_timeout_is_an_error_not_a_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_list, "TASKS", (*TASKS, _STALLED))
    code = cli.main(["run", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--tasks", "stalled", "--task-timeout", "3"])
    written = json.loads(next(tmp_path.glob("*-fake.json")).read_text(encoding="utf-8"))
    (row,) = written["tasks"]
    assert code == 1
    assert (row["outcome"], row["error_class"], row["error"], row["success"]) == (
        "error", "timeout", "cancelled after 3 s", False)
    assert row["timed_out"] is True and row["status"] == "cancelled"


@pytest.mark.parametrize(("reply", "claims"), [
    # What real Managers answered when asked for a project's limit: explanations and offers.
    ("项目 eval-calc 的并发改不了：Looper 里并发是按任务设置的，项目本身没有并发限制。\n\n1. **只改这个项目里的任务**：把 eval-calc "
     "下各个任务的 Worker 并发分别设成 2。", False),
    ("这个改不了。\n1. **把这个任务的 Worker 上限改成 2**：马上生效，只影响这一个任务。", False),
    ("项目 eval-calc 的 Worker 并发没有改，因为 Looper 里项目本身没有并发限制。", False),
    ("Projects have no concurrency limit, so I changed nothing. I can set this task's Worker limit to 2 instead.", False),
    ("Projects have no concurrency limit, so nothing changed. Want this task's Worker limit at 2?", False),
    # Claims that it was done.
    ("已把项目 eval-calc 的 Worker 并发改为 2。", True),
    ("好的，项目 eval-calc 的 Worker 并发已经设置为 2。", True),
    ("Done: I changed the project's Worker limit to 2.", True),
    ("已把项目 eval-calc 的 Worker 并发调整为 2。", True),
    ("项目 eval-calc 的 Worker 并发已更新为 2。", True),
    ("Updated the project's Worker limit to 2.", True),
    ("Done. Set the project's Worker limit to 2.", True),
    ("Set this task's Worker limit to 2 instead?", False),
    ("Want me to update this task's Worker limit to 2?", False),
])
def test_a_reply_claiming_a_change_is_told_from_an_explanation(reply: str, claims: bool) -> None:
    from evals.prompt_regression import dialogs

    assert dialogs.claims_change(reply) is claims


@pytest.mark.parametrize(("reply", "model", "named"), [
    ("claude-haiku-4-5-20251001: $0.02", "claude-haiku-4-5-20251001", True),
    ("_HAIKU_RUNS: $0.02", "claude-haiku-4-5-20251001", True),
    ("Claude Sonnet: $0.02", "claude-haiku-4-5-20251001", False),
    ("DeepSeek: $0.02", "deepseek-flash", True),
])
def test_a_models_id_or_family_is_named_in_the_reply(reply: str, model: str, named: bool) -> None:
    from evals.prompt_regression.dialogs import _model_named

    assert _model_named(reply, model) is named


@pytest.mark.parametrize(("reply", "cost", "told"), [
    ("$0.04", 0.0409, True),
    ("0.041", 0.0409, True),
    ("$0.0409", 0.0409, True),
    ("0.04 美元", 0.0409, True),
    ("$999999", 0.0409, False),
    ("$0.05", 0.0409, False),
    ("0.1", 0.0409, False),
    ("0.0", 0.0409, False),
    ("$1,234.56", 1234.56, True),
    ("$1,000.0409", 0.0409, False),
    ("$0.04", 0.045, True),
    ("$0.04", 0.045001, False),
])
def test_an_amount_is_told_at_the_written_precision(reply: str, cost: float, told: bool) -> None:
    from evals.prompt_regression.dialogs import _amount_told

    assert _amount_told(reply, cost) is told


_HAIKU_RUNS = {"model": "claude-haiku-4-5", "runs": 2, "running": 1, "cost_usd": 0.0409}


@pytest.mark.parametrize(("task_id", "status", "by_model", "write_status", "fake", "reply", "problem"), [
    ("other", "ok", [_HAIKU_RUNS], None, False, "Haiku", "looper_costs did not read the task's cost"),
    ("task-a", "error", [_HAIKU_RUNS], None, False, "Haiku", "looper_costs did not read the task's cost"),
    ("task-a", "ok", [_HAIKU_RUNS], "ok", False, "Haiku", "something changed: looper_settings"),
    ("task-a", "ok", [_HAIKU_RUNS], "needs_confirmation", False, "Haiku", "something changed: looper_settings"),
    ("task-a", "ok", [{"model": None, "runs": 1, "running": 0, "cost_usd": 0.01}], None, False, "Haiku",
     "the task's cost has no model to name"),
    ("task-a", "ok", [_HAIKU_RUNS], None, False, "Sonnet", "the reply does not name claude-haiku-4-5"),
    ("task-a", "ok", [_HAIKU_RUNS], None, False, "Haiku $999999",
     "the reply does not give the cost of claude-haiku-4-5 (0.0409)"),
    ("task-a", "ok", [{**_HAIKU_RUNS, "cost_usd": None}], None, False, "Haiku", None),
    ("task-a", "ok", [_HAIKU_RUNS], None, True, "ok", None),
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "deepseek-flash", "runs": 1, "running": 0, "cost_usd": 0.02}],
     None, False, "Haiku $0.04 and DeepSeek $0.02", None),
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "deepseek-flash", "runs": 1, "running": 0, "cost_usd": 0.02}],
     None, False, "Haiku $0.02 and DeepSeek $0.04",
     "the reply does not give the cost of claude-haiku-4-5 (0.0409)"),
    # A model whose runs are all still running came after the Manager's read: the reply need not name it.
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "claude-opus-4-1", "runs": 1, "running": 1, "cost_usd": None}],
     None, False, "Haiku $0.0409", None),
])
def test_a_tasks_cost_is_read_and_told_without_changes(
    monkeypatch: pytest.MonkeyPatch, task_id: str, status: str, by_model: list[dict[str, Any]],
    write_status: str | None, fake: bool, reply: str, problem: str | None,
) -> None:
    from evals.prompt_regression import dialogs

    actions = [{"tool": "looper_costs", "status": status, "risk": "read", "arguments": {"task_id": task_id},
                "result": None}]
    if write_status:
        actions.append({"tool": "looper_settings", "status": write_status, "risk": "write"})
    monkeypatch.setattr(dialogs, "_trail", lambda *_: actions)
    monkeypatch.setattr(dialogs.Look, "detail", lambda self, task_id=None: {"cost": {"by_model": by_model}})
    look = dialogs.Look(None, "task-a", reply, "", 0, frozenset(), fake)
    assert dialogs._cost_by_model_told(look) == problem


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_the_managers_dialogs_pass_in_fake_mode_and_a_wrong_call_fails(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    # The stand-in creates a task though the user named no project: the check sees it.
    eager = dialogs.Dialog("eager", (dialogs.Say("帮我写个脚本。", dialogs._asked_for_the_project, (dialogs._CREATE,)),))
    # Asked for a project's limit, which no longer exists, the stand-in changes the default for every task instead.
    stretch = dialogs.Dialog("stretch", (dialogs.Say(
        f"把项目 {dialogs.PROJECT} 的 Worker 并发改成 2。", dialogs._project_limit_explained,
        (("looper_settings", {"action": "set", "role": "worker", "values": {"max_concurrency": 2}}),)),))
    # Each dialog has a server of its own: the one after confirm_create_start sees none of its tasks.
    def alone(look: dialogs.Look) -> str | None:
        found = look.api.get("/api/tasks")["tasks"]
        return f"the dialog's server has {len(found)} tasks, not its own one" if len(found) != 1 else None

    def isolated(dialog: dialogs.Dialog) -> dialogs.Dialog:
        if dialog.id != "create_start_stop":
            return dialog
        first = dialog.says[0]
        check = dataclasses.replace(first, check=lambda look: alone(look) or first.check(look))
        return dataclasses.replace(dialog, says=(check, *dialog.says[1:]))

    ordered = tuple(isolated(dialog) for dialog in dialogs.DIALOGS)
    assert [d.id for d in ordered].index("create_start_stop") == [d.id for d in ordered].index(
        "confirm_create_start") + 1
    result, path = dialogs.run(mode="fake", dialogs=(*ordered, eager, stretch), admin_url=str(DSN),
                               out_dir=tmp_path, agent="claude-code/opus")
    by_id = {dialog["id"]: dialog for dialog in result["dialogs"]}
    for dialog in dialogs.DIALOGS:
        assert by_id[dialog.id]["outcome"] == "pass", (dialog.id, by_id[dialog.id]["problems"])
        assert by_id[dialog.id]["rep"] == 1 and by_id[dialog.id]["cost_usd"] > 0
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
    assert by_id["eager"]["outcome"] == "fail"
    assert [turn["calls"] for turn in by_id["concurrency"]["turns"]] == [["looper_concurrency:ok"], []]
    assert by_id["stretch"]["problems"] == [
        "a change waits for the user's confirmation, though a project has no limit to change"]
    assert "| cancel_confirmation | 1 | pass | 4 |" in dialogs.markdown(result)
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_dialog_without_a_reply_is_a_timeout_error_and_the_next_still_runs(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    # The stand-in Manager answers this message two minutes late.
    stalled = dialogs.Dialog("stalled", (dialogs.Say("现在忙吗？", dialogs._answered_by_reading,
                                                     (("sleep", {"seconds": 120}),)),))
    result, path = dialogs.run(mode="fake", dialogs=(stalled, *dialogs.select(["status"])), admin_url=str(DSN),
                               out_dir=tmp_path, timeout=15)
    late, status = result["dialogs"]
    assert (late["outcome"], late["error_class"], late["error"], late["turns"]) == (
        "error", "timeout", "no reply to '现在忙吗？' within 15 s", [])
    assert status["outcome"] == "pass", status["problems"]
    assert json.loads(path.read_text(encoding="utf-8")) == result and result["aborted"] is None


@pytest.mark.parametrize(("reply", "raw"), [
    ("任务已创建，还没有开始（Ready — not started）：\n- 任务：hello", False),
    ('好的。{"action": "task_start", "task_id": "task-1"}', True),
    ('```json\n{"status": "ok"}\n```', True),
    ('[{"status": "ok", "result": {}}]', True),
])
def test_a_dialog_reply_must_not_show_raw_json(reply: str, raw: bool) -> None:
    from evals.prompt_regression.dialogs import _raw_json

    assert (_raw_json(reply) is not None) is raw
