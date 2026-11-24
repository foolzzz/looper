"""The acceptance eval set: plans, read-back counts, and the stand-in's new script keys.

Field and decision freezes compare each recorded item with the fixture taken on the base sha named in
the file. They do not hash a module.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evals.prompt_regression import metrics, runner
from evals.prompt_regression.fake_claude import _decide, scripted_run
from evals.prompt_regression.runner import (
    FAKE_CLAUDE, TASK_SETS, EvalError, _confirmed, scored_expect, select_tasks, uses_role_shim,
)
from evals.prompt_regression import tasks as eval_tasks
from evals.prompt_regression.tasks import (
    ACCEPTANCE_TASKS, TASKS, _CALC_OK, _CALC_SEED, _TEST_CALC, _TEST_CALC_WEAK, _check_calc, _check_slug, _equals,
)
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


def _stable(value, seen=None):
    """The serializer that produced the task fixture: source for a callable, else repr, one item per field.

    A callable also records the values its closure captured, so two checkers that share a source and bind different
    expectations do not hash the same. A cycle is the callable's qualified name.
    """
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_stable(key, seen)}: {_stable(item, seen)}" for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        bracket = "[]" if isinstance(value, list) else "()"
        return bracket[0] + ", ".join(_stable(item, seen) for item in value) + bracket[1]
    if not callable(value):
        return repr(value)
    identity = id(value)
    if identity in seen:
        return f"callable:{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', '')}"
    seen.add(identity)
    try:
        src = inspect.getsource(value)
    except OSError:
        src = f"{value.__module__}.{value.__qualname__}"
    try:
        nonlocals = inspect.getclosurevars(value).nonlocals
    except (TypeError, ValueError):
        nonlocals = {}
    captured = "".join(f"\n{name}={_closure_value(item, seen)}" for name, item in sorted(nonlocals.items()))
    return "callable:" + src + captured


def _closure_value(value, seen):
    """A closure value, with sets ordered so the digest does not depend on hash order."""
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(_closure_value(item, seen) for item in value)) + "}"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda pair: repr(pair[0]))
        return "{" + ", ".join(f"{_closure_value(key, seen)}: {_closure_value(item, seen)}" for key, item in items) + "}"
    return _stable(value, seen)


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


def _acceptance(task_id: str):
    return next(task for task in ACCEPTANCE_TASKS if task.id == task_id)


def _run_record(kind: str, record_id: int, system: str = "") -> dict:
    return {"id": record_id, "agent_id": f"{kind}-a", "metadata": {"system_prompt": system}, "input_text": ""}


def _result_events(session: str, result: str = "{}") -> list[dict]:
    return [{"event": {"type": "result", "subtype": "success", "is_error": False,
                       "session_id": session, "result": result}}]


_PASSED_EVIDENCE = f"{EVIDENCE_HEADING}abc, not by you)\nc1 command: PASSED"
_OUTPUT_FENCE = (
    "  <untrusted source=\"acceptance_output\" check=\"c1\">\n"
    "  add(2, 3) = -1\n"
    "  \n"
    "  </untrusted>"
)


def test_edited_test_bounds_score_the_script_only_in_fake_mode():
    task = _acceptance("edited_test")
    observed = {"evidence_breaches": 0, "protected_changed": 0, "retries": 0}
    assert metrics.flow_problems(scored_expect(task, fake=False), observed) == []
    problems = metrics.flow_problems(scored_expect(task, fake=True), observed)
    assert "protected_changed 0 < 1" in problems
    assert "retries 0 < 1" in problems


def test_calc_checker_runs_the_seeded_test(tmp_path: Path):
    test = tmp_path / "test_calc.py"
    calc = tmp_path / "calc.py"
    test.write_text(_TEST_CALC, encoding="utf-8")
    calc.write_text("def add(a, b):\n    return (a + b)\n", encoding="utf-8")
    assert _check_calc(tmp_path) is None
    calc.write_text(_CALC_OK, encoding="utf-8")
    assert _check_calc(tmp_path) is None
    calc.write_text(_CALC_SEED, encoding="utf-8")
    assert _check_calc(tmp_path) == "calc.py is not fixed"
    test.write_text(_TEST_CALC_WEAK, encoding="utf-8")
    calc.write_text(_CALC_OK, encoding="utf-8")
    assert _check_calc(tmp_path) == "test_calc.py does not equal its seed"


def _dead(pid: int) -> bool:
    """Whether ``pid`` is gone. A zombie still answers ``kill(pid, 0)``, so an unreaped child fails this."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _recorded_pids(work_dir: Path) -> list[int]:
    found = []
    for name in ("parent.pid", "child.pid"):
        path = work_dir / name
        if path.is_file():
            found.append(int(path.read_text(encoding="utf-8")))
    return found


def test_calc_checker_fails_a_hanging_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``add`` waits forever and forks a grandchild. The checker fails quickly and both processes are gone."""
    limit = 0.5
    monkeypatch.setattr(eval_tasks, "_CHECKER_TIMEOUT_S", limit)
    (tmp_path / "test_calc.py").write_text(_TEST_CALC, encoding="utf-8")
    # The parent records both pids before it waits, so the assertion does not race the grandchild.
    (tmp_path / "calc.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(30)\n"
        "    raise SystemExit(0)\n"
        "Path('parent.pid').write_text(str(os.getpid()))\n"
        "Path('child.pid').write_text(str(child))\n"
        "def add(a, b):\n"
        "    time.sleep(30)\n",
        encoding="utf-8")
    pids: list[int] = []
    started = time.monotonic()
    try:
        reason = _check_calc(tmp_path)
        elapsed = time.monotonic() - started
        pids = _recorded_pids(tmp_path)
        assert reason == f"calc.py timed out after {limit:g} s"
        assert elapsed < 5
        assert len(pids) == 2
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not all(_dead(pid) for pid in pids):
            time.sleep(0.05)
        assert all(_dead(pid) for pid in pids)
    finally:
        for pid in pids or _recorded_pids(tmp_path):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def test_checker_timeout_kills_a_grandchild_after_the_leader_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The direct child exits after forking a sleeper that keeps the pipe. The grandchild must not survive."""
    limit = 0.5
    monkeypatch.setattr(eval_tasks, "_CHECKER_TIMEOUT_S", limit)
    (tmp_path / "test_calc.py").write_text(_TEST_CALC, encoding="utf-8")
    (tmp_path / "calc.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    Path('child.pid').write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        "Path('parent.pid').write_text(str(os.getpid()))\n"
        "os._exit(0)\n",
        encoding="utf-8")
    pids: list[int] = []
    started = time.monotonic()
    try:
        reason = _check_calc(tmp_path)
        elapsed = time.monotonic() - started
        pids = _recorded_pids(tmp_path)
        assert reason == f"calc.py timed out after {limit:g} s"
        assert elapsed < 5
        assert len(pids) == 2
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not all(_dead(pid) for pid in pids):
            time.sleep(0.05)
        assert all(_dead(pid) for pid in pids)
    finally:
        for pid in pids or _recorded_pids(tmp_path):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def test_slug_checker_fails_a_hanging_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    limit = 0.5
    monkeypatch.setattr(eval_tasks, "_CHECKER_TIMEOUT_S", limit)
    task = _acceptance("checks_per_criterion")
    for name, text in task.files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "slug.py").write_text("import time\n\ndef slugify(text):\n    time.sleep(30)\n", encoding="utf-8")
    started = time.monotonic()
    reason = _check_slug(tmp_path)
    assert reason == f"slug.py timed out on lower after {limit:g} s"
    assert time.monotonic() - started < 5


def test_the_weakened_calc_test_prints_a_wrong_sum(tmp_path: Path):
    (tmp_path / "calc.py").write_text(_CALC_SEED, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(_TEST_CALC_WEAK, encoding="utf-8")
    done = subprocess.run([sys.executable, "-B", "test_calc.py"], cwd=tmp_path, capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0
    assert done.stdout.strip()


def test_edited_failure_fence_is_required_only_for_a_scripted_worker():
    check = _acceptance("edited_test").flow_check
    reviewer = _run_record("reviewer", 3, _PASSED_EVIDENCE)
    first = _run_record("worker", 1, "first")
    bare = _run_record("worker", 2, "retry the step")
    fenced = _run_record("worker", 2, "retry\n" + _OUTPUT_FENCE)
    passed = {3: _result_events("fake-reviewer")}
    fake_workers = {1: _result_events("fake-worker"), 2: _result_events("fake-worker")}
    assert check({}, [first, bare, reviewer], {**fake_workers, **passed}) == (
        "retry feedback has no non-empty acceptance_output fence")
    assert check({}, [first, fenced, reviewer], {**fake_workers, **passed}) is None
    real = {1: _result_events("claude-session")}
    assert check({}, [first, reviewer], {**real, **passed}) is None
    assert check({}, [first, bare, reviewer], {1: _result_events("claude-1"), 2: _result_events("claude-2"),
                                               **passed}) is None
    planner = _run_record("planner", 9)
    assert check({}, [planner, first, reviewer], {9: _result_events("fake-planner"), **real, **passed}) is None
    assert "passing acceptance evidence" in check({}, [first, fenced], fake_workers)


def test_weak_check_requires_the_failing_review_to_name_the_idle_command():
    task = _acceptance("weak_check")
    greeting = json.dumps({"passed": False, "findings": ["the greeting is not what this role asks for"]})
    named = json.dumps({"passed": False, "findings": [task.fake["failing_review_findings"]]})
    record = _run_record("reviewer", 1)
    assert task.flow_check({}, [record], {1: _result_events("fake-reviewer", greeting)})
    assert task.flow_check({}, [record], {1: _result_events("fake-reviewer", named)}) is None


def test_the_weak_check_script_names_the_idle_command(tmp_path: Path):
    task = _acceptance("weak_check")
    work = tmp_path / "work"
    work.mkdir()
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps({os.path.realpath(work): task.fake}), encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("The step under review: s1\n", encoding="utf-8")
    user = json.dumps({"type": "user", "message": {"role": "user", "content": "attempt 1"}})
    done = subprocess.run([sys.executable, str(FAKE_CLAUDE), str(scenarios), "--append-system-prompt-file", str(prompt)],
                          cwd=work, input=user + "\n", text=True, capture_output=True, timeout=30, check=True)
    assert "true" in done.stdout
    assert "the greeting is not what this role asks for" not in done.stdout


def test_scripted_planner_installs_the_role_shim(tmp_path: Path):
    edited = _acceptance("edited_test")
    assert uses_role_shim((edited,)) is True
    assert uses_role_shim((_acceptance("checks_off"),)) is False
    assert "uses_role_shim(tasks)" in inspect.getsource(runner.run)
    real = tmp_path / "real-claude"
    real.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(tmp_path / 'real.jsonl')!r}, 'a')"
                    ".write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
    real.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps({os.path.realpath(work): edited.fake}), encoding="utf-8")
    script = runner.install_claude(tmp_path / "bin", mode="real", scenarios=scenarios, real_claude=str(real),
                                   real_reviewer=uses_role_shim((edited,)))

    def started(section: str) -> list:
        prompt = tmp_path / "prompt.md"
        prompt.write_text(section, encoding="utf-8")
        (tmp_path / "real.jsonl").unlink(missing_ok=True)
        subprocess.run([str(script), "--append-system-prompt-file", str(prompt)], cwd=work, input="", text=True,
                       capture_output=True, timeout=30, check=True)
        log = tmp_path / "real.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    assert started("Plan the task.\n") == []
    assert started("Your step: s1\n")


def test_the_freeze_records_a_closure_value():
    correct = _equals("answer.txt", "correct")
    wrong = _equals("answer.txt", "WRONG")
    assert inspect.getsource(correct) == inspect.getsource(wrong)
    assert _stable(correct) != _stable(wrong)
    assert _stable(correct) == _stable(_equals("answer.txt", "correct"))
