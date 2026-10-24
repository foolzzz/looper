"""Offline contracts of the opt-in dsh real-validation launcher."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from examples.dsh import validate as launcher


PG = "postgresql://eval:password@127.0.0.1:55432/postgres"


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "probe_dsh", lambda: None)


def test_file_invocation_imports_checkout_evals(tmp_path):
    """Running the script by path must see evals; -m is not the only entry."""
    script = Path(__file__).resolve().parents[1] / "examples" / "dsh" / "validate.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert "smoke" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


@pytest.mark.parametrize("argv", [["--help"], ["preflight", "--help"], ["task-basic", "--help"], ["smoke", "--help"]])
def test_help_has_no_side_effects(argv, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("help read credentials or started a process")
    monkeypatch.setattr(launcher, "read_key", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as raised:
        launcher.main(argv)
    assert raised.value.code == 0
    assert "--help" in capsys.readouterr().out


def test_smoke_exposes_selected_dsh_through_a_symlink(tmp_path):
    """A shell wrapper hides the install from createRequire(realpath(dsh))."""
    target = tmp_path / "install" / "dsh"
    target.parent.mkdir()
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    target.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exposed = launcher.expose_selected_dsh(bin_dir, str(target))
    assert exposed.is_symlink()
    assert exposed.resolve() == target.resolve()
    assert not exposed.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_disposable_root_is_physical_for_dsh_snapshot_guards(tmp_path):
    """macOS tempfile keeps the /var alias. Snapshot parents and trees reject it."""
    from looper.agents.runtimes import RuntimeUnsupported
    from looper.agents.runtimes.dsh import _snapshot_parent, _snapshot_tree

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    linked = alias / "looper-dsh-smoke"
    linked.mkdir()
    root = launcher.disposable_root(linked)
    assert root == (physical / "looper-dsh-smoke").resolve()
    assert not root.is_symlink()
    session_parent = root / "runtime" / ".local" / "dsh" / "run-sessions" / "task"
    alias_parent = linked / "runtime" / ".local" / "dsh" / "run-sessions" / "task"
    with pytest.raises(RuntimeUnsupported, match="symlink"):
        _snapshot_parent(alias_parent)
    _snapshot_parent(session_parent)
    session = session_parent / "worker"
    (session / "dsh").mkdir(parents=True)
    (session / "dsh" / "keep").write_text("ok", encoding="utf-8")
    _snapshot_tree(session)
    with pytest.raises(RuntimeUnsupported, match="symlink"):
        _snapshot_tree(linked / "runtime" / ".local" / "dsh" / "run-sessions" / "task" / "worker")


def test_preflight_does_not_read_key_or_connect(ready, monkeypatch, capsys):
    import socket
    def forbidden(*args, **kwargs):
        pytest.fail("preflight read credentials or connected")
    monkeypatch.setattr(launcher, "read_key", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert launcher.main(["preflight", "--disposable-pg-url", PG]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["real_validation"] == "untested"
    assert result["credentials"] == "untested"
    assert result["interpreter_matches"] is True
    assert result["checkout_matches"] is True


@pytest.mark.parametrize("url", [None, "", "https://localhost/postgres",
    "postgresql://localhost/loopx", "postgresql://localhost/looper",
    "postgresql://localhost/existing", "postgresql://db.example/postgres",
    "postgresql://localhost:8767/postgres", "postgresql://localhost/postgres?service=operator"])
def test_refuse_missing_or_non_disposable_database(url, ready):
    with pytest.raises(launcher.ValidationError):
        launcher.preflight(url)


def test_refuse_wrong_checkout(ready, monkeypatch):
    import looper
    monkeypatch.setattr(looper, "__file__", "/elsewhere/looper/__init__.py")
    with pytest.raises(launcher.ValidationError, match="checkout"):
        launcher.preflight(PG)


@pytest.mark.parametrize("version", [None, "0.2.1", "0.2.1-rc.1", "0.2.1-rc.3"])
def test_version_must_match_exactly(version, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/fake/dsh")
    async def probe(*args, **kwargs):
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return launcher.ProcessResult(0, json.dumps({"version": version, "ready": True}).encode(), b"")
    monkeypatch.setattr(launcher, "run_process", probe)
    with pytest.raises(launcher.ValidationError, match="0.2.1-rc.2"):
        launcher.probe_dsh()


@pytest.mark.parametrize("ready", [False, True])
def test_matching_version_still_needs_working_help_and_process_observation(ready, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/fake/dsh")
    monkeypatch.setattr(launcher.proc_tree, "_snapshot", lambda: {})
    async def probe(*args, **kwargs):
        return launcher.ProcessResult(0, json.dumps({"version": "0.2.1-rc.2", "ready": ready}).encode(), b"")
    monkeypatch.setattr(launcher, "run_process", probe)
    with pytest.raises(launcher.ValidationError):
        launcher.probe_dsh()


@pytest.mark.parametrize("command", ["task-basic", "task-review", "manager", "smoke"])
def test_real_commands_require_explicit_opt_in(command, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "read_key", lambda: pytest.fail("read key without --real"))
    monkeypatch.setattr(launcher, "probe_dsh", lambda: pytest.fail("probed dsh without --real"))
    assert launcher.main([command, "--disposable-pg-url", PG]) == 2
    assert "--real" in capsys.readouterr().out


def test_smoke_outer_timeout_never_reports_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path)
    async def run(argv, *, env, timeout, capture):
        assert "synthetic-private-key" not in " ".join(argv)
        assert env["LOOPER_EVAL_API_KEY"] == "synthetic-private-key"
        assert "LOOPER_DATABASE_URL" not in env
        assert timeout == launcher.OUTER_TIMEOUT
        return launcher.ProcessResult(0, b'{"status":"pass"}', b"private", True)
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.smoke(PG, "synthetic-private-key")
    assert code != 0 and summary["status"] == "error"
    assert summary["outer_timeout"] is True


def test_smoke_process_failure_keeps_observed_cost_and_stage(monkeypatch):
    async def run(*args, **kwargs):
        return launcher.ProcessResult(1, json.dumps({"status": "error", "stage": "room_consult",
            "observed_cost_usd": .03, "auxiliary_cost_coverage": "unknown"}).encode(), b"private")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.smoke(PG, "synthetic-private-key")
    assert code != 0 and summary["status"] == "error"
    assert summary["observed_cost_usd"] == .03 and summary["stage"] == "room_consult"


@pytest.mark.parametrize("stdout,code,timed_out", [(b"", 0, False), (b" \n", 0, False),
    (b"secret", 1, False), (b"secret", 0, True), (b"noise\nsecret", 0, False)])
def test_empty_failed_or_noisy_key_capture_is_private(stdout, code, timed_out, monkeypatch, capsys):
    async def run(*args, **kwargs):
        return launcher.ProcessResult(code, stdout, b"secret diagnostic", timed_out)
    monkeypatch.setattr(launcher, "run_process", run)
    with pytest.raises(launcher.ValidationError) as raised:
        launcher.read_key()
    assert "secret" not in str(raised.value)
    assert capsys.readouterr() == ("", "")


def test_key_is_captured_only_in_memory(monkeypatch, capsys):
    async def run(argv, **kwargs):
        assert argv == ["zsh", "-ic", 'printf %s "$DEEPSEEK_API_KEY"']
        assert kwargs["timeout"] == launcher.KEY_CAPTURE_TIMEOUT == 30
        return launcher.ProcessResult(0, b"synthetic-private-key", b"ignored startup diagnostic", False)
    monkeypatch.setattr(launcher, "run_process", run)
    assert launcher.read_key() == "synthetic-private-key"
    assert "synthetic-private-key" not in repr(launcher.ProcessResult(0, b"synthetic-private-key", b"", False))
    assert capsys.readouterr() == ("", "")


def test_key_capture_exception_is_not_forwarded(monkeypatch, capsys):
    async def failed(*args, **kwargs):
        raise RuntimeError("synthetic-private-key")
    monkeypatch.setattr(launcher, "run_process", failed)
    with pytest.raises(launcher.ValidationError) as raised:
        launcher.read_key()
    assert "synthetic-private-key" not in str(raised.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("command,selector,case", [
    ("task-basic", "--tasks", "single_step,dependent_steps"),
    ("task-review", "--tasks", "reviewed_step,planner_plan_file"),
    ("manager", "--dialogs", "status,confirm_create_start"),
])
def test_eval_arguments_are_bounded_and_register_only_dsh(command, selector, case, tmp_path):
    argv = launcher.eval_argv(command, PG, tmp_path)
    assert argv[:3] == [sys.executable, "-m", "evals.prompt_regression"]
    for option, expected in {"--mode": "real", "--jobs": "1", "--reps": "2",
                             "--max-budget-usd": "0.50", "--total-cap-usd": "4", selector: case,
                             "--timeout" if command == "manager" else "--task-timeout": "300"}.items():
        assert argv[argv.index(option) + 1] == expected
    assert "--case-budget-usd" not in argv
    assert "--model" not in argv
    assert "--manager-model" not in argv
    spec = json.loads(Path(argv[argv.index("--register-agent") + 1]).read_text())
    assert spec["runtime"]["type"] == "dsh"
    assert spec["provider"]["auth"] == "api_key"
    assert spec["provider"]["protocols"] == ["anthropic-messages"]
    assert spec["provider"]["base_url"] == "https://api.deepseek.com/anthropic"
    assert spec["provider"]["api_key_env"] == "LOOPER_EVAL_API_KEY"
    assert "api_key" not in spec["provider"]
    assert spec["agent"]["model"] == "deepseek-flash"
    assert spec["agent"]["effort"] == "high"


def manager_row(case="confirm_create_start", rep=1):
    return {"id": case, "rep": rep, "outcome": "pass", "turns": [
        {"calls": ["looper_tasks:ok"]} if case == "status" else {"calls": []},
        *([] if case == "status" else [{"calls": ["looper_task_create:ok"]},
                                      {"calls": ["looper_task_control:ok"]}])],
        "runtime_evidence": [{"role": "manager", "runtime": "dsh", "executable": "dsh",
            "system_prompt_mode": "append", "session_id": "one-session", "status": "completed",
            "root_usage_complete": True, "tokens": {"input_tokens": 10, "output_tokens": 2},
            "observed_cost_usd": .01} for _ in range(1 if case == "status" else 3)]}


@pytest.mark.parametrize("problem", ["none", "missing", "runtime", "session", "missing_session", "mode", "mcp", "failed"])
def test_manager_acceptance_requires_runtime_session_and_mcp_evidence(problem):
    row = manager_row()
    if problem == "missing":
        row["runtime_evidence"].clear()
    elif problem == "runtime":
        row["runtime_evidence"][0]["runtime"] = "claude-code"
    elif problem == "session":
        row["runtime_evidence"][1]["session_id"] = "new-session"
    elif problem == "missing_session":
        row["runtime_evidence"][1]["session_id"] = None
    elif problem == "mode":
        row["runtime_evidence"][0]["system_prompt_mode"] = "replace"
    elif problem == "mcp":
        row["turns"][1]["calls"] = []
    elif problem == "failed":
        row["runtime_evidence"][0]["status"] = "failed"
    assert bool(launcher.manager_evidence_gaps([row])) is (problem != "none")


@pytest.mark.parametrize("problem", ["none", "runtime", "executable", "mode"])
def test_manager_acceptance_allows_started_planner_without_session(problem):
    row = manager_row()
    planner = {"role": "planner", "runtime": "dsh", "executable": "dsh",
               "system_prompt_mode": "append", "session_id": None, "status": "running",
               "root_usage_complete": None, "observed_cost_usd": None,
               "tokens": {"input_tokens": None, "output_tokens": None}}
    if problem == "runtime":
        planner["runtime"] = "claude-code"
    elif problem == "executable":
        planner["executable"] = "claude"
    elif problem == "mode":
        planner["system_prompt_mode"] = "replace"
    row["runtime_evidence"].append(planner)
    assert bool(launcher.manager_evidence_gaps([row])) is (problem != "none")


def test_manager_execute_accepts_completed_dialog_with_started_planner(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path)

    async def run(argv, **kwargs):
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        rows = [manager_row(case, rep) for case in ("status", "confirm_create_start") for rep in (1, 2)]
        for row in rows:
            if row["id"] == "confirm_create_start":
                row["runtime_evidence"].append({"role": "planner", "runtime": "dsh", "executable": "dsh",
                    "system_prompt_mode": "append", "session_id": None, "status": "running",
                    "root_usage_complete": None, "observed_cost_usd": None})
        (out_dir / "test-real.json").write_text(json.dumps({"dialogs": rows}))
        return launcher.ProcessResult(0, b"", b"")

    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute("manager", PG, "synthetic-private-key", None)
    assert code == 0 and summary["status"] == "pass"
    assert summary["evidence_gaps"] == []
    assert summary["effective_system_prompt_mode"] == "append"
    assert summary["outcomes"] == {"pass": 4, "fail": 0, "error": 0, "skipped": 0, "untested": 0}
    assert summary["root_usage_unknown_fraction"] == .2


def test_manager_execute_does_not_accept_dialog_success_without_runtime_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path)
    async def run(argv, **kwargs):
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        rows = [manager_row(case, rep) for case in ("status", "confirm_create_start") for rep in (1, 2)]
        rows[0]["runtime_evidence"] = []
        (out_dir / "test-real.json").write_text(json.dumps({"dialogs": rows}))
        return launcher.ProcessResult(0, b"", b"")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute("manager", PG, "synthetic-private-key", None)
    assert code != 0 and summary["evidence_gaps"]
    assert summary["effective_system_prompt_mode"] is None


def test_dialog_runtime_readback_keeps_unknown_usage_and_omits_private_command():
    from evals.prompt_regression import dialogs

    class Api:
        def get(self, path, **kwargs):
            if path == "/api/llm-records":
                return {"records": [{"id": 1}] if not kwargs.get("offset") else []}
            if path.endswith("/events"):
                return {"events": [{"seq": 1, "event": {"type": "result", "observed_cost_usd": .02,
                                                        "root_usage_complete": False}}]}
            return {"record": {"id": 1, "agent_id": "manager", "session_id": "continued",
                "status": "completed", "input_tokens": 10, "output_tokens": None,
                "metadata": {"command": ["env", "PRIVATE=synthetic-secret", "/bin/dsh"],
                    "system_prompt_mode": "append", "profile": {"runtime": {"type": "dsh"}}}}}

    rows = dialogs.runtime_evidence(Api())
    assert rows[0]["session_id"] == "continued"
    assert rows[0]["executable"] == "dsh"
    assert rows[0]["tokens"]["output_tokens"] is None
    assert rows[0]["root_usage_complete"] is False
    assert rows[0]["observed_cost_usd"] == .02
    assert "synthetic-secret" not in json.dumps(rows)


def test_manager_summary_preserves_unknown_usage_ratio_and_observed_spend():
    rows = [manager_row("status", rep) for rep in (1, 2)]
    rows[1]["runtime_evidence"][0]["root_usage_complete"] = False
    summary = launcher.summarize({"dialogs": rows, "total_cost_usd": 0}, ("status",), reps=2)
    assert summary["root_usage_unknown_fraction"] == .5
    assert summary["observed_cost_usd"] == .02


def test_basic_summary_keeps_missing_repetitions_as_gaps():
    summary = launcher.summarize({"tasks": [
        {"id": "single_step", "rep": 1, "outcome": "pass"},
        {"id": "dependent_steps", "rep": 1, "outcome": "error"},
    ]}, ("single_step", "dependent_steps"), reps=2)
    assert summary["outcomes"] == {"pass": 1, "fail": 0, "error": 1, "skipped": 0, "untested": 2}
    assert summary["cases"]["single_step"]["outcomes"]["untested"] == 1
    assert summary["cases"]["dependent_steps"]["outcomes"]["untested"] == 1


def test_basic_summary_does_not_accept_duplicate_repetitions():
    summary = launcher.summarize({"tasks": [
        {"id": "single_step", "rep": 1, "outcome": "pass"},
        {"id": "single_step", "rep": 1, "outcome": "pass"},
    ]}, ("single_step",), reps=2)
    assert summary["sample_gaps"]


@pytest.mark.parametrize("flag", ["timed_out", "stopped_by_cap"])
def test_summary_never_accepts_pass_with_timeout_or_cap(flag):
    summary = launcher.summarize({"tasks": [
        {"id": "single_step", "outcome": "pass", flag: True},
    ]}, ("single_step",))
    assert summary["outcomes"]["pass"] == 0
    assert summary["outcomes"]["error"] == 1


def basic_row(case="single_step", rep=1):
    return {"id": case, "rep": rep, "outcome": "pass", "contract_violations": {"total": 0},
        "planner_modified_files": 0, "runtime_evidence": [
            {"role": role, "runtime": "dsh", "executable": "dsh", "system_prompt_mode": "append",
             "session_present": True, "root_usage_complete": True} for role in ("planner", "worker")],
        "dependency_order": {"edges": 1, "verified": 1, "violations": 0, "unknown": 0}}


@pytest.mark.parametrize("problem", ["runtime", "missing_role", "mode", "contract", "planner_write", "dependency"])
def test_basic_acceptance_requires_evidence_beyond_task_pass(problem):
    row = basic_row("dependent_steps")
    if problem == "runtime":
        row["runtime_evidence"][0]["executable"] = "claude"
    elif problem == "missing_role":
        row["runtime_evidence"].pop()
    elif problem == "mode":
        row["runtime_evidence"][0]["system_prompt_mode"] = "replace"
    elif problem == "contract":
        row["contract_violations"]["total"] = 1
    elif problem == "planner_write":
        row["planner_modified_files"] = 1
    else:
        row["dependency_order"]["verified"] = 0
        row["dependency_order"]["unknown"] = 1
    assert launcher.basic_evidence_gaps([row])


def test_basic_acceptance_accepts_observed_runtime_and_order():
    assert launcher.basic_evidence_gaps([basic_row()]) == []


def review_row(case="reviewed_step", rep=1):
    row = basic_row(case, rep)
    row["reviews"] = 1 if case == "reviewed_step" else 0
    for run in row["runtime_evidence"]:
        run.update(status="completed", report_valid=True, verdict_passed=None)
    if case == "reviewed_step":
        row["runtime_evidence"].append({**row["runtime_evidence"][-1], "role": "reviewer",
                                       "verdict_passed": True})
    return row


@pytest.mark.parametrize("problem", ["missing_reviewer", "failed_reviewer", "invalid_verdict", "not_passed",
                                   "planner_write", "missing_worker", "invalid_worker", "runtime", "contract"])
def test_review_acceptance_requires_execution_and_parsed_verdict(problem):
    row = review_row()
    if problem == "missing_reviewer":
        row["runtime_evidence"].pop()
    elif problem == "failed_reviewer":
        row["runtime_evidence"][-1]["status"] = "failed"
    elif problem == "invalid_verdict":
        row["runtime_evidence"][-1]["report_valid"] = False
    elif problem == "not_passed":
        row["runtime_evidence"][-1]["verdict_passed"] = False
    elif problem == "planner_write":
        row["planner_modified_files"] = 1
    elif problem == "missing_worker":
        row["runtime_evidence"].pop(1)
    elif problem == "invalid_worker":
        row["runtime_evidence"][1]["report_valid"] = False
    elif problem == "runtime":
        row["runtime_evidence"][-1]["runtime"] = "claude-code"
    else:
        row["contract_violations"]["total"] = 1
    assert launcher.review_evidence_gaps([row])


def test_review_acceptance_accepts_both_cases_and_explained_guard_denials():
    rows = [review_row(case) for case in ("reviewed_step", "planner_plan_file")]
    rows[-1]["planner_denials"] = 1
    rows[-1]["runtime_evidence"][0]["permission_denials"] = [
        {"id": "denied-write", "tool": "write"}]
    assert launcher.review_evidence_gaps(rows) == []


def test_review_execute_rejects_aggregate_pass_without_reviewer_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path)
    async def run(argv, **kwargs):
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        rows = [basic_row(case, rep) for case in ("reviewed_step", "planner_plan_file") for rep in (1, 2)]
        (out_dir / "test-real.json").write_text(json.dumps({"tasks": rows}))
        return launcher.ProcessResult(0, b"", b"")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute("task-review", PG, "synthetic-private-key", None)
    assert code != 0 and summary["evidence_gaps"]
    assert summary["effective_system_prompt_mode"] is None


@pytest.mark.parametrize("command", ["task-basic", "task-review"])
@pytest.mark.parametrize("problem", ["missing_sample", "duplicate_rep", "runtime", "contract", "timeout"])
def test_execute_rejects_incomplete_or_invalid_success_rows(command, problem, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path)
    factory = review_row if command == "task-review" else basic_row
    rows = [factory(case, rep) for case in launcher.CASES[command].split(",") for rep in (1, 2)]
    if problem == "missing_sample":
        rows.pop()
    elif problem == "duplicate_rep":
        rows[1]["rep"] = 1
    elif problem == "runtime":
        rows[0]["runtime_evidence"][0]["executable"] = "claude"
    elif problem == "contract":
        rows[0]["contract_violations"]["total"] = 1
    else:
        rows[0]["timed_out"] = True
    async def run(argv, **kwargs):
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / "test-real.json").write_text(json.dumps({"tasks": rows}))
        return launcher.ProcessResult(0, b"", b"")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute(command, PG, "synthetic-private-key", None)
    assert code != 0
    assert summary["status"] != "pass"


def test_agent_spec_provider_passes_registry_validation():
    from looper.agents import profile_registry

    from evals.prompt_regression import runner

    spec = json.loads(launcher.SPEC.read_text())
    source = {key: value for key, value in spec["provider"].items() if key != "api_key_env"}
    runtime = {"runtime_id": "rt-dsh", "name": "DSH", "type": "dsh"}
    provider, models, selected = runner._legacy_eval_spec(spec, runtime, source)
    fields, _ = profile_registry._provider_fields(provider, None)
    assert fields["endpoints"] == [{"protocol": "anthropic-messages",
        "base_url": "https://api.deepseek.com/anthropic", "use_runtime_default": False}]
    assert selected == {"name": spec["agent"]["model"], "protocol": "anthropic-messages"}
    assert {model["name"] for model in models} == {selected["name"]}
    account = profile_registry.provider_spec({**fields, "id": "account"})
    model = profile_registry.ModelSpec("selected", account.id, **models[0])
    endpoint = profile_registry.Endpoint(account.id, model.protocol, fields["endpoints"][0]["base_url"])
    resolved = profile_registry.ResolvedModel(model, account, endpoint)
    resolved.check_runtime(profile_registry.RuntimeSpec("rt-dsh", "DSH", "dsh"), spec["agent"]["effort"])
    assert "api_key" not in provider and "api_key_env" not in provider


def test_summary_distinguishes_all_outcomes_and_missing_rows():
    rows = [{"id": name, "outcome": name} for name in ("pass", "fail", "error")]
    rows.append({"id": "skipped", "skipped": True})
    run = {"tasks": rows, "total_cost_usd": 0.12}
    summary = launcher.summarize(run, ("pass", "fail", "error", "skipped", "missing"))
    assert summary["outcomes"] == {"pass": 1, "fail": 1, "error": 1, "skipped": 1, "untested": 1}
    assert summary["auxiliary_cost_coverage"] == "unknown"
    assert summary["hard_billing_cap"] is False
    assert summary["observed_cost_usd"] == 0.12


@pytest.mark.parametrize("baseline,comparable", [
    (None, False), ([], False), ({"mode": "real", "validation": [], "tasks": None}, False),
    ({"tasks": [{"id": "single_step", "outcome": "pass"}]}, False),
    ({"validation": {"effective_system_prompt_mode": "replace"},
      "tasks": [{"id": "single_step", "outcome": "pass"}]}, False),
    ({"validation": {"effective_system_prompt_mode": "append"},
      "tasks": [{"id": "other", "outcome": "pass"}]}, False),
    ({"validation": {"effective_system_prompt_mode": "append"}, "mode": "fake",
      "tasks": [{"id": "single_step", "outcome": "pass"}]}, False),
    ({"validation": {"effective_system_prompt_mode": "append"}, "mode": "real",
      "tasks": [{"id": "single_step", "outcome": "pass"}]}, True),
])
def test_baseline_requires_explicit_case_and_effective_append(baseline, comparable):
    assert launcher.baseline_status(baseline, ("single_step",), dialogs=False)["comparable"] is comparable


def test_real_launch_keeps_key_out_of_output_spec_and_exception(ready, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "read_key", lambda: "synthetic-private-key")
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic-private-key")
    monkeypatch.setattr(launcher, "execute", failed)
    assert launcher.main(["task-basic", "--real", "--disposable-pg-url", PG]) == 2
    assert "synthetic-private-key" not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["task-basic", "task-review", "manager"])
def test_execute_uses_only_eval_key_env_and_ignored_private_root(command, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-credential")
    monkeypatch.setenv("LOOPER_DATABASE_URL", "operator-database")
    async def run(argv, *, env, timeout, capture):
        assert timeout == 1200
        assert capture is False
        assert env["LOOPER_EVAL_API_KEY"] == "synthetic-private-key"
        assert "DEEPSEEK_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "LOOPER_DATABASE_URL" not in env
        assert "synthetic-private-key" not in " ".join(argv)
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        assert Path(env["HOME"]).is_relative_to(out_dir)
        assert Path(env["TMPDIR"]).is_relative_to(out_dir)
        rows_key = "dialogs" if command == "manager" else "tasks"
        (out_dir / "test-real.json").write_text(json.dumps({rows_key: [
            (manager_row if command == "manager" else review_row if command == "task-review" else basic_row)(case, rep)
            for case in launcher.CASES[command].split(",")
            for rep in (1, 2)], "total_cost_usd": 0.1}))
        return launcher.ProcessResult(0, b"private eval output", b"private diagnostic")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute(command, PG, "synthetic-private-key", tmp_path / "missing-baseline.json")
    assert code == 0
    assert summary["baseline"]["comparable"] is False
    assert summary["outcomes"]["pass"] == 4
    for path in (tmp_path / "artifacts").rglob("*"):
        if path.is_file():
            assert "synthetic-private-key" not in path.read_text()


def test_outer_timeout_cleans_owned_descendants(tmp_path):
    marker = tmp_path / "child.pid"
    heartbeat = tmp_path / "heartbeat"
    child_code = ("import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
                  "\nwhile True: p.write_text(str(time.monotonic_ns())); time.sleep(0.05)")
    code = ("import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[3]]); "
            "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    result = asyncio.run(launcher.run_process([sys.executable, "-c", code, str(marker), child_code, str(heartbeat)],
                                              timeout=1, env={"PATH": os.environ["PATH"]}))
    assert result.timed_out
    assert marker.exists()  # positive control: a real child started before the timeout
    assert heartbeat.exists()  # it reached its loop, rather than failing to start
    pid = int(marker.read_text())
    stopped = heartbeat.read_text()
    time.sleep(0.2)
    if heartbeat.read_text() != stopped:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("launcher left its child running")


def _serve_smoke_stand_in(mode):
    """Process-level API/DB peer for the smoke orchestrator, not a model eval.

    The real serve/tiers/MCP fixture is exercised separately when PG is available.
    This peer makes errors deterministic at each workflow boundary.
    """
    from http.server import BaseHTTPRequestHandler
    from io import BytesIO
    from looper.agents.operator_auth import ensure_token
    from looper.agents.runtimes.dsh import DshRuntime

    # Exercise discovery through smoke's executable wrapper, including the
    # stand-in driver's adjacent script, before answering any API request.
    assert DshRuntime().probe("dsh").capabilities

    port, root = int(sys.argv[1]), Path(sys.argv[2])
    ensure_token(root)
    state_path = root / "peer.json"
    state = {"records": [], "sessions": [], "consults": [], "calls": []}
    messages, profiles, projects, tasks, roles = [], {}, {}, {}, {}

    def record(agent, task=""):
        from looper.agents.runtimes.dsh import DshHome
        role = agent.split("-", 1)[0]
        session = "session-shared" if mode == "owner" else "session-" + agent
        if role == "consult":
            session = "session-worker-" + task
        owner = root / "owners" / agent
        command = ["env", *(f"{k}={v}" for k, v in DshHome(owner, owner).paths.items()),
                   "dsh", "--profile", "headless", "--patch", str(owner / "configs" / "hash" / "looper.yml"), "-"]
        row = {"id": len(state["records"]) + 1, "agent_id": agent, "task_id": task,
               "session_id": session, "status": "completed", "model": "deepseek-flash",
               "input_tokens": 10, "output_tokens": 4, "cache_read_tokens": 7,
               "cache_creation_tokens": 2, "cost_usd": None,
               "metadata": {"profile": {"id": "profile", "runtime": {"type": "dsh"},
                                         "provider": {"id": "provider"}}, "system_prompt_mode": "append",
                            "command": command}}
        state["records"].append(row)
        if role in ("manager", "planner") and not any(s["agent_id"] == agent for s in state["sessions"]):
            state["sessions"].append({"agent_id": agent, "session_id": session, "runtime_type": "dsh",
                                      "provider_id": "provider", "profile_id": "profile"})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply("GET")

        def do_POST(self):
            self.reply("POST")

        def do_PUT(self):
            self.reply("PUT")

        def reply(self, method):
            from urllib.parse import urlsplit
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            state["calls"].append([method, path])  # never persist the provider request body
            result = {}
            status = 200
            if method == "POST" and path == "/api/v1/system/status":
                result = {"status": "ok", "result": {"status": "running"}}
            elif path == "/api/channel-configs":
                result = {"configs": [{"enabled": mode == "channels"}]}
            elif path == "/api/registry":
                result = {"runtimes": [{"name": "DSH", "type": "dsh", "builtin": True, "runtime_id": "rt-dsh"}],
                          "providers": [], "models": []}
            elif path == "/api/providers":
                assert body["api_key"] == "<memory-only>"
                assert body["endpoints"] == [{"protocol": "anthropic-messages",
                                              "base_url": "https://api.deepseek.com/anthropic"}]
                result = {"provider": {"provider_id": "provider"}}
            elif path == "/api/models":
                assert body["provider_id"] == "provider"
                assert (body["name"], body["protocol"]) == ("deepseek-flash", "anthropic-messages")
                result = {"model": {**body, "model_id": "model"}}
            elif path == "/api/agent-profiles":
                assert body["runtime_id"] == "rt-dsh"
                assert body["model_id"] == "model"
                profiles.update(body, profile_id="profile", provider_id="provider")
                result = {"profile": profiles}
                if mode == "profile":
                    status = 500
            elif path.startswith("/api/agent-settings/roles/"):
                assert body["run_budget_usd"] == .5
                roles[path.rsplit("/", 1)[1]] = {"values": body}
            elif path == "/api/agent-settings":
                result = {"roles": roles}
            elif path == "/api/chat/send":
                record("manager")
                if mode != "manager-timeout":
                    messages.append({"message_id": body["message_id"] + ":reply", "role": "assistant",
                                     "content": "private transcript /synthetic/private/project"})
            elif path == "/api/v1/operator_chat/messages":
                assert method == "POST" and body == {"session_id": "smoke"}
                result = {"status": "ok", "result": {"messages": messages}}
            elif path == "/api/projects":
                project = "project-" + str(len(projects) + 1)
                projects[project] = body
                result = {"project_id": project}
            elif path == "/api/tasks/create":
                task = "task-" + str(len(tasks) + 1)
                tasks[task] = body
                result = {"task_id": task}
            elif path == "/api/v1/ops/task_control/start":
                task = body["task_id"]
                result = {"status": "ok", "result": {"task_id": task, "started": True}}
                work = Path(projects[tasks[task]["project_id"]]["work_dir"])
                (work / "delivery.txt").write_text("delivered by disposable DSH\n")
                for agent in (f"planner-{task}", f"worker-{task}", f"reviewer-{task}", f"planner-{task}", f"consult-{task}"):
                    record(agent, task)
                state["consults"].append({"task_id": task, "state": "answered",
                    "source_record": state["records"][-4]["id"], "consult_run": "consult-" + task})
                if mode == "task":
                    status = 500
            elif path == "/api/v1/tasks/room_view":
                result = {"status": "ok", "result": {"consults": {"answered": 0 if mode == "consult" else 1,
                                      "list": [{"state": "failed" if mode == "consult" else "answered"}]}}}
            elif path.startswith("/api/tasks/"):
                result = {"status": "running" if mode == "task-timeout" else "completed"}
            elif path == "/api/llm-records":
                result = {"records": [{"id": r["id"]} for r in state["records"]]}
            elif path.endswith("/events"):
                result = {"events": [{"seq": 1, "event": {"type": "result", "is_error": False,
                    "observed_cost_usd": .6 if mode == "budget" else .000008442}}]}
            elif path.startswith("/api/llm-records/"):
                row = dict(state["records"][int(path.rsplit("/", 1)[1]) - 1])
                if mode == "readback":
                    row["status"] = "failed"
                result = {"record": row}
            state_path.write_text(json.dumps(state))
            wire = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    (root / "pid").write_text(str(os.getpid()))
    # File IPC lets the real serve lifecycle run even where the sandbox refuses
    # loopback sockets. Only this synthetic peer uses this transport.
    peer = root / "ipc"
    peer.mkdir()
    for number in range(10000):
        request_path = peer / f"request-{number}.json"
        while not request_path.exists():
            time.sleep(.005)
        request = json.loads(request_path.read_text())
        handler = Handler.__new__(Handler)
        handler.path = request["path"]
        payload = json.dumps(request["body"]).encode()
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile, handler.wfile = BytesIO(payload), BytesIO()
        status = []
        handler.send_response = status.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.reply(request["method"])
        response = peer / f"response-{number}.json"
        temporary = response.with_suffix(".tmp")
        temporary.write_text(json.dumps({"status": status[0], "body": json.loads(handler.wfile.getvalue())}))
        temporary.replace(response)


@pytest.fixture
def smoke_peer(tmp_path, monkeypatch):
    from evals.prompt_regression import runner
    from tests.dsh_runtime_stand_in import install

    # Reuse the four-role fixture's isolated binaries; the API peer never invokes
    # a real runtime or provider, including during discovery.
    bin_dir, _ = install(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("LOOPER_EVAL_API_KEY", "synthetic-private-key")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    owned = []
    original_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    @contextmanager
    def database(url):
        assert url == PG
        yield "postgresql://synthetic-disposable", 0

    monkeypatch.setattr(runner, "fresh_database", database)
    original_wait = launcher._wait
    monkeypatch.setattr(launcher, "_wait", lambda *a, **kw: original_wait(*a, **kw, timeout=.3))
    monkeypatch.setattr(launcher, "_session_accounting", lambda *a: {"coverage": "unknown", "reader": "untested"})

    def run(mode):
        root = tmp_path / "workflow"
        root.mkdir()

        class ProcessApi:
            def __init__(self, base_url):
                self.base_url, self.token, self.timeout = base_url, None, 2
                self.number = 0

            def request(self, method, path, body=None, **query):
                number = self.number
                self.number += 1
                peer = root / "runtime" / "ipc"
                deadline = time.monotonic() + 2
                while not peer.exists():
                    if time.monotonic() > deadline:
                        raise runner.EvalError("synthetic peer did not start")
                    time.sleep(.005)
                # Exercise credential registration in memory; the process peer
                # receives only a marker, never a credential in an IPC file.
                if path == "/api/providers":
                    assert body["api_key"] == "synthetic-private-key"
                    body = {**body, "api_key": "<memory-only>"}
                request = peer / f"request-{number}.json"
                temporary = request.with_suffix(".tmp")
                temporary.write_text(json.dumps({"method": method, "path": path, "body": body or {}}))
                temporary.replace(request)
                response = peer / f"response-{number}.json"
                while not response.exists():
                    if time.monotonic() > deadline:
                        raise runner.EvalError("synthetic API timed out")
                    time.sleep(.005)
                reply = json.loads(response.read_text())
                if reply["status"] != 200:
                    raise runner.EvalError("synthetic API failed")
                return reply["body"]

            get = lambda self, path, **kw: self.request("GET", path, **kw)
            post = lambda self, path, body: self.request("POST", path, body)
            put = lambda self, path, body: self.request("PUT", path, body)

        monkeypatch.setattr(runner, "Api", ProcessApi)
        monkeypatch.setattr(runner, "free_port", lambda: 49152 + int.from_bytes(os.urandom(2)) % 16000)
        script = ("from tests.test_dsh_validation import _serve_smoke_stand_in; "
                  f"_serve_smoke_stand_in({mode!r})")
        monkeypatch.setattr(runner, "_SERVE", script)
        def readback(url):
            assert url == "postgresql://synthetic-disposable"
            state = json.loads((root / "runtime" / "peer.json").read_text())
            if mode == "database":
                state["sessions"][0]["session_id"] = "wrong-session"
            return {"records": [{k: v for k, v in r.items() if k != "metadata"} for r in state["records"]],
                    "sessions": state["sessions"], "consults": state["consults"]}
        monkeypatch.setattr(launcher, "_smoke_database", readback)
        result = launcher._smoke_workflow(PG, root)
        assert owned and (root / "runtime" / "pid").exists()  # positive control: serve really ran
        assert all(p.poll() is not None for p in owned)
        return result, root
    return run


def test_smoke_process_peer_completes_four_roles_resume_and_readback(smoke_peer):
    (summary, code), root = smoke_peer("ok")
    assert code == 0 and summary["status"] == "pass"
    assert summary["roles"] == list(launcher.ROLES)
    assert summary["manager_turns"] == 2 and summary["tasks"] == 2
    assert summary["database_readback"] is True
    assert summary["auxiliary_cost_coverage"] == "unknown"
    assert summary["observed_cost_usd"] == pytest.approx(12 * .000008442)
    assert summary["hard_billing_cap"] is False
    wire = json.dumps(summary)
    for private in (str(root), "synthetic-private-key", "private transcript", "/synthetic/private"):
        assert private not in wire
    for path in root.rglob("*"):
        if path.is_file():
            assert "synthetic-private-key" not in path.read_text()


@pytest.mark.parametrize("mode,stage", [
    ("channels", "serve"), ("profile", "profile"), ("manager-timeout", "manager"),
    ("task", "tasks"), ("task-timeout", "tasks"), ("consult", "room_consult"),
    ("readback", "api_readback"), ("database", "db_readback"), ("budget", "manager"),
    ("owner", "api_readback"),
])
def test_smoke_process_peer_failure_reaps_serve_and_never_passes(smoke_peer, mode, stage):
    (summary, code), _ = smoke_peer(mode)
    assert code != 0 and summary["status"] == "error"
    assert summary["stage"] == stage
    assert summary["timeout"] is mode.endswith("timeout")
    if mode == "budget":
        assert summary["observed_cost_usd"] == .6


def test_session_reader_peer_counts_children_and_summary_usage_separately(tmp_path, monkeypatch):
    from looper.agents.runtimes.dsh import DshRuntime

    # Only synthetic public-module peers are imported by Node, never installed
    # dsh, its persistence or a model. The reader must use the official seam.
    if not launcher.shutil.which("node"):
        pytest.skip("Node is required for the Session reader process peer")
    executable = tmp_path / "dsh.mjs"
    executable.write_text("// synthetic installation anchor\n")
    owner = tmp_path / "runtime" / "owner"
    (owner / "dsh" / "sessions").mkdir(parents=True)
    modules = {
        "cordis": "export class Context {async dispose() {}}",
        "dsh-token-meter": "export class TokenMeter {constructor(ctx) {}}",
        "dsh-session-projection": """export class SessionProjectionRegistry {
          constructor(ctx) {ctx.sessionProjections = this;}
          stateOf(session) {return {totals: {uncachedInputTokens: session.id === 'root' ? 10 : 3,
            outputTokens: 4, cacheReadTokens: 7, cacheWriteTokens: 2}};}
        }""",
        "dsh-session": "export class Session {static fromRestore(id) {return {id};}}",
        "dsh-session-persistence-jsonl": """export async function apply(ctx, {root}) {
          if (!root.endsWith('/dsh/sessions')) throw Error('wrong private root');
          ctx.sessionPersistence = {
            async list() {return [{header: {id: 'root'}}, {header: {id: 'child', parentSession: 'root'}}];},
            async open(id, access) {
              if (access !== 'read') throw Error('reader took write ownership');
              return {inheritedEventCount: 0, async close() {}, async read() {return {eventState: 'owned',
                events: id === 'child' ? [{type: 'compaction/summary', data: {usage: {
                  inputTokens: 5, outputTokens: 2, cacheReadTokens: 1, cacheWriteTokens: 0,
                  private: '/synthetic/private/transcript'}}}] : []};}};
            }
          };
        }""",
    }
    for name, source in modules.items():
        package = tmp_path / "node_modules" / "@deepseek-ai" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"type": "module", "exports": "./index.mjs"}))
        (package / "index.mjs").write_text(source)
    original_which = launcher.shutil.which
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(executable) if name == "dsh" else original_which(name))
    monkeypatch.setattr(DshRuntime, "kept_session_dir", lambda self, command: owner)
    records = [{"metadata": {"command": []}}, {"metadata": {"command": []}}]
    summary = launcher._session_accounting(records, tmp_path / "runtime")
    assert summary["reader"] == "observed"
    assert summary["root_sessions"] == summary["child_sessions"] == summary["summary_requests"] == 1
    assert summary["coverage"] == "unknown"
    assert summary["token_meter_includes_summaries"] is False
    assert summary["root_observed_cost_usd"] == pytest.approx(.000008442)
    assert summary["child_observed_cost_usd"] == pytest.approx(.000006342)
    assert summary["summary_observed_cost_usd"] == pytest.approx(.000003906)
    assert "/synthetic/private" not in json.dumps(summary)


def test_session_reader_failure_keeps_coverage_unknown(tmp_path, monkeypatch):
    from looper.agents.runtimes.dsh import DshRuntime
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/synthetic/dsh")
    monkeypatch.setattr(DshRuntime, "kept_session_dir", lambda *a: tmp_path / "owner")
    async def failed(*args, **kwargs):
        return launcher.ProcessResult(7, b"private transcript", b"private path")
    monkeypatch.setattr(launcher, "run_process", failed)
    result = launcher._session_accounting([{"metadata": {}}], tmp_path)
    assert result["reader"] == "unavailable" and result["coverage"] == "unknown"
    assert "private" not in json.dumps(result)


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                    reason="disposable PostgreSQL is required for the real serve/tiers readback")
def test_smoke_real_serve_stand_in_keeps_the_room_consult_gap_visible(tmp_path, monkeypatch):
    """Reuse the four-role DB fixture. Its current script has no room consult;
    completing the task alone must never make this stricter smoke pass.
    """
    from evals.prompt_regression import runner
    from tests.dsh_runtime_stand_in import install, read_calls

    bin_dir, state = install(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("LOOPER_EVAL_API_KEY", "synthetic-private-key")
    original_database = runner.fresh_database
    original_popen = subprocess.Popen
    snapshots, owned = [], []

    @contextmanager
    def database(url):
        with original_database(url) as created:
            try:
                yield created
            finally:
                snapshots.append(launcher._smoke_database(created[0]))

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(runner, "fresh_database", database)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    root = tmp_path / "workflow"
    root.mkdir()
    summary, code = launcher._smoke_workflow(os.environ["LOOPER_TEST_DATABASE_URL"], root)
    assert code != 0 and summary["status"] == "error" and summary["stage"] == "room_consult"
    assert owned and all(process.poll() is not None for process in owned)
    calls = read_calls(state)
    assert {row["role"] for row in calls} == set(launcher.ROLES)
    managers = sorted((r for r in calls if r["role"] == "manager"), key=lambda r: r["turn"])
    assert [r["resumed"] for r in managers] == [False, True]
    assert len({r["session"] for r in managers}) == 1
    rows = snapshots[0]["records"]
    assert {r["session_id"] for r in rows} == {r["session"] for r in calls}
    assert all(r["status"] == "completed" for r in rows)
    assert "synthetic-private-key" not in json.dumps(snapshots, default=str)


def test_dialog_cost_uses_observed_root_spend_and_keeps_a_missing_one_unknown():
    from evals.prompt_regression.dialogs import observed_cost

    evidence = [{"role": "manager", "observed_cost_usd": .25}, {"role": "planner", "observed_cost_usd": .5}]
    assert observed_cost(0.0, evidence) == (.75, True)
    mixed = [{"role": "manager", "cost_usd": 2.0, "observed_cost_usd": None},
             {"role": "planner", "cost_usd": None, "observed_cost_usd": .5}]
    assert observed_cost(2.0, mixed) == (2.5, False)
    priced = [{"role": "planner", "cost_usd": .4, "observed_cost_usd": .5}]
    assert observed_cost(.4, priced) == (pytest.approx(.5), True)
    assert observed_cost(0.0, [*evidence, {"role": "manager", "observed_cost_usd": None}]) == (.75, False)
    assert observed_cost(0.0, [{"role": "manager", "observed_cost_usd": None}]) == (0.0, False)


def test_a_dialogs_running_check_counts_observed_root_spend():
    from evals.prompt_regression import dialogs

    class Api:
        def get(self, path, **params):
            if path == "/api/llm-records":
                return {"records": [{"id": 1, "status": "completed", "cost_usd": None}]}
            return {"events": [{"seq": 1, "event": {"type": "result", "observed_cost_usd": 1.2}}]}

    seen: dict[int, float] = {}
    assert dialogs._root_overrun(Api(), seen) == pytest.approx(1.2)
    assert dialogs._root_overrun(Api(), seen) == pytest.approx(1.2)
