"""Argument and budget boundaries of the Pi Responses eval script.

A fake ``uv`` records every invocation. Missing keys, endpoints and budgets
must fail before that ``uv`` runs. The script is not a new eval engine.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "examples" / "pi" / "eval-responses.sh"
EXAMPLE = REPO / "evals" / "agents" / "pi-cpa.example.json"
STATE = REPO / ".local" / "pi-eval-responses"
RUNS = REPO / "evals" / "runs"
FIXTURE_JSON = RUNS / "pi-responses-command-fixture.json"

TASKS = (
    "single_step,dependent_steps,reviewed_step,room_interface,room_consult,"
    "operator_notes,planner_plan_file,vault_closeout_keeps,vault_closeout_nothing"
)
DIALOGS = "status,confirm_create_start,config_tools,cancel_confirmation"
KEY = "sk-test-pi-eval-not-a-real-key"
ENDPOINT = "https://responses.example/v1"
TOTAL = "2.5"
CASE = "0.4"
PG_URL = "postgresql://127.0.0.1/postgres"

FAKE_UV = """\
#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
record = {"argv": sys.argv[1:], "telegram": os.environ.get("LOOPER_TELEGRAM_BOT_TOKEN", "")}
path = Path(os.environ["PI_EVAL_FAKE_UV_LOG"])
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
destination = os.environ.get("PI_EVAL_FAKE_RUN_JSON")
if destination:
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(os.environ.get("PI_EVAL_FAKE_RUN_BODY", ""), encoding="utf-8")
delay = os.environ.get("PI_EVAL_FAKE_UV_SLEEP")
if delay:
    time.sleep(float(delay))
"""


@pytest.fixture(autouse=True)
def clean_local_state():
    if STATE.exists():
        shutil.rmtree(STATE)
    FIXTURE_JSON.unlink(missing_ok=True)
    yield
    if STATE.exists():
        shutil.rmtree(STATE)
    FIXTURE_JSON.unlink(missing_ok=True)


def _environment(tmp_path: Path, **updates: str | None) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    uv = bindir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path / "home"),
        "PI_EVAL_API_KEY": KEY,
        "PI_EVAL_BASE_URL": ENDPOINT,
        "PI_EVAL_TOTAL_CAP_USD": TOTAL,
        "PI_EVAL_CASE_BUDGET_USD": CASE,
        "LOOPER_EVAL_DATABASE_URL": PG_URL,
        "PI_EVAL_FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "LOOPER_TELEGRAM_BOT_TOKEN": "telegram-secret-token",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in updates.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _run(tmp_path: Path, env: dict[str, str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
    )


def _invocations(tmp_path: Path) -> list[dict[str, object]]:
    log = tmp_path / "uv.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _command(invocations: list[dict[str, object]], name: str) -> list[str]:
    matches = []
    for record in invocations:
        argv = record["argv"]
        assert isinstance(argv, list)
        if "evals.prompt_regression" in argv and name in argv[argv.index("evals.prompt_regression") + 1:]:
            matches.append(argv)
    assert len(matches) == 1, invocations
    return matches[0]


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_assembles_runner_commands_from_explicit_env(tmp_path: Path) -> None:
    example_before = EXAMPLE.read_bytes()
    env = _environment(tmp_path)
    home = Path(env["HOME"])
    home.mkdir()
    secret = home / ".cpa" / "config.json"
    secret.parent.mkdir()
    secret.write_text(json.dumps({"base_url": "http://operator-cpa.invalid/v1"}), encoding="utf-8")
    secret.chmod(0)
    env["CPA_CONFIG"] = str(secret)
    env["PI_CPA_BASE_URL"] = "http://operator-cpa.invalid/v1"
    try:
        completed = _run(tmp_path, env)
    finally:
        secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert completed.returncode == 0, completed.stderr
    invocations = _invocations(tmp_path)
    assert len(invocations) == 2
    run_argv = _command(invocations, "run")
    dialog_argv = _command(invocations, "dialogs")
    for argv in (run_argv, dialog_argv):
        assert argv[:6] == ["run", "--extra", "test", "python", "-m", "evals.prompt_regression"]
        assert _flag(argv, "--mode") == "real"
        assert _flag(argv, "--reps") == "2"
        assert _flag(argv, "--jobs") == "3"
        assert _flag(argv, "--total-cap-usd") == TOTAL
        assert _flag(argv, "--case-budget-usd") == CASE
        assert _flag(argv, "--max-budget-usd") == CASE
        assert _flag(argv, "--pg-url") == PG_URL
        assert _flag(argv, "--out-dir") == "evals/runs"
        assert "--baseline" not in argv
        assert "8767" not in argv
        assert KEY not in " ".join(argv)
        spec_path = Path(_flag(argv, "--register-agent"))
        assert spec_path == STATE / "agent.json"
    assert [record["telegram"] for record in invocations] == ["", ""]
    assert _flag(run_argv, "--tasks") == TASKS
    assert _flag(dialog_argv, "--dialogs") == DIALOGS

    spec = json.loads((STATE / "agent.json").read_text(encoding="utf-8"))
    example = json.loads(example_before)
    assert spec["runtime"] == example["runtime"]
    assert spec["agent"] == example["agent"]
    assert spec["provider"]["name"] == example["provider"]["name"]
    assert spec["provider"]["protocols"] == example["provider"]["protocols"]
    assert spec["provider"]["auth"] == example["provider"]["auth"]
    assert spec["provider"]["api_key_env"] == "PI_EVAL_API_KEY"
    assert spec["provider"]["base_url"] == ENDPOINT
    assert KEY not in (STATE / "agent.json").read_text(encoding="utf-8")
    assert "operator-cpa.invalid" not in (STATE / "agent.json").read_text(encoding="utf-8")
    assert EXAMPLE.read_bytes() == example_before

    gaps = (STATE / "gaps.txt").read_text(encoding="utf-8")
    for phrase in ("steering", "session", "cancel", "strict", "operator_notes_stale", "schedule_",
                   "unverified", "catalog cost 0 is not a free run"):
        assert phrase in gaps
    assert "8767" not in SCRIPT.read_text(encoding="utf-8")
    for needle in ("~/.looper", "~/.claude", "~/.codex", "~/.grok"):
        assert needle not in SCRIPT.read_text(encoding="utf-8")


def test_database_url_prefers_eval_over_test(tmp_path: Path) -> None:
    test_url = "postgresql://localhost/postgres"
    env = _environment(tmp_path, LOOPER_TEST_DATABASE_URL=test_url)
    completed = _run(tmp_path, env)
    assert completed.returncode == 0, completed.stderr
    argv = _command(_invocations(tmp_path), "run")
    assert _flag(argv, "--pg-url") == PG_URL


def test_database_url_falls_back_to_test_database(tmp_path: Path) -> None:
    test_url = "postgresql://localhost:5432/postgres"
    env = _environment(tmp_path, LOOPER_EVAL_DATABASE_URL=None, LOOPER_TEST_DATABASE_URL=test_url)
    completed = _run(tmp_path, env)
    assert completed.returncode == 0, completed.stderr
    argv = _command(_invocations(tmp_path), "run")
    assert _flag(argv, "--pg-url") == test_url


@pytest.mark.parametrize("updates", [
    {"PI_EVAL_API_KEY": None},
    {"PI_EVAL_API_KEY": ""},
    {"PI_EVAL_API_KEY": "   "},
    {"PI_EVAL_BASE_URL": None},
    {"PI_EVAL_BASE_URL": ""},
    {"PI_EVAL_BASE_URL": "ftp://responses.example/v1"},
    {"PI_EVAL_BASE_URL": "/tmp/cpa.json"},
    {"PI_EVAL_TOTAL_CAP_USD": None},
    {"PI_EVAL_CASE_BUDGET_USD": None},
    {"PI_EVAL_TOTAL_CAP_USD": "0"},
    {"PI_EVAL_TOTAL_CAP_USD": "-1"},
    {"PI_EVAL_CASE_BUDGET_USD": "0"},
    {"PI_EVAL_CASE_BUDGET_USD": "nan"},
    {"PI_EVAL_TOTAL_CAP_USD": "inf"},
    {"PI_EVAL_TOTAL_CAP_USD": "1e309"},
    {"PI_EVAL_CASE_BUDGET_USD": "nope"},
    {"PI_EVAL_CASE_BUDGET_USD": "3", "PI_EVAL_TOTAL_CAP_USD": "1"},
    {"LOOPER_EVAL_DATABASE_URL": None},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://127.0.0.1/loopx"},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://db.example.com/postgres"},
    {"PI_EVAL_DEADLINE_SECONDS": "1501"},
    {"PI_EVAL_DEADLINE_SECONDS": "0"},
])
def test_preflight_stops_before_uv(tmp_path: Path, updates: dict[str, str | None]) -> None:
    completed = _run(tmp_path, _environment(tmp_path, **updates))
    assert completed.returncode != 0
    assert _invocations(tmp_path) == []
    assert KEY not in completed.stdout + completed.stderr
    gaps = (STATE / "gaps.txt").read_text(encoding="utf-8")
    assert "unverified" in gaps
    assert not (STATE / "agent.json").exists()


def test_deadline_kills_the_runner_and_records_the_gap(tmp_path: Path) -> None:
    env = _environment(tmp_path, PI_EVAL_DEADLINE_SECONDS="1", PI_EVAL_FAKE_UV_SLEEP="30")
    started = time.monotonic()
    completed = _run(tmp_path, env, timeout=20)
    elapsed = time.monotonic() - started
    assert elapsed < 15
    assert completed.returncode != 0
    assert _invocations(tmp_path), "the first runner starts before the deadline"
    gaps = (STATE / "gaps.txt").read_text(encoding="utf-8")
    assert "deadline" in gaps
    assert "steering" in gaps


def test_zero_catalog_cost_is_not_recorded_as_free(tmp_path: Path) -> None:
    body = json.dumps({
        "agent": "pi/cpa/gpt-6.1-sol",
        "tasks": [{
            "id": "single_step",
            "rep": 1,
            "success": True,
            "cost_usd": 0,
            "cost_basis": {"equivalent": 0, "provider_price": 0, "unpriced": 1},
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        }],
    })
    env = _environment(tmp_path, PI_EVAL_FAKE_RUN_JSON=str(FIXTURE_JSON), PI_EVAL_FAKE_RUN_BODY=body)
    completed = _run(tmp_path, env)
    assert completed.returncode == 0, completed.stderr
    gaps = (STATE / "gaps.txt").read_text(encoding="utf-8")
    assert "pricing unverified: single_step rep 1" in gaps
    assert "catalog cost 0 is not a free run" in gaps
    assert "readback agent: pi/cpa/gpt-6.1-sol" in gaps
    assert "outcome pass" in gaps
