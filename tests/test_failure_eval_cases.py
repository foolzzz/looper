"""Failure evaluation fixtures never replace a successful model answer."""
import json
from pathlib import Path
import subprocess
import sys


def test_new_cases_are_registered_and_keep_real_roles():
    from evals.prompt_regression import dialogs, tasks
    wanted = {"failure_retry_context", "failure_breaker_history", "failure_reviewer_resume"}
    cases = tasks.select(list(wanted))
    assert {case.id for case in cases} == wanted
    assert all(case.failure_handling and not case.real_reviewer for case in cases)
    assert all(not case.fake.get("real_planner") and not case.fake.get("scripted_planner") for case in cases)
    names = {"task_hold_retry", "task_hold_planner", "task_hold_stop", "task_hold_operator_boundary"}
    found = {case.id: case for case in dialogs.select(list(names))}
    assert set(found) == names
    assert all(case.failure_hold for case in found.values())
    assert found["task_hold_operator_boundary"].client


def test_error_wrapper_delegates_every_role_after_fixture_errors(tmp_path):
    from evals.prompt_regression import failure_cli
    scenario = tmp_path / 'scenario.json'
    scenario.write_text(json.dumps({str(tmp_path): {"_engine_failures": {"worker": ["connection_failed"]},
                                                   "_engine_residual": True}}))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Your step:\nCreate the receipt.')
    delegate = tmp_path / 'delegate.py'
    delegate.write_text('import sys; print("DELEGATED", *sys.argv[1:])')
    command = [sys.executable, str(Path(failure_cli.__file__)), str(scenario),
               json.dumps([sys.executable, str(delegate)]), '--append-system-prompt-file', str(prompt), '--model', 'test']
    first = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=True)
    events = [json.loads(line) for line in first.stdout.splitlines()]
    assert events[-1]['api_error'] == 'connection_failed'
    assert events[-1]['is_error'] and events[-1]['total_cost_usd'] == 0
    assert (tmp_path / 'receipt.txt').read_text() == 'applied once\n'
    second = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert 'DELEGATED --append-system-prompt-file' in second.stdout and '--model test' in second.stdout
    prompt.write_text('Planner turn')
    third = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert third.stdout.startswith('DELEGATED')


def test_failure_cases_require_separate_databases(tmp_path):
    import pytest
    from evals.prompt_regression import runner, tasks
    cases = tasks.select(["failure_retry_context", "failure_reviewer_resume"])
    with pytest.raises(runner.EvalError, match="separate run and disposable database"):
        runner.run(mode="fake", tasks=cases, admin_url="unused", out_dir=tmp_path)
    assert not any(case.failure_handling for case in tasks.select(None))
