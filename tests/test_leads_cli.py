"""Lead CLI input validation and refusal with the environment of an actual agent launch."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from looper import cli
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.profile_registry import builtin_resolved_profile


@pytest.mark.parametrize("argv", [
    ["search", "--limit", "0"], ["search", "--limit", "51"],
    ["search", "--days", "0"], ["search", "--days", "3651"],
    ["search", "--since", "tomorrow"], ["search", "--since", "2026-02-30"],
    ["mark", "one", "--inaccurate"], ["delete", "not-a-lead"],
    ["mark", "1", "--inaccurate", "--note", "x" * 501],
    ["search", "--kind", "unknown"], ["search", "--state", "unknown"],
    ["eval-export", "--out", "x.jsonl", "--sample", "-1"],
])
def test_invalid_input_precedes_config_and_database(monkeypatch, argv):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    def no_config(args):
        pytest.fail("invalid CLI input must not read config or connect to PostgreSQL")
    monkeypatch.setattr(cli, "_load_ops", no_config)
    try:
        code = cli.main(["leads", *argv])
    except SystemExit as error:
        code = error.code
    assert code == 2


@pytest.mark.parametrize("argv", [["search"], ["mark", "1", "--inaccurate"], ["delete", "1"],
                                  ["eval-export", "--out", "set.jsonl"]])
def test_real_launch_environment_refuses_leads_cli(tmp_path, argv):
    env = Launch(AgentSettings(profile=builtin_resolved_profile()), role=AgentType.WORKER).environment()
    if not env.get("LOOPER_AGENT_RUN"):
        pytest.skip("Requires #1451: Launch must set LOOPER_AGENT_RUN on agent processes")
    env["HOME"] = str(tmp_path)
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root), env.get("PYTHONPATH"))))
    result = subprocess.run(
        [sys.executable, "-m", "looper.cli", "leads", *argv, "--config", str(tmp_path / "missing.toml"),
         "--runtime-root", str(tmp_path / "runtime")],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 4, result.stderr
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("options", [
    ["--chat", "synthetic"], ["--days", "7"], ["--intents"],
    ["--chat", "synthetic", "--intents"],
    ["--chat", "synthetic", "--days", "31", "--intents"],
])
def test_intents_eval_export_validates_the_complete_option_group_before_connecting(monkeypatch, options):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    monkeypatch.setattr(cli, "_load_ops", lambda _: pytest.fail("invalid options must not connect"))
    assert cli.main(["leads", "eval-export", "--out", "set.jsonl", *options]) == 2
