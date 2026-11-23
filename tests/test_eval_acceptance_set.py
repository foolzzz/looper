"""The acceptance eval set: plans, read-back counts, and the stand-in's new script keys.

Field and decision freezes compare each recorded item with the fixture taken on the base sha named in
the file. They do not hash a module.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import shlex
from pathlib import Path

import pytest

from evals.prompt_regression import metrics
from evals.prompt_regression.fake_claude import _decide, scripted_run
from evals.prompt_regression.runner import TASK_SETS, _confirmed, select_tasks
from evals.prompt_regression.runner import EvalError
from evals.prompt_regression.tasks import ACCEPTANCE_TASKS, TASKS
from looper.agents.acceptance_evidence import EVIDENCE_HEADING
from looper.agents.project_execution import AcceptanceChecksMode
from looper.agents.tiers.planner_protocol import ProtocolError, parse_plan

FIXTURE = json.loads(Path("tests/fixtures/eval_tasks_before_acceptance.json").read_text(encoding="utf-8"))
ROW_KEYS = json.loads(Path("tests/fixtures/eval_row_keys_before_acceptance.json").read_text(encoding="utf-8"))
# Keys the suite adds around task_metrics. They were on the recorded single_step row and are not metrics.
_ROW_WRAPPER = {
    "accepted_steps", "attempts", "cost_split", "duration_s", "failure_kinds", "first_pass_steps", "id",
    "outcome", "rep", "stopped_by_cap", "timed_out", "user_gates", "wait_resumes", "work_dir",
}
_ACCEPTANCE_NAMES = {
    "acceptance_checks", "checks_defined", "criteria_checked", "trivial_checks", "protected_changed",
    "rechecks", "evidence_breaches", "review_fails", "artifacts", "reviews_with_evidence",
}
_PATH_SUFFIXES = (".py", ".txt", ".sh")


def _stable(value):
    """The serializer that produced the task fixture: source for a callable, else repr, one item per field."""
    if callable(value):
        try:
            src = inspect.getsource(value)
        except OSError:
            src = f"{value.__module__}.{value.__qualname__}"
        return "callable:" + src
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_stable(key)}: {_stable(item)}" for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        bracket = "[]" if isinstance(value, list) else "()"
        return bracket[0] + ", ".join(_stable(item) for item in value) + bracket[1]
    return repr(value)


def _task_digest(task) -> str:
    payload = "\n".join(f"{name}={_stable(getattr(task, name))}" for name in FIXTURE["fields"])
    return hashlib.sha256(payload.encode()).hexdigest()


def _fence(steps) -> str:
    return "```json\n" + json.dumps({"steps": steps}) + "\n```"


def _path_tokens(command: str) -> list[str]:
    return [token for token in shlex.split(command) if "/" in token or token.endswith(_PATH_SUFFIXES)]


class _Api:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        return self.answers.pop(0)


def _metrics(**kwargs):
    values = dict(detail={"status": "completed"}, activity=[], records=[], events={}, changes={},
                  work_dir=Path("."), check_problem=None, expect={}, now=0)
    values.update(kwargs)
    return metrics.task_metrics(**values)


def test_existing_tasks_keep_their_recorded_fields():
    recorded = {task.id: task for task in TASKS if task.id in FIXTURE["tasks"]}
    assert set(recorded) == set(FIXTURE["tasks"])
    for task_id, digest in FIXTURE["tasks"].items():
        task = recorded[task_id]
        assert task.acceptance_checks is None
        assert _task_digest(task) == digest


def test_decide_answers_match_the_recorded_table():
    for case in FIXTURE["decide"]:
        assert _decide(case["script"], case["message"], {}) == case["answer"]


def test_a_row_without_acceptance_keeps_the_recorded_keys(tmp_path):
    result = metrics.task_metrics(detail={"status": "completed"}, activity=[], records=[], events={}, changes={},
                                  work_dir=tmp_path, check_problem=None, expect={}, now=0)
    recorded = set(ROW_KEYS["keys"])
    assert set(result) | _ROW_WRAPPER == recorded
    assert recorded.isdisjoint(_ACCEPTANCE_NAMES)
    assert _ACCEPTANCE_NAMES.isdisjoint(result)


def test_named_task_sets_keep_their_recorded_ids():
    assert [task.id for task in select_tasks(["hard"])] == FIXTURE["select_tasks"]["hard"]
    assert [task.id for task in select_tasks(["planner"])] == FIXTURE["select_tasks"]["planner"]
    assert TASK_SETS["acceptance"] == tuple(task.id for task in ACCEPTANCE_TASKS)
    assert set(TASK_SETS["acceptance"]) <= {task.id for task in TASKS}


@pytest.mark.parametrize("task", ACCEPTANCE_TASKS, ids=lambda task: task.id)
def test_acceptance_plans_parse_only_when_checks_are_enabled(task):
    text = _fence(task.fake["plan"])
    parse_plan(text, acceptance_checks=AcceptanceChecksMode.LOCAL)
    if task.id == "checks_off":
        parse_plan(text, acceptance_checks=AcceptanceChecksMode.OFF)
        return
    with pytest.raises(ProtocolError, match="acceptance_checks is off"):
        parse_plan(text, acceptance_checks=AcceptanceChecksMode.OFF)


def test_acceptance_checks_name_only_seeded_files():
    for task in ACCEPTANCE_TASKS:
        assert task.init_git
        seeded = set(task.files)
        for step in task.fake["plan"]:
            for check in step.get("checks") or []:
                outputs = set(check.get("artifact") or [])
                for token in _path_tokens(check["command"]):
                    if token in outputs:
                        continue
                    assert token in seeded, f"{task.id} {check['id']} names {token}"


def test_acceptance_metrics_count_trivial_commands_and_leave_a_near_miss():
    checks = [{"id": f"c{i}", "criterion": 1, "command": " ".join(command), "required": True}
              for i, command in enumerate(metrics.NOOP_COMMANDS)]
    checks.append({"id": "near", "criterion": 1, "command": "true && false", "required": True})
    detail = {"steps": [{"id": "s", "status": "accepted", "checks": checks, "result": {}}]}
    evidence = {"steps": [{"step_id": "s", "runs": []}]}
    counted = metrics.acceptance_metrics(detail, evidence)
    assert counted["trivial_checks"] == len(metrics.NOOP_COMMANDS)
    assert counted["checks_defined"] == len(metrics.NOOP_COMMANDS) + 1


def test_evidence_breaches_follow_the_current_run_of_a_verified_step():
    required = {"id": "c1", "criterion": 1, "command": "python3 test_calc.py", "required": True}
    advisory = {"id": "shot", "criterion": 1, "command": "python3 make_shot.py out/shot.png", "required": False}
    detail = {"steps": [{"id": "step-1", "status": "verified", "checks": [required, advisory],
                         "result": {"acceptance": {"rechecks_used": 1}}}]}

    def runs(*states):
        return {"steps": [{"step_id": "step-1", "runs": [
            {"step_id": "step-1", "check_id": "c1", "attempt": 1, "run": index, "state": state,
             "required": True, "protected_changed": changed or []}
            for index, (state, changed) in enumerate(states, 1)
        ]}]}

    failed = metrics.acceptance_metrics(detail, runs(("failed", [])))
    assert failed["evidence_breaches"] == 1
    assert failed["rechecks"] == 1
    advisory_failed = metrics.acceptance_metrics(detail, {"steps": [{"step_id": "step-1", "runs": [
        {"step_id": "step-1", "check_id": "c1", "attempt": 1, "run": 1, "state": "passed", "required": True},
        {"step_id": "step-1", "check_id": "shot", "attempt": 1, "run": 1, "state": "failed", "required": False},
    ]}]})
    assert advisory_failed["evidence_breaches"] == 0
    cleared = metrics.acceptance_metrics(detail, runs(("failed", []), ("passed", [])))
    assert cleared["evidence_breaches"] == 0
    protected = metrics.acceptance_metrics(detail, runs(("passed", ["test_calc.py"])))
    assert protected["evidence_breaches"] == 1
    assert protected["protected_changed"] == 1


def test_an_absent_evidence_read_adds_no_keys():
    detail = {"steps": [{"id": "s", "status": "verified", "checks": [
        {"id": "c1", "criterion": 1, "command": "true", "required": True}], "result": {"acceptance": {
            "rechecks_used": 4}}}]}
    assert metrics.acceptance_metrics(detail, None) == {}


def test_review_counts_use_reviewer_records_only():
    heading = f"{EVIDENCE_HEADING}abc, not by you)"
    records = [
        {"agent_id": "reviewer-a", "input_text": "", "metadata": {"outcome": "not_passed"}},
        {"agent_id": "reviewer-b", "input_text": "", "metadata": {"outcome": "passed", "system_prompt": heading}},
        {"agent_id": "worker-a", "input_text": heading, "metadata": {"outcome": "not_passed"}},
    ]
    detail = {"steps": [{"id": "s", "status": "verified", "checks": [], "result": {}}]}
    counted = metrics.acceptance_metrics(detail, {"steps": []}, records)
    assert counted["review_fails"] == 1
    assert counted["reviews_with_evidence"] == 1


def test_decision_rechecks_then_falls_through_to_retry():
    script = {"decision_rechecks": {"s1": 1}, "retry_feedback": {"s1": ["again"]}}
    message = "[DECISION] Step s1\nAllowed decisions now: retry (1 left), recheck, fail."
    retried, rechecked = {}, {}
    assert _decide(script, message, retried, rechecked)["decision"] == "recheck"
    assert _decide(script, message, retried, rechecked)["decision"] == "retry"
    withheld = "[DECISION] Step s1\nAllowed decisions now: retry (1 left), fail."
    assert _decide(script, withheld, {}, {})["decision"] == "retry"


@pytest.mark.parametrize("script,review,worker,scripted", [
    ({"real_reviewer": True}, False, True, True),
    ({"real_reviewer": True}, True, False, False),
    ({"real_planner": True}, False, False, False),
    ({"real_planner": True}, True, False, True),
    ({"real_planner": True}, False, True, True),
    ({"scripted_planner": True}, False, False, True),
    ({"scripted_planner": True}, False, True, False),
    ({"scripted_planner": True}, True, False, False),
    ({}, False, False, False),
])
def test_scripted_run_keeps_one_role_on_the_script(script, review, worker, scripted):
    assert scripted_run(script, review=review, worker=worker) is scripted


def test_confirmed_accepts_ok_and_confirms_once():
    ok = _Api([{"status": "ok", "result": {"acceptance_checks": "local"}}])
    assert _confirmed(ok, "/api/v1/acceptance_checks/update", {"project_id": "p", "acceptance_checks": "local"})["status"] == "ok"
    assert ok.calls == [("/api/v1/acceptance_checks/update", {"project_id": "p", "acceptance_checks": "local"})]
    asking = _Api([{"status": "needs_confirmation", "confirmation_id": "c1"}, {"status": "ok", "result": {}}])
    assert _confirmed(asking, "/api/v1/acceptance_checks/update", {"project_id": "p"})["status"] == "ok"
    assert asking.calls[1] == ("/api/v1/confirm", {"confirmation_id": "c1"})


def test_confirmed_rejects_any_other_status():
    with pytest.raises(EvalError, match="answered error"):
        _confirmed(_Api([{"status": "error"}]), "/api/v1/acceptance_checks/update", {})
    with pytest.raises(EvalError, match="confirmation answered error"):
        _confirmed(_Api([{"status": "needs_confirmation", "confirmation_id": "c1"}, {"status": "error"}]),
                   "/api/v1/acceptance_checks/update", {})


def test_task_metrics_apply_acceptance_counts_before_the_flow_bounds():
    passed = _metrics(expect={"min_checks_defined": 3}, acceptance={"checks_defined": 3})
    assert passed["success"]
    assert passed["checks_defined"] == 3
    missed = _metrics(expect={"min_checks_defined": 3})
    assert not missed["success"]
    assert "checks_defined" not in missed
    assert any(problem.startswith("checks_defined ") for problem in missed["problems"])
