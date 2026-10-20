"""Offline contracts of the opt-in dsh real-validation launcher."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from examples.dsh import validate as launcher


PG = "postgresql://eval:password@127.0.0.1:55432/postgres"


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "probe_dsh", lambda: None)


@pytest.mark.parametrize("argv", [["--help"], ["preflight", "--help"], ["task-basic", "--help"]])
def test_help_has_no_side_effects(argv, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("help read credentials or started a process")
    monkeypatch.setattr(launcher, "read_key", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(SystemExit) as raised:
        launcher.main(argv)
    assert raised.value.code == 0
    assert "--help" in capsys.readouterr().out


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


@pytest.mark.parametrize("command", ["task-basic", "task-review", "manager"])
def test_real_commands_require_explicit_opt_in(command, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "read_key", lambda: pytest.fail("read key without --real"))
    assert launcher.main([command, "--disposable-pg-url", PG]) == 2
    assert "--real" in capsys.readouterr().out


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
        assert kwargs["timeout"] == 10
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
    ("task-basic", "--tasks", "single_step"),
    ("task-review", "--tasks", "reviewed_step"),
    ("manager", "--dialogs", "status"),
])
def test_eval_arguments_are_bounded_and_register_only_dsh(command, selector, case, tmp_path):
    argv = launcher.eval_argv(command, PG, tmp_path)
    assert argv[:3] == [sys.executable, "-m", "evals.prompt_regression"]
    for option, expected in {"--mode": "real", "--jobs": "1", "--reps": "1",
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
            {"id": launcher.CASES[command], "outcome": "pass"}], "total_cost_usd": 0.1}))
        return launcher.ProcessResult(0, b"private eval output", b"private diagnostic")
    monkeypatch.setattr(launcher, "run_process", run)
    summary, code = launcher.execute(command, PG, "synthetic-private-key", tmp_path / "missing-baseline.json")
    assert code == 0
    assert summary["baseline"]["comparable"] is False
    assert summary["outcomes"]["pass"] == 1
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
