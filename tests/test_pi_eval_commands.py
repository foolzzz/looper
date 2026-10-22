"""Argument and budget boundaries of the Pi provider eval scripts.

A fake ``uv`` records every invocation. Missing keys, endpoints and budgets
must fail before that ``uv`` runs. The script is not a new eval engine.
"""
from __future__ import annotations

import fcntl
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
if os.environ.get("PI_EVAL_FAKE_UV_EXIT"):
    sys.exit(int(os.environ["PI_EVAL_FAKE_UV_EXIT"]))
"""


@pytest.fixture(autouse=True)
def clean_local_state():
    # The scripts write under the repository's .local and evals/runs, which every pytest-xdist worker shares: one
    # test at a time, across processes, owns that state.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with (STATE.parent / "pi-eval-commands.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
    uv.write_text(FAKE_UV + '''
if not destination and "dialogs" not in sys.argv:
    args = sys.argv[1:]
    spec = json.loads(Path(args[args.index("--register-agent") + 1]).read_text())
    out = Path(args[args.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "pi-responses-command-fixture.json").write_text(json.dumps({
        "agent": spec["agent"]["name"], "total_cost_usd": 0.2,
        "tasks": [{"id": "single_step", "rep": 1, "success": True, "cost_usd": 0.2,
                   "cost_basis": {"provider_price": 1}, "tokens": {"input": 11, "output": 3}}],
    }))
''', encoding="utf-8")
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


def _run(tmp_path: Path, env: dict[str, str], *, timeout: float = 30,
         script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, text=True,
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
        assert _flag(argv, "--total-cap-usd") == (TOTAL if argv is run_argv else "2.3")
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
    env = _environment(tmp_path, PI_EVAL_DEADLINE_SECONDS="5", PI_EVAL_FAKE_UV_SLEEP="60")
    started = time.monotonic()
    completed = _run(tmp_path, env, timeout=60)
    elapsed = time.monotonic() - started
    assert elapsed < 45
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
    assert completed.returncode == 2, completed.stderr
    assert len(_invocations(tmp_path)) == 1
    gaps = (STATE / "gaps.txt").read_text(encoding="utf-8")
    assert "pricing unverified: single_step rep 1" in gaps
    assert "catalog cost 0 is not a free run" in gaps
    assert "readback agent: pi/cpa/gpt-6.1-sol" in gaps
    assert "outcome pass" in gaps


DEEPSEEK_SCRIPT = REPO / "examples" / "pi" / "eval-deepseek.sh"
DEEPSEEK_STATE = REPO / ".local" / "pi-eval-deepseek"
DEEPSEEK_EXAMPLE = REPO / "evals" / "agents" / "pi-deepseek.example.json"
DEEPSEEK_AGENT = "pi/deepseek/deepseek-flash"


@pytest.fixture
def deepseek_env(tmp_path: Path):
    env = _environment(tmp_path, DEEPSEEK_API_KEY=KEY, PI_EVAL_API_KEY="wrong-provider-key",
                       PI_EVAL_BASE_URL="https://wrong-provider.invalid/v1")
    # Emit one priced result into the requested output directory, like the real runner.
    uv = tmp_path / "bin" / "uv"
    uv.write_text(FAKE_UV + '''
if os.environ.get("PI_EVAL_FAKE_NO_RESULT") != "1":
    args = sys.argv[1:]
    phase = "dialogs" if "dialogs" in args else "tasks"
    body = json.loads(os.environ.get("PI_EVAL_FAKE_RESULT", "null"))
    if body is None:
        body = {"agent": "pi/deepseek/deepseek-flash", "total_cost_usd": 0.2,
                phase: [{"id": "status" if phase == "dialogs" else "single_step", "rep": 1,
                         "outcome": "pass", "cost_usd": 0.2,
                         "cost_basis": {"provider_price": 1, "unpriced": 0},
                         "tokens": {"input": 11, "cache_read": 5, "output": 3}}]}
    out = Path(args[args.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / (phase + ".json")).write_text(json.dumps(body))
''', encoding="utf-8")
    if DEEPSEEK_STATE.exists():
        shutil.rmtree(DEEPSEEK_STATE)
    yield env
    if DEEPSEEK_STATE.exists():
        shutil.rmtree(DEEPSEEK_STATE)


def test_deepseek_commands_use_chat_spec_and_fresh_results(tmp_path: Path, deepseek_env) -> None:
    before = DEEPSEEK_EXAMPLE.read_bytes()
    FIXTURE_JSON.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_JSON.write_text(json.dumps({"agent": "pi/cpa/gpt-6.1-sol", "total_cost_usd": 999}))
    for _ in range(2):
        completed = _run(tmp_path, deepseek_env, script=DEEPSEEK_SCRIPT)
        assert completed.returncode == 0, completed.stderr
    invocations = _invocations(tmp_path)
    assert len(invocations) == 4
    outputs = []
    for pair in (invocations[:2], invocations[2:]):
        tasks, dialogs = _command(pair, "run"), _command(pair, "dialogs")
        outputs.append(_flag(tasks, "--out-dir"))
        assert _flag(tasks, "--out-dir") == _flag(dialogs, "--out-dir")
        assert Path(outputs[-1]).is_relative_to(DEEPSEEK_STATE)
        assert _flag(tasks, "--tasks") == TASKS
        assert _flag(dialogs, "--dialogs") == DIALOGS
        assert float(_flag(tasks, "--total-cap-usd")) == float(TOTAL)
        assert float(_flag(dialogs, "--total-cap-usd")) == pytest.approx(float(TOTAL) - 0.2)
        for argv in (tasks, dialogs):
            assert _flag(argv, "--mode") == "real"
            assert _flag(argv, "--reps") == "2"
            assert _flag(argv, "--jobs") == "3"
            assert _flag(argv, "--case-budget-usd") == CASE
            assert _flag(argv, "--max-budget-usd") == CASE
            assert _flag(argv, "--pg-url") == PG_URL
            assert "--baseline" not in argv
            assert KEY not in " ".join(argv)
            spec = json.loads(Path(_flag(argv, "--register-agent")).read_text())
            assert spec["runtime"] == {"type": "pi"}
            assert spec["agent"] == {"name": DEEPSEEK_AGENT, "model": "deepseek-flash"}
            assert spec["provider"]["protocols"] == ["openai-chat"]
            assert spec["provider"]["auth"] == "api_key"
            assert spec["provider"]["base_url"] == "https://api.deepseek.com"
            assert spec["provider"]["api_key_env"] == "DEEPSEEK_API_KEY"
            assert KEY not in json.dumps(spec)
    assert outputs[0] != outputs[1]
    assert all(record["telegram"] == "" for record in invocations)
    assert DEEPSEEK_EXAMPLE.read_bytes() == before
    gaps = (DEEPSEEK_STATE / "gaps.txt").read_text()
    assert "readback agent: pi/cpa/" not in gaps
    assert "usage input 11 cache_read 5 output 3" in gaps
    for term in ("steering", "session", "cancel", "schema", "read_only", "CPA", "provider/API",
                 "input", "cache_read", "output", "pricing", "unverified"):
        assert term in gaps
    subprocess.run(["bash", "-n", str(DEEPSEEK_SCRIPT)], check=True, timeout=5)


@pytest.mark.parametrize("updates", [
    {"DEEPSEEK_API_KEY": None}, {"DEEPSEEK_API_KEY": ""}, {"DEEPSEEK_API_KEY": "   "},
    {"PI_EVAL_TOTAL_CAP_USD": None}, {"PI_EVAL_CASE_BUDGET_USD": None},
    {"PI_EVAL_TOTAL_CAP_USD": "0"}, {"PI_EVAL_TOTAL_CAP_USD": "-1"},
    {"PI_EVAL_CASE_BUDGET_USD": "0"}, {"PI_EVAL_CASE_BUDGET_USD": "nan"},
    {"PI_EVAL_TOTAL_CAP_USD": "inf"}, {"PI_EVAL_TOTAL_CAP_USD": "1e309"},
    {"PI_EVAL_CASE_BUDGET_USD": "nope"},
    {"PI_EVAL_CASE_BUDGET_USD": "3", "PI_EVAL_TOTAL_CAP_USD": "1"},
    {"PI_EVAL_DEADLINE_SECONDS": "1501"}, {"PI_EVAL_DEADLINE_SECONDS": "0"},
    {"PI_EVAL_DEADLINE_SECONDS": "nan"},
    {"LOOPER_EVAL_DATABASE_URL": None},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://127.0.0.1/loopx"},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://db.example.com/postgres"},
])
def test_deepseek_preflight_rejects_before_uv(tmp_path: Path, deepseek_env, updates) -> None:
    deepseek_env.update({name: value for name, value in updates.items() if value is not None})
    for name, value in updates.items():
        if value is None:
            deepseek_env.pop(name, None)
    completed = _run(tmp_path, deepseek_env, script=DEEPSEEK_SCRIPT)
    assert completed.returncode != 0
    assert _invocations(tmp_path) == []
    assert KEY not in completed.stdout + completed.stderr
    assert "unverified" in (DEEPSEEK_STATE / "gaps.txt").read_text()
    assert not (DEEPSEEK_STATE / "agent.json").exists()


@pytest.mark.parametrize("result", [
    None,
    {"agent": DEEPSEEK_AGENT, "total_cost_usd": 0, "tasks": [
        {"id": "single_step", "rep": 1, "success": True, "cost_usd": None,
         "cost_basis": {"unpriced": 1}, "tokens": {"input": 11, "output": 3}}]},
    {"agent": DEEPSEEK_AGENT, "total_cost_usd": 2.5, "tasks": [
        {"id": "single_step", "rep": 1, "success": True, "cost_usd": 2.5,
         "cost_basis": {"provider_price": 1}, "tokens": {"input": 11, "output": 3}}]},
    {"agent": "pi/cpa/gpt-6.1-sol", "total_cost_usd": 0.1, "tasks": []},
])
def test_deepseek_missing_unknown_or_exhausted_spend_stops_dialogs(tmp_path: Path, deepseek_env, result) -> None:
    if result is None:
        deepseek_env["PI_EVAL_FAKE_NO_RESULT"] = "1"
    else:
        deepseek_env["PI_EVAL_FAKE_RESULT"] = json.dumps(result)
    completed = _run(tmp_path, deepseek_env, script=DEEPSEEK_SCRIPT)
    assert completed.returncode != 0
    assert len(_invocations(tmp_path)) == 1
    gaps = (DEEPSEEK_STATE / "gaps.txt").read_text()
    assert "dialogs unverified" in gaps
    assert "catalog cost 0 is not a free run" in gaps
    if result and result.get("tasks") and result["tasks"][0].get("cost_usd") is None:
        assert "pricing unverified: single_step rep 1" in gaps


def test_deepseek_unavailable_runner_is_unverified(tmp_path: Path, deepseek_env) -> None:
    deepseek_env["PI_EVAL_FAKE_UV_EXIT"] = "1"
    completed = _run(tmp_path, deepseek_env, script=DEEPSEEK_SCRIPT)
    assert completed.returncode != 0
    assert len(_invocations(tmp_path)) == 1
    gaps = (DEEPSEEK_STATE / "gaps.txt").read_text()
    assert "real readback: unverified" in gaps
    assert "dialogs unverified" in gaps


def test_deepseek_deadline_terminates_runner(tmp_path: Path, deepseek_env) -> None:
    deepseek_env.update(PI_EVAL_DEADLINE_SECONDS="5", PI_EVAL_FAKE_UV_SLEEP="60")
    started = time.monotonic()
    completed = _run(tmp_path, deepseek_env, script=DEEPSEEK_SCRIPT, timeout=60)
    assert time.monotonic() - started < 15
    assert completed.returncode == 124
    assert len(_invocations(tmp_path)) == 1
    assert "deadline" in (DEEPSEEK_STATE / "gaps.txt").read_text()


COMPARISON_SCRIPT = REPO / "examples/pi/eval-prompt-comparison.sh"
COMPARISON_STATE = REPO / ".local" / "pi-eval-prompt-comparison"
PLANNER_TASKS = "dependent_steps,room_interface,room_consult,operator_notes,planner_plan_file,vault_closeout_keeps,vault_closeout_nothing"


@pytest.fixture
def comparison_env(tmp_path: Path):
    env = _environment(tmp_path, ANTHROPIC_API_KEY=KEY)
    uv = tmp_path / "bin" / "uv"
    uv.write_text(FAKE_UV + '''
args = sys.argv[1:]
if "compare" not in args and os.environ.get("PI_EVAL_FAKE_NO_RESULT") != "1":
    phase = "dialogs" if "dialogs" in args else "tasks"
    flag = "--dialogs" if phase == "dialogs" else "--tasks"
    ids = args[args.index(flag) + 1].split(",")
    model = args[args.index("--model") + 1]
    body = {"mode": "real", "model": model, "agent": "claude-code/" + model,
            "total_cost_usd": 0.1, phase: [
                {"id": case, "rep": rep, "outcome": "pass", "cost_usd": 0.1 / (len(ids) * 2),
                 "planner_denials": 0, "planner_modified_files": 0,
                 "planner_edit_tool_paths": [], "planner_changed_files": [],
                 "tokens": {"input": 11, "output": 3}}
                for case in ids for rep in (1, 2)]}
    if phase == "dialogs":
        body["manager_model"] = args[args.index("--manager-model") + 1] if "--manager-model" in args else model
    override = os.environ.get("PI_EVAL_FAKE_COMPARISON_OVERRIDE")
    if override:
        body.update(json.loads(override))
    out = Path(args[args.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(body))
''', encoding="utf-8")
    if COMPARISON_STATE.exists():
        shutil.rmtree(COMPARISON_STATE)
    yield env
    if COMPARISON_STATE.exists():
        shutil.rmtree(COMPARISON_STATE)


def test_comparison_runs_only_affected_cases_at_documented_tiers(tmp_path: Path, comparison_env) -> None:
    baselines = REPO / "evals/prompt_regression/baselines"
    before = {p.name: p.read_bytes() for p in baselines.glob("*.json")}
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT)
    assert completed.returncode == 0, completed.stderr
    invocations = _invocations(tmp_path)
    runs = [r["argv"] for r in invocations if "compare" not in r["argv"]]
    assert len(runs) == 4
    assert [_flag(r, "--model") for r in runs] == ["haiku", "opus", "haiku", "haiku"]
    assert [_flag(r, "--tasks") for r in runs[:2]] == [TASKS, PLANNER_TASKS]
    assert [_flag(r, "--dialogs") for r in runs[2:]] == [DIALOGS, DIALOGS]
    assert _flag(runs[3], "--manager-model") == "opus"
    assert _flag(runs[3], "--manager-effort") == "xhigh"
    for index, argv in enumerate(runs):
        assert _flag(argv, "--mode") == "real"
        assert _flag(argv, "--reps") == "2"
        assert _flag(argv, "--jobs") == "4"
        assert float(_flag(argv, "--total-cap-usd")) == pytest.approx(float(TOTAL) - index * 0.1)
        assert _flag(argv, "--case-budget-usd") == CASE
        assert _flag(argv, "--max-budget-usd") == CASE
        assert _flag(argv, "--task-timeout" if index < 2 else "--timeout") == "180"
        assert _flag(argv, "--pg-url") == PG_URL
        assert "--register-agent" not in argv
        assert KEY not in " ".join(argv)
        assert Path(_flag(argv, "--out-dir")).is_relative_to(COMPARISON_STATE)
    comparisons = [r["argv"] for r in invocations if "compare" in r["argv"]]
    assert len(comparisons) == 4
    assert {Path(r[-1]).name for r in comparisons} == {
        "real-haiku.json", "real-opus.json", "dialogs-real-haiku.json", "dialogs-real-opus.json"}
    ledger = (COMPARISON_STATE / "latest" / "ledger.txt").read_text()
    for term in ("48", "planner_denials", "planner_modified_files", "positive control", "unverified",
                 "Responses", "DeepSeek", "runtime_notes", "context", "schema", "fake"):
        assert term in ledger
    assert all(r["telegram"] == "" for r in invocations)
    assert before == {p.name: p.read_bytes() for p in baselines.glob("*.json")}
    subprocess.run(["bash", "-n", str(COMPARISON_SCRIPT)], check=True, timeout=5)


@pytest.mark.parametrize("updates", [
    {"ANTHROPIC_API_KEY": None}, {"ANTHROPIC_API_KEY": " "},
    {"PI_EVAL_TOTAL_CAP_USD": None}, {"PI_EVAL_CASE_BUDGET_USD": None},
    {"PI_EVAL_TOTAL_CAP_USD": "nan"}, {"PI_EVAL_TOTAL_CAP_USD": "inf"},
    {"PI_EVAL_CASE_BUDGET_USD": "0"}, {"PI_EVAL_CASE_BUDGET_USD": "3"},
    {"PI_EVAL_DEADLINE_SECONDS": "1501"}, {"PI_EVAL_DEADLINE_SECONDS": "0"},
    {"PI_EVAL_DEADLINE_SECONDS": "1.5"},
    {"LOOPER_EVAL_DATABASE_URL": None},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://db.example/postgres"},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://127.0.0.1/loopx"},
    {"LOOPER_EVAL_DATABASE_URL": "postgresql://127.0.0.1/looper"},
])
def test_comparison_preflight_records_unverified_without_uv(tmp_path: Path, comparison_env, updates) -> None:
    for key, value in updates.items():
        if value is None:
            comparison_env.pop(key, None)
        else:
            comparison_env[key] = value
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT)
    assert completed.returncode != 0
    assert _invocations(tmp_path) == []
    assert KEY not in completed.stdout + completed.stderr
    assert "unverified" in (COMPARISON_STATE / "latest" / "ledger.txt").read_text()


@pytest.mark.parametrize("override", [None, {"mode": "fake"}, {"total_cost_usd": None},
                                       {"total_cost_usd": 2.5}, {"tasks": []}])
def test_comparison_missing_fake_or_exhausted_readback_stops_next_tier(tmp_path: Path, comparison_env, override) -> None:
    if override is None:
        comparison_env["PI_EVAL_FAKE_NO_RESULT"] = "1"
    else:
        comparison_env["PI_EVAL_FAKE_COMPARISON_OVERRIDE"] = json.dumps(override)
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT)
    assert completed.returncode != 0
    runs = [r for r in _invocations(tmp_path) if "compare" not in r["argv"]]
    assert len(runs) == 1
    assert "unverified" in (COMPARISON_STATE / "latest" / "ledger.txt").read_text()


def test_comparison_deadline_kills_process_group(tmp_path: Path, comparison_env) -> None:
    comparison_env.update(PI_EVAL_DEADLINE_SECONDS="5", PI_EVAL_FAKE_UV_SLEEP="60")
    started = time.monotonic()
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT, timeout=60)
    assert time.monotonic() - started < 15
    assert completed.returncode == 124
    assert len(_invocations(tmp_path)) == 1
    assert "deadline" in (COMPARISON_STATE / "latest" / "ledger.txt").read_text()


def _pi_comparison_report(path: Path, provider: str, phase: str) -> None:
    ids = (TASKS + ",operator_notes_stale") if phase == "tasks" else DIALOGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": "real", "agent": provider, "total_cost_usd": 0.1,
        phase: [{"id": case, "rep": rep, "outcome": "pass", "planner_denials": 1,
                 "planner_modified_files": 0} for case in ids.split(",") for rep in (1, 2)]}))


def test_comparison_keeps_independent_pi_profiles_and_same_case_reps(tmp_path: Path, comparison_env) -> None:
    for provider, agent in (("RESPONSES", "pi/cpa/gpt-6.1-sol"), ("DEEPSEEK", DEEPSEEK_AGENT)):
        for phase in ("tasks", "dialogs"):
            path = COMPARISON_STATE / f"{provider}-{phase}.json"
            _pi_comparison_report(path, agent, phase)
            comparison_env[f"PI_EVAL_{provider}_{phase.upper()}_JSON"] = str(path)
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT)
    assert completed.returncode == 0, completed.stderr
    comparisons = [r["argv"] for r in _invocations(tmp_path) if "compare" in r["argv"]]
    assert len(comparisons) == 12
    pi_pairs = [argv for argv in comparisons if "-selected.json" in argv[-2]]
    assert len(pi_pairs) == 8
    for argv in pi_pairs:
        pi_run, claude = (json.loads(Path(path).read_text()) for path in argv[-2:])
        phase = "dialogs" if "dialogs" in claude else "tasks"
        assert {(r["id"], r["rep"]) for r in pi_run[phase]} == {(r["id"], r["rep"]) for r in claude[phase]}
        assert "operator_notes_stale" not in {r["id"] for r in pi_run[phase]}
    ledger = (COMPARISON_STATE / "latest/ledger.txt").read_text()
    assert "planner_denials=1" in ledger


@pytest.mark.parametrize("mode,agent", [("fake", DEEPSEEK_AGENT), ("real", "pi/cpa/gpt-6.1-sol")])
def test_comparison_rejects_fake_or_wrong_provider_input(tmp_path: Path, comparison_env, mode, agent) -> None:
    path = COMPARISON_STATE / "prior.json"
    _pi_comparison_report(path, agent, "tasks")
    body = json.loads(path.read_text())
    body["mode"] = mode
    path.write_text(json.dumps(body))
    comparison_env["PI_EVAL_DEEPSEEK_TASKS_JSON"] = str(path)
    completed = _run(tmp_path, comparison_env, script=COMPARISON_SCRIPT)
    assert completed.returncode == 0, completed.stderr
    assert len([r for r in _invocations(tmp_path) if "compare" in r["argv"]]) == 4
    assert "DeepSeek: unverified" in (COMPARISON_STATE / "latest/ledger.txt").read_text()


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("denied", [False, True])
def test_comparison_planner_edit_positive_and_denial_controls(tmp_path: Path, tool: str, denied: bool) -> None:
    """Exercise actual event translation and task metrics, with no diff to mask a missed call."""
    from evals.prompt_regression import metrics
    from looper.agents.runtimes.pi import PiStream

    stream = PiStream(model="selected")
    raw = [
        {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "edit-1", "name": tool, "arguments": {"path": "PLAN.md"}}]}},
        {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "edit-1",
            "isError": denied, "content": [{"type": "text", "text": "denied" if denied else "done"}]}},
    ]
    events = [{"event": event} for row in raw for event in stream.translate(row)]
    record = {"id": 1, "agent_id": "planner-task-1", "metadata": {
        "permission_denials": [{"tool": tool, "id": "edit-1"}] if denied else []}}
    result = metrics.task_metrics(detail={"status": "completed", "steps": []}, activity=[], records=[record],
        events={1: events}, changes={}, work_dir=tmp_path, check_problem=None,
        expect={"max_planner_modified_files": 0}, now=0.0)
    assert result["planner_denials"] == int(denied)
    assert result["planner_modified_files"] == int(not denied)
    assert result["planner_edit_tool_paths"] == ([] if denied else ["PLAN.md"])
    assert result["problems"] == ([] if denied else ["planner_modified_files 1 > 0"])
