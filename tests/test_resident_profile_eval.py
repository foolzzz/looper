"""Synthetic resident-profile dialogs and their budgeted evaluation matrix; no paid calls."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.prompt_regression import resident_profile as profile
from evals.prompt_regression import dialogs
from evals.prompt_regression import runner
from evals.prompt_regression import __main__ as cli
from tests.db_support import DSN


@pytest.mark.parametrize(("question", "answer", "known", "passes"), [
    ("identity", "You are Mira Vale.", True, True),
    ("project", "You are working on Lantern Atlas.", True, True),
    ("restriction", "No proactive messages from 22:00 to 08:00 in Pacific/Honolulu.", True, True),
    ("identity", "你叫 Mira Vale。", True, True),
    ("identity", "You're Mira Vale.", True, True),
    ("restriction", "Proactive messages must not be sent between 22:00 and 08:00 in Pacific/Honolulu.", True, True),
    ("identity", "You are someone else.", True, False),
    ("identity", "You are not Mira Vale.", True, False),
    ("project", "You are working on another project.", True, False),
    ("restriction", "No proactive messages from 08:00 to 22:00 in Pacific/Honolulu.", True, False),
    ("restriction", "No proactive messages from 22:00 to 08:00 in UTC.", True, False),
    ("identity", "I don't know who you are.", False, True),
    ("identity", "I don’t know who you are.", False, True),
    ("project", "I do not know what you are working on. Please tell me.", False, True),
    ("restriction", "我不知道你的沟通限制。", False, True),
    ("identity", "You are Mira Vale.", False, False),
    ("identity", "I don't know your name. You are Alex.", False, False),
    ("identity", "I don't know your name, but you are Alex.", False, False),
    ("identity", "", False, False),
])
def test_answer_checks(question, answer, known, passes):
    assert (profile.answer_problem(question, answer, known=known) is None) is passes


@pytest.mark.parametrize("case", profile.cases(), ids=lambda case: case.id)
@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_profile_fake_mode_reads_synthetic_source_and_actual_stable_input(tmp_path, monkeypatch, case):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    result, path = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                               model="sonnet", stable_context=True)
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row
    assert row["state_views"] == ["whole"]
    assert row["models"]["manager"] == ["fake"]
    assert row["resolved_models"] == ["fake"]  # Runtime evidence differs from the configured Sonnet alias.
    assert json.loads(path.read_text()) == result


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_invented_answer_fails_the_fake_dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    case = profile.cases()[0]
    case = replace(case, says=(replace(case.says[0], fake=(("__capture_context__", {}),
                                    ("__reply__", {"text": "You are Alex."}))),))
    result, _ = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                            model="sonnet", stable_context=True)
    assert result["aborted"] is None
    assert result["dialogs"][0]["outcome"] == "fail"


@pytest.mark.parametrize("opus_outcome", ["fail", "pass", "error"])
def test_matrix_caps_resolved_models_and_failure_only_opus_retry(tmp_path, monkeypatch, opus_outcome):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        rows = [{"id": case.id, "rep": rep, "outcome": "pass", "success": True,
                 "cost_usd": .1, "models": {"manager": ["sonnet"]}, "resolved_models": ["resolved-version"]}
                for rep in range(1, kwargs["reps"] + 1) for case in kwargs["dialogs"]]
        if len(calls) == 2:
            rows[0].update(outcome=opus_outcome, success=opus_outcome == "pass")
        return {"dialogs": rows, "aborted": None, "stopped_by_cap": False}, Path("unused")

    monkeypatch.setattr(dialogs, "run", run)
    result, path = profile.run(mode="fake", admin_url="disposable", out_dir=tmp_path)
    assert runner.previous_run(out_dir=tmp_path, current=tmp_path / "zzzz-fake.json",
                               mode="fake", model=None, dialogs=True) is None
    if opus_outcome != "fail":
        assert len(calls) == 2
        assert len(result["dialogs"]) == 36
        return
    assert [c["reps"] for c in calls] == [2, 1, 1]
    assert [c["manager_model"] for c in calls] == ["sonnet", "opus", "opus"]
    assert [c["max_budget_usd"] for c in calls] == [.5, 1, 1]
    assert [c["total_cap_usd"] for c in calls] == pytest.approx([12, 9.6, 8.4])
    assert calls[1]["manager_effort"] == "xhigh"
    assert len(calls[2]["dialogs"]) == 1
    assert len(result["dialogs"]) == 37  # Preserve the first failure, even when its retry passes.
    assert result["dialogs"][-1]["attempt"] == 2
    assert result["total_cost_usd"] == pytest.approx(3.7)
    assert result["resolved_models"] == ["resolved-version"]
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize(("stopped", "outcome", "code"), [(False, "pass", 0), (False, "fail", 1),
                                                            (True, "pass", 2)])
def test_one_command_routes_the_plan_and_reports_incomplete_runs(monkeypatch, tmp_path, stopped, outcome, code):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"stopped_by_cap": stopped, "aborted": None, "total_cost_usd": .1,
                "dialogs": [{"outcome": outcome}]}, tmp_path / "report.json"

    monkeypatch.setattr(profile, "run", run)
    assert cli.main(["resident-profile", "--mode", "real", "--claude", "/synthetic/claude",
                     "--reps", "2,1", "--pg-url", "disposable", "--total-cap-usd", "12",
                     "--sonnet-cap-usd", ".5", "--opus-cap-usd", "1"]) == code
    assert calls[0]["reps"] == (2, 1)
    assert calls[0]["claude_bin"] == "/synthetic/claude"
    assert calls[0]["total_cap_usd"] == 12


def test_cap_stops_next_model_and_reports_unrun_cases(tmp_path, monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"dialogs": [{"id": kwargs["dialogs"][0].id, "rep": 1, "outcome": "pass",
                              "cost_usd": .5, "models": {"manager": ["fake"]}}],
                "aborted": None, "stopped_by_cap": True}, Path("unused")

    monkeypatch.setattr(dialogs, "run", run)
    result, _ = profile.run(mode="fake", admin_url="disposable", out_dir=tmp_path, total_cap_usd=.5)
    assert len(calls) == 1
    assert result["stopped_by_cap"] is True
    assert result["planned_calls"] == 36
    assert result["completed_calls"] == 1


@pytest.mark.parametrize("kwargs", [{"total_cap_usd": float("nan")}, {"sonnet_cap_usd": 0},
                                   {"opus_cap_usd": 2}, {"reps": (2, 2)}])
def test_invalid_plan_fails_before_starting(tmp_path, kwargs):
    with pytest.raises((ValueError, runner.EvalError)):
        profile.run(mode="fake", admin_url="unused", out_dir=tmp_path, **kwargs)
