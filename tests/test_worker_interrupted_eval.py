"""The paired interruption eval exercises served Workers without calling a model."""

import copy
from contextlib import nullcontext
import json
import subprocess
import sys

import pytest

from evals.prompt_regression import worker_interrupted as evaluation
from tests.db_support import DSN


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    if not DSN:
        pytest.skip("requires disposable LOOPER_TEST_DATABASE_URL")
    root = tmp_path_factory.mktemp("interrupted-eval")
    result, path = evaluation.run(mode="fake", admin_url=DSN, out_dir=root / "report", reps=2)
    assert path.is_file()
    return result, path


def test_fake_pair_uses_real_served_worker_inputs_and_detects_duplicate_effect(paired):
    result, path = paired
    assert result["outcome"] == "pass", result
    assert len(result["runs"]) == 4
    assert len(result["pairs"]) == 2
    for pair in result["pairs"]:
        assert pair["only_hint_differs"], pair
        assert evaluation.EXPECTED_LINE in (path.parent / pair["diff"]).read_text()
    for row in result["runs"]:
        assert row["checks"]["same_attempt_new_run"]
        assert row["checks"]["recorded_input_matches_cli"]
        assert row["checks"]["real_worker_prompt"]
        assert row["checks"]["finished_task"]
        assert row["checks"]["output_written"]
        assert row["checks"]["partial_work_preserved"]
        inputs = row["inputs"]
        assert "publish.py release-1" in inputs["system_prompt"]
        assert "events.jsonl" not in inputs["system_prompt"] + inputs["user_prompt"]
        assert inputs["tools"] == ["Bash", "Read", "Write"]
        assert "--max-budget-usd" not in inputs["command"]  # fake mode cannot delegate to the real CLI
        if row["arm"] == "baseline":
            assert row["outcome"] == "fail"
            assert not row["checks"]["no_duplicate_effect"]
            assert row["effect_count"] == 2
            assert evaluation.EXPECTED_LINE not in inputs["user_prompt"]
        else:
            assert row["outcome"] == "pass", row
            assert row["effect_count"] == 1
            assert inputs["user_prompt"].count(evaluation.EXPECTED_LINE) == 1
            assert row["tool_calls"]
            assert all(row["checks"].values()), row


@pytest.mark.parametrize("field", ["system_prompt", "user_prompt", "command", "tools", "cwd", "launch_files", "workspace", "git_head"])
def test_pair_rejects_any_non_hint_model_input_drift(paired, field):
    rows = paired[0]["runs"][:2]
    baseline = copy.deepcopy(next(row["inputs"] for row in rows if row["arm"] == "baseline"))
    treatment = copy.deepcopy(next(row["inputs"] for row in rows if row["arm"] == "treatment"))
    assert evaluation.compare_inputs(baseline, treatment)["only_hint_differs"]
    if isinstance(treatment[field], dict):
        treatment[field]["unexpected"] = "unexpected"
    elif isinstance(treatment[field], list):
        treatment[field].append("unexpected")
    else:
        treatment[field] += "unexpected"
    assert not evaluation.compare_inputs(baseline, treatment)["only_hint_differs"]


def test_no_tool_evidence_is_not_a_behavior_pass(paired):
    row = copy.deepcopy(next(row for row in paired[0]["runs"] if row["arm"] == "treatment"))
    checks = evaluation.behavior_checks(tool_calls=[], effect_count=1, output="published release-1\n",
                                        partial="unfinished edit\n", audit=row["audit"])
    assert not checks["checked_before_effect"]
    assert not checks["inspected_workspace"]


@pytest.mark.parametrize("read_with", ["Bash", "Read"])
def test_existing_receipt_read_is_valid_evidence(read_with):
    def call(name, inputs, text):
        return {"name": name, "input": inputs, "result": {"content": text, "is_error": False}}

    calls = [call("Bash", {"command": "git status --short"}, " M partial.txt"),
             call("Read", {"file_path": "publish.py"}, evaluation.PUBLISHER)]
    if read_with == "Bash":
        calls.append(call("Bash", {"command": "cat ../publisher/events.jsonl"}, '{"release":"release-1"}'))
    else:
        calls.append(call("Read", {"file_path": "../publisher/events.jsonl"}, '{"release":"release-1"}'))
    checks = evaluation.behavior_checks(tool_calls=calls, effect_count=1, output="published release-1\n",
                                        partial="unfinished edit\n", audit=[])
    assert all(checks.values()), checks
    for item in calls:
        item["result"]["is_error"] = True
    checks = evaluation.behavior_checks(tool_calls=calls, effect_count=1, output="published release-1\n",
                                        partial="unfinished edit\n", audit=[])
    assert not checks["inspected_workspace"]
    assert not checks["inspected_publisher"]
    assert not checks["checked_before_effect"]


def test_real_cli_forwarder_preserves_launch_and_stream_with_a_local_stand_in(tmp_path, paired):
    """Exercise the paid-path process adapter without a Claude executable or model request."""
    from evals.prompt_regression import worker_interrupted_cli

    system = tmp_path / "system.txt"
    system.write_text(paired[0]["runs"][0]["inputs"]["system_prompt"])
    capture = tmp_path / "cli.json"
    forwarded = tmp_path / "forwarded.json"
    executable = tmp_path / "stand-in"
    executable.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n"
                          f"pathlib.Path({str(forwarded)!r}).write_text(json.dumps([sys.argv[1:], sys.stdin.read()]))\n"
                          "print('native stream output', flush=True)\n")
    executable.chmod(0o755)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mode": "real", "budget": 0.5, "claude": str(executable), "capture": str(capture)}))
    args = ["--print", "--append-system-prompt-file", str(system), "--strict-mcp-config"]
    evaluation.init_repository(tmp_path)
    stream = json.dumps({"message": {"content": "Start the step"}}) + "\n" + json.dumps({"message": {"content": "next"}}) + "\n"
    process = subprocess.run([sys.executable, worker_interrupted_cli.__file__, str(config), *args],
                             input=stream, text=True, capture_output=True, timeout=10, check=True, cwd=tmp_path)
    assert process.stdout == "native stream output\n"
    assert json.loads(forwarded.read_text()) == [args + ["--max-budget-usd", "0.5"], stream]
    assert json.loads(capture.read_text())["user_prompt"] == "Start the step"


def test_failed_arm_preserves_completed_arm_evidence(tmp_path, monkeypatch, paired):
    baseline = copy.deepcopy(paired[0]["runs"][0])
    monkeypatch.setattr(evaluation, "fresh_database", lambda _: nullcontext(("unused", 0)))

    def arm(*args, enabled, **kwargs):
        if enabled:
            raise evaluation.EvalError("synthetic launch failure")
        return baseline

    monkeypatch.setattr(evaluation, "_arm", arm)
    with pytest.raises(evaluation.EvalError, match="synthetic launch failure"):
        evaluation.run(mode="fake", admin_url="unused", out_dir=tmp_path)
    saved = json.loads((tmp_path / "worker-interrupted.json").read_text())
    assert saved["outcome"] == "error"
    assert saved["aborted"] == "synthetic launch failure"
    assert saved["runs"] == [baseline]


@pytest.mark.parametrize("kwargs", [
    {}, {"total_cap_usd": 1}, {"case_budget_usd": 1},
    {"total_cap_usd": float("nan"), "case_budget_usd": 1},
    {"total_cap_usd": 1, "case_budget_usd": float("inf")},
    {"total_cap_usd": 0.5, "case_budget_usd": 1},
])
def test_real_mode_requires_explicit_finite_caps_before_any_launch(tmp_path, kwargs):
    with pytest.raises(evaluation.EvalError, match="budget|cap"):
        evaluation.run(mode="real", admin_url="must-not-connect", out_dir=tmp_path, **kwargs)
