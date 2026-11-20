"""The evals choose their agent's runtime and model by flags, and need the `claude` binary only for Claude Code."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import dsh_consult_replay, lark_quote, stop_realpath
from evals.prompt_regression import runner

CODEX = {"runtime": {"type": "codex"}, "provider": {"provider_id": "prov-codex-login"},
         "model": {"name": "gpt-6.1-sol", "protocol": "openai-responses"}, "agent": {"name": "codex-eval"}}
CLAUDE = {**CODEX, "runtime": {"type": "claude-code"}}


@pytest.mark.parametrize(("kwargs", "types"), [
    ({}, {"claude-code"}),
    ({"model": "haiku"}, {"claude-code"}),
    ({"register": CODEX, "model": "haiku"}, {"codex"}),
    ({"register": CLAUDE}, {"claude-code"}),
    ({"agent": "codex/gpt"}, set()),
    ({"agent": "codex/gpt", "manager_model": "opus"}, {"claude-code"}),
    ({"register": CODEX, "specs": (CLAUDE, None)}, {"codex", "claude-code"}),
    ({"register": {"runtime": {"name": "mine"}}}, set()),
], ids=["builtin", "model", "codex-spec", "claude-spec", "named-agent", "manager-model", "other-specs", "by-name"])
def test_planned_runtime_types(kwargs, types):
    assert runner.planned_runtime_types(**kwargs) == types


@pytest.mark.parametrize(("mode", "claude", "types", "refused"), [
    ("real", None, {"claude-code"}, True),
    ("real", None, {"codex"}, False),
    ("real", None, set(), False),
    ("real", "/bin/claude", {"claude-code"}, False),
    ("fake", None, {"claude-code"}, False),
])
def test_claude_is_required_only_for_a_claude_code_agent_in_real_mode(mode, claude, types, refused):
    if refused:
        with pytest.raises(runner.EvalError, match="Claude Code agent needs the `claude` CLI"):
            runner.require_claude(mode, claude, types)
    else:
        runner.require_claude(mode, claude, types)


def test_real_mode_installs_no_claude_stand_in_when_no_agent_runs_it(tmp_path):
    assert runner.install_claude(tmp_path / "none", mode="real", real_claude=None) is None
    assert not (tmp_path / "none" / "claude").exists()
    installed = runner.install_claude(tmp_path / "real", mode="real", real_claude="/bin/claude")
    assert installed == tmp_path / "real" / "claude" and "/bin/claude" in installed.read_text()


def test_the_real_claude_wrapper_adds_the_eval_cap_only_to_a_call_without_its_own(tmp_path):
    import subprocess

    echo = tmp_path / "echo-args"
    echo.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    echo.chmod(0o755)
    wrapper = runner.install_claude(tmp_path / "bin", mode="real", real_claude=str(echo), max_budget_usd=0.25)

    def args(*given: str) -> list[str]:
        return subprocess.run([str(wrapper), *given], capture_output=True, text=True, check=True).stdout.split()

    assert args("--print", "--model", "sonnet") == ["--print", "--model", "sonnet", "--max-budget-usd", "0.25"]
    # a spawned run's cap (the advisor's 1 USD consult budget) is not lowered by the eval's
    assert args("--print", "--max-budget-usd", "0.9994") == ["--print", "--max-budget-usd", "0.9994"]


def test_stop_realpath_defaults_and_agent_flags(tmp_path):
    parsed = stop_realpath.parse_args(["--runtime", "claude"])
    assert (parsed.claude_model, parsed.deepseek_model, parsed.spec) == ("sonnet", "deepseek-flash", None)
    omp = tmp_path / "omp.json"
    omp.write_text(json.dumps({**CODEX, "runtime": {"type": "omp"}}))
    assert stop_realpath.parse_args(["--runtime", "omp", "--register-agent", str(omp)]).spec["runtime"] == {"type": "omp"}
    with pytest.raises(SystemExit):  # a codex agent does not fit the claude cells
        spec = tmp_path / "codex.json"
        spec.write_text(json.dumps(CODEX))
        stop_realpath.parse_args(["--runtime", "claude", "--register-agent", str(spec)])


def test_stop_realpath_sets_the_chosen_models_profile(monkeypatch):
    seen = []

    class Api:
        def post(self, path, body):
            return {"result": {}}

    monkeypatch.setattr(stop_realpath.runner, "register_agent",
                        lambda api, spec: seen.append(spec) or {"profile_id": "p"})
    stop_realpath._set_profile(Api(), "claude", claude_model="opus")
    stop_realpath._set_profile(Api(), "claude", spec=CLAUDE)
    assert seen[0]["model"]["name"] == "opus" and seen[1] is CLAUDE


def _lark_main(monkeypatch, tmp_path, *flags):
    monkeypatch.setattr(sys, "argv", ["lark_quote", "--mode", "real", "--pg-url", "postgresql://x/y",
                                      "--out", str(tmp_path / "out.json"), *flags])
    monkeypatch.setattr(lark_quote, "run_once", lambda args, stand_in, root: {
        "cases": {key: {"passed": True} for key in lark_quote.CASES}, "unaddressed_read": False,
        "seen": [args.model, args.agent, args.spec]})
    return lark_quote.main()


def test_lark_quote_needs_claude_only_for_a_claude_code_agent(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _lark_main(monkeypatch, tmp_path, "--claude", "")
    assert "needs the `claude` CLI" in capsys.readouterr().err
    spec = tmp_path / "codex.json"
    spec.write_text(json.dumps(CODEX))
    assert _lark_main(monkeypatch, tmp_path, "--claude", "", "--register-agent", str(spec)) == 0
    assert json.loads((tmp_path / "out.json").read_text())["agent"] == "codex-eval"
    assert _lark_main(monkeypatch, tmp_path, "--claude", "", "--agent", "codex/gpt") == 0
    with pytest.raises(SystemExit):  # the Manager's own model is Claude Code
        _lark_main(monkeypatch, tmp_path, "--claude", "", "--agent", "codex/gpt", "--manager-model", "opus")
    assert _lark_main(monkeypatch, tmp_path, "--claude", "/bin/claude") == 0
    assert json.loads((tmp_path / "out.json").read_text())["model"] == "sonnet"


def test_dsh_consult_replay_model_and_effort_flags(monkeypatch):
    seen = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(dsh_consult_replay, "replay", lambda args: seen.append((args.model, args.effort)) or _noop())
    for flags in ([], ["--model", "deepseek-pro", "--effort", "max"]):
        monkeypatch.setattr(sys, "argv", ["replay", "--capture", "c", "--out", "o", *flags])
        dsh_consult_replay.main()
    assert seen == [("deepseek-flash", "high"), ("deepseek-pro", "max")]


async def _noop():
    return None


def test_lark_quote_register_agent_keeps_the_per_run_budget_cap(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    posts = []

    class Api:
        def post(self, path, body):
            posts.append((path, body))
            return {"result": {}}

    @contextmanager
    def server(**kwargs):
        kwargs["log_path"].write_text("")
        yield Api()

    @contextmanager
    def database(url):
        yield "postgresql://x/y", None

    runner = SimpleNamespace(
        install_claude=lambda *a, **k: None, fresh_database=database, running_server=server,
        register_agent=lambda api, spec: {"name": "codex-eval"},
        select_agent=lambda api, agent, model: {"profile_id": "p-codex", "runtime_type": "codex"},
        require_claude=lambda *a: None)
    args = SimpleNamespace(mode="real", spec=CODEX, agent=None, model="sonnet", manager_model=None, port=0,
                           timeout=0, pg_url="x", claude=None, max_budget_usd=2.5, runner=runner, dialogs=None)
    monkeypatch.setattr(lark_quote.asyncio, "run", lambda coro: coro.close() or {
        "tasks": 0, "manager_writes": [], "cost_usd": 0.0})
    monkeypatch.setattr(lark_quote.time, "sleep", lambda s: None)
    stand_in = importlib.import_module("lark_cli_stand_in")
    lark_quote.run_once(args, stand_in, tmp_path)
    assert ("/api/v1/agent_settings/update_system",
            {"values": {"profile_id": "p-codex", "run_budget_usd": 2.5}}) in posts


def test_lark_quote_checkout_validates_with_the_current_helpers(tmp_path):
    old = tmp_path / "old"  # an old checkout: its runner has none of the flag helpers
    (old / "evals" / "prompt_regression").mkdir(parents=True)
    (old / "evals" / "__init__.py").write_text("")
    (old / "evals" / "prompt_regression" / "__init__.py").write_text("")
    (old / "evals" / "prompt_regression" / "runner.py").write_text("REPO_ROOT = None\n")
    done = subprocess.run(
        [sys.executable, "-m", "evals.lark_quote", "--mode", "real", "--pg-url", "postgresql://x/y",
         "--claude", "", "--checkout", str(old)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120)
    assert done.returncode == 2 and "needs the `claude` CLI" in done.stderr
    assert "AttributeError" not in done.stderr
