"""The paired interruption eval exercises served Workers without calling a model."""

import copy
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.prompt_regression import worker_interrupted as evaluation
from tests.db_support import DSN


@pytest.fixture(scope="module")
def paired(tmp_path_factory):
    if not DSN:
        pytest.skip("requires disposable LOOPER_TEST_DATABASE_URL")
    root = tmp_path_factory.mktemp("interrupted-eval")
    streams = []
    records = evaluation._records

    async def capture_records(url):
        rows = await records(url)
        streams.extend(row["events"] for row in rows if row["agent_id"].startswith("worker-") and row["events"])
        return rows

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evaluation, "_records", capture_records)
        result, path = evaluation.run(mode="fake", admin_url=DSN, out_dir=root / "report", reps=2)
    assert path.is_file()
    return result, path, streams


def test_fake_pair_uses_real_served_worker_inputs_and_detects_duplicate_effect(paired):
    result, path = paired[:2]
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


def test_fake_stream_contains_string_content_and_still_scores_end_to_end(paired):
    result, path, streams = paired
    assert len(streams) == 4
    assert all(any(event.get("type") == "user" and isinstance(event.get("message", {}).get("content"), str)
                   for event in events) for events in streams)
    assert result["outcome"] == "pass"
    assert all(row["unparsed_events"] == 0 for row in result["runs"])
    saved = sorted((path.parent / "runtime").glob("*.events.jsonl"))
    assert len(saved) == 4
    assert [evaluation._json_lines(file) for file in saved] == streams


@pytest.fixture
def native_stream_shapes():
    # Event shapes from a real Claude Code run; all text replaced by placeholders. The stream envelope was rebuilt from the CLI session log, which has no system init or result events.
    return evaluation._json_lines(Path(__file__).parent / "fixtures" / "worker_interrupted_stream_shapes.jsonl")


def test_native_stream_shapes_preserve_tool_evidence(score_stream, native_stream_shapes):
    assert len(native_stream_shapes) == 11
    row = score_stream(native_stream_shapes)
    assert [call["id"] for call in row["tool_calls"]] == ["toolu_01", "toolu_05", "toolu_08"]
    assert all(call["result"]["tool_use_id"] == call["id"] for call in row["tool_calls"])
    assert row["unparsed_events"] == 0


@pytest.mark.parametrize("failure_at", ["_parse_events", "behavior_checks"])
def test_raw_events_survive_scoring_failure(tmp_path, monkeypatch, score_stream, native_stream_shapes, failure_at):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic scoring failure")

    monkeypatch.setattr(evaluation, failure_at, fail)
    # The server and recorder are local stand-ins; no Claude process or model request is made.
    with pytest.raises(RuntimeError, match="synthetic scoring failure"):
        score_stream(native_stream_shapes, mode="real")
    saved = tmp_path / "report" / "runtime" / "1-treatment.events.jsonl"
    assert saved.is_file()
    assert [json.loads(line) for line in saved.read_text().splitlines()] == native_stream_shapes


@pytest.fixture
def score_stream(tmp_path, monkeypatch):
    """Feed recorded event shapes through the arm's actual scoring and row creation."""
    async def seed(*args, **kwargs):
        return 1, 7

    monkeypatch.setattr(evaluation, "_seed", seed)

    def score(events, *, mode="fake"):
        prompt = "You are a Worker in Looper"

        async def records(url):
            return [{"id": 2, "agent_id": "worker-after", "attempt_id": 7, "input_text": "start",
                     "metadata": {"system_prompt": prompt}, "events": events, "cost_usd": 0.25}]

        @contextmanager
        def server(**kwargs):
            (tmp_path / "received.json").write_text(json.dumps({"user_prompt": "start", "system_prompt": prompt}))
            (tmp_path / "work" / "result.txt").write_text("published release-1\n")
            yield SimpleNamespace(post=lambda *args: {"result": {"status": "completed"}})

        monkeypatch.setattr(evaluation, "_records", records)
        monkeypatch.setattr(evaluation, "running_server", server)
        return evaluation._arm(tmp_path, "unused", mode=mode, enabled=True, model="sonnet", budget=None,
                               claude=None, timeout=1, log=tmp_path / "server.log",
                               events_path=tmp_path / "report" / "runtime" / "1-treatment.events.jsonl")

    return score


def inspection_events():
    events = [{"type": "system", "subtype": "init", "tools": ["Bash", "Read", "Write"]}]
    for ident, name, inputs, receipt in [
        ("status", "Bash", {"command": "git status --short"}, " M partial.txt"),
        ("publisher", "Read", {"file_path": "publish.py"}, evaluation.PUBLISHER),
        ("receipt", "Read", {"file_path": "../publisher/events.jsonl"}, '{"release":"release-1"}'),
    ]:
        events.extend([
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident,
                                                          "name": name, "input": inputs}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
                                                     "content": receipt}]}},
        ])
    return events


def test_string_mixed_and_missing_content_preserve_real_tool_evidence(score_stream):
    events = inspection_events()
    events.insert(0, {"type": "user", "message": {"content": "initial prompt"}})
    events.append({"type": "user", "message": {}})
    events[2]["message"]["content"].insert(0, "not a block")
    row = score_stream(events)
    assert row["outcome"] == "pass", row
    assert all(row["checks"].values())
    assert [call["id"] for call in row["tool_calls"]] == ["status", "publisher", "receipt"]
    assert all(call["result"]["tool_use_id"] == call["id"] for call in row["tool_calls"])
    assert row["unparsed_events"] == 1
    assert row["cost_usd"] == 0.25


@pytest.mark.parametrize("unknown", [
    "unknown event", {"type": "future_event"}, {"type": "user", "message": []},
    {"type": "user", "message": {"content": 42}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
])
def test_unknown_event_shape_is_counted_without_aborting(score_stream, unknown):
    row = score_stream([*inspection_events(), unknown])
    assert row["unparsed_events"] == 1
    assert row["outcome"] == "pass", row


def test_unparsed_required_evidence_fails_with_a_reason(score_stream):
    row = score_stream([{"type": "user", "message": {"content": 42}}])
    assert row["unparsed_events"] == 1
    assert row["outcome"] == "fail"
    assert "inspected_workspace" in row["reason"]
    assert "tools_recorded" in row["reason"]


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


def test_real_mode_rejects_artifacts_inside_repository(monkeypatch):
    out_dir = Path(evaluation.__file__).resolve().parents[2] / "evals" / "runs" / "must-not-create"
    launches = []
    monkeypatch.setattr(evaluation.shutil, "which", lambda _: "stand-in")
    monkeypatch.setattr(evaluation, "fresh_database", lambda url: launches.append(url))
    with pytest.raises(evaluation.EvalError, match="outside.*repository"):
        evaluation.run(mode="real", admin_url="must-not-connect", out_dir=out_dir,
                       total_cap_usd=2, case_budget_usd=1)
    assert launches == []
    assert not out_dir.exists()


@pytest.mark.parametrize("reps,total", [(1, 1.5), (2, 3.5)])
def test_total_cap_reserves_both_arms_before_any_launch(tmp_path, monkeypatch, reps, total):
    launches = []

    def database(url):
        launches.append(url)
        raise RuntimeError("unexpected database launch")

    monkeypatch.setattr(evaluation.shutil, "which", lambda _: "stand-in")
    monkeypatch.setattr(evaluation, "fresh_database", database)
    with pytest.raises(evaluation.EvalError, match="reserve"):
        evaluation.run(mode="real", admin_url="must-not-connect", out_dir=tmp_path,
                       reps=reps, total_cap_usd=total, case_budget_usd=1)
    assert launches == []
    assert not (tmp_path / "worker-interrupted.json").exists()


@pytest.mark.parametrize("reps", [1, 2])
def test_total_cap_exact_reservation_boundary_is_accepted(tmp_path, monkeypatch, reps):
    launches = []
    monkeypatch.setattr(evaluation.shutil, "which", lambda _: "stand-in")
    monkeypatch.setattr(evaluation, "fresh_database", lambda url: nullcontext((url, 0)))

    def arm(*args, enabled, **kwargs):
        launches.append(enabled)
        return {"arm": "treatment" if enabled else "baseline", "cost_usd": 1, "outcome": "pass",
                "inputs": {"user_prompt": "start" + ("\n" + evaluation.EXPECTED_LINE if enabled else "")}}

    monkeypatch.setattr(evaluation, "_arm", arm)
    result, _ = evaluation.run(mode="real", admin_url="unused", out_dir=tmp_path,
                               reps=reps, total_cap_usd=2 * reps, case_budget_usd=1)
    assert result["outcome"] == "pass"
    assert launches == [False, True] * reps
