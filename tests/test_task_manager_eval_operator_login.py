"""``--credential operator-login`` of the task Manager eval: no token, the operator's own login. Fake mode and unit
tests only; no model is called. Every "operator home" here is a temporary directory."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from evals.prompt_regression import task_manager_cases as cases
from evals.prompt_regression import task_manager_eval as evaluation
from evals.prompt_regression.runner import EvalError
from looper.agents import task_manager_sandbox
from tests.db_support import DSN

SECRET = "sk-test-not-a-real-secret-value"
SETTINGS = {"env": {"MARKER_IN_SETTINGS": "settings-secret-marker"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hook-secret-marker"}]}]}}
MEMORY = "memory-secret-marker\n"
HOSTILE = {"foreign_task_refused", "readonly_progress"}  # of the cases on this checkout


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def operator(monkeypatch, tmp_path):
    """A synthetic operator: a home with user settings and memory, no configuration directory variable."""
    home = tmp_path / "operator-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(SETTINGS))
    (home / ".claude" / "CLAUDE.md").write_text(MEMORY)
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(task_manager_sandbox, "SUPPORTED_PLATFORM", sys.platform)
    return home


# --- The choice and the environment -----------------------------------------------------------------------------


class Untouchable(dict):
    def get(self, *args):
        raise AssertionError("a variable was read")

    __getitem__ = get


def test_operator_login_is_an_explicit_choice_that_reads_no_variable():
    assert evaluation.resolve_credential("operator-login", Untouchable()) is None
    with pytest.raises(EvalError, match="operator-login"):
        evaluation.resolve_credential(None, {"CLAUDE_CODE_OAUTH_TOKEN": SECRET})
    for choice, name in (("oauth", "CLAUDE_CODE_OAUTH_TOKEN"), ("api-key", "ANTHROPIC_API_KEY")):
        with pytest.raises(EvalError, match=name):  # no fallback from a token mode to the operator's login
            evaluation.resolve_credential(choice, {})


@pytest.mark.parametrize("custom", [False, True])
def test_environment_keeps_home_and_configuration_and_isolates_the_rest(operator, monkeypatch, tmp_path, custom):
    if custom:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "operator-config"))
    planted = {"SSH_AUTH_SOCK": "/tmp/agent.sock", "GH_TOKEN": "x", "ANTHROPIC_API_KEY": SECRET,
               "ANTHROPIC_AUTH_TOKEN": SECRET, "CLAUDE_CODE_OAUTH_TOKEN": SECRET, "XDG_CONFIG_HOME": "/operator/xdg",
               "LOOPER_ANYTHING": "x", "GIT_CONFIG_GLOBAL": "/operator/gitconfig", "ANTHROPIC_BASE_URL": "x"}
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    with evaluation.isolated_environment(tmp_path / "case", None, operator_login=True) as home:
        assert home == operator and os.environ["HOME"] == str(operator)
        assert os.environ.get("CLAUDE_CONFIG_DIR") == (str(tmp_path / "operator-config") if custom else None)
        assert os.environ["PATH"] == evaluation.MINIMAL_PATH
        assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull and os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
        extra = set(os.environ) - evaluation.KEPT_VARIABLES - {
            "HOME", "PATH", "CLAUDE_CONFIG_DIR", "GIT_TERMINAL_PROMPT", "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"} - {n for n in os.environ if n.startswith("LC_")}
        assert not extra, extra
    assert dict(os.environ) == before
    assert not (tmp_path / "case" / "home").exists() and not (tmp_path / "case" / "claude-config").exists()
    assert (operator / ".claude" / "settings.json").read_text() == json.dumps(SETTINGS)  # no synthetic layer


def test_operator_login_takes_no_credential_and_needs_a_home(operator, monkeypatch, tmp_path):
    with pytest.raises(EvalError, match="no credential"):
        with evaluation.isolated_environment(tmp_path / "a", ("ANTHROPIC_API_KEY", SECRET), operator_login=True):
            pass
    monkeypatch.delenv("HOME")
    with pytest.raises(EvalError, match="HOME"):
        with evaluation.isolated_environment(tmp_path / "b", None, operator_login=True):
            pass


# --- What the report records ------------------------------------------------------------------------------------


def test_fingerprint_hashes_the_three_user_level_inputs_and_prints_only_hashes(operator, monkeypatch, tmp_path):
    found = evaluation.config_fingerprint(os.environ, "1.2.3")
    hooks = json.dumps(SETTINGS["hooks"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert found == {"cli_version": "1.2.3",
                     "user_settings_sha256": sha(json.dumps(SETTINGS).encode()),
                     "user_hooks_sha256": sha(hooks), "user_claude_md_sha256": sha(MEMORY.encode())}
    assert "secret-marker" not in json.dumps(found)
    # The configuration directory the operator uses is the one hashed.
    other = tmp_path / "other-config"
    other.mkdir()
    (other / "settings.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    moved = evaluation.config_fingerprint(os.environ, "1.2.3")
    assert moved["user_settings_sha256"] == sha(b"{}")
    assert moved["user_hooks_sha256"] == "absent" and moved["user_claude_md_sha256"] == "absent"
    (other / "settings.json").write_text("not json")
    assert evaluation.config_fingerprint(os.environ, "1")["user_hooks_sha256"] == "unreadable"


def test_fingerprint_opens_no_other_file(operator, monkeypatch):
    for name in (".credentials.json", "settings.local.json"):
        (operator / ".claude" / name).write_text("never-read")
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self.name) or real(self))
    evaluation.config_fingerprint(os.environ, "1")
    assert sorted(set(opened)) == ["CLAUDE.md", "settings.json"]


def report(tmp_path, name, **operator_login):
    body = {"suite": "task-manager", "rows": [{"case": "direct_edit_brief", "rep": 1, "outcome": "pass"}]}
    if operator_login:
        body["operator_login"] = operator_login
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body))
    return path


def test_runs_with_different_hashes_are_not_directly_comparable(tmp_path):
    config = {"cli_version": "1", "user_settings_sha256": "a", "user_hooks_sha256": "b", "user_claude_md_sha256": "c"}
    base = report(tmp_path, "base", config=config, changed_during_run=False)
    same, text = evaluation.compare_runs(base, report(tmp_path, "same", config=config, changed_during_run=False))
    assert same and "not directly comparable" not in text and "direct_edit_brief" in text
    for key in config:
        other = report(tmp_path, key, config={**config, key: "z"}, changed_during_run=False)
        same, text = evaluation.compare_runs(base, other)
        assert not same and "not directly comparable" in text and key in text
    changed = report(tmp_path, "changed", config=config, changed_during_run=True)
    assert not evaluation.compare_runs(base, changed)[0]
    isolated = report(tmp_path, "isolated")
    assert not evaluation.compare_runs(base, isolated)[0] and evaluation.compare_runs(isolated, isolated)[0]
    unreadable = report(tmp_path, "unreadable", config={**config, "user_hooks_sha256": "unreadable"},
                        changed_during_run=False)
    assert not evaluation.compare_runs(unreadable, unreadable)[0]
    with pytest.raises(SystemExit) as exit_code:
        evaluation.main(["--compare-runs", str(base), str(changed)])
    assert exit_code.value.code == 1


# --- The staged order -------------------------------------------------------------------------------------------


def stub(monkeypatch, outcomes=None, abort_on=None, verified=1):
    started = []

    def run_case(case, rep, **kwargs):
        assert kwargs["operator_login"] is True and kwargs["credential"] is None
        started.append((case.id, rep))
        row = {"case": case.id, "rep": rep, "model": case.model, "cost_usd": 0.0,
               "outcome": (outcomes or {}).get(case.id, "pass"),
               "verified_direct_launches": verified if case.direct else 0}
        if case.id == abort_on:
            row.update(outcome="error", abort="launch_refused")
        return row

    monkeypatch.setattr(evaluation, "run_case", run_case)
    return started


def suite(tmp_path, **override):
    arguments = {"mode": "fake", "admin_url": "postgresql://nobody@127.0.0.1:1/never", "out_dir": tmp_path / "out",
                 "credential_choice": "operator-login", **override}
    return evaluation.run_suite(**arguments)


def test_every_non_hostile_case_of_every_repetition_runs_before_a_hostile_one(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    result, _ = suite(tmp_path, reps=2)
    ids = [case for case, _ in started]
    first = len(ids) - 2 * len(HOSTILE)
    assert set(ids[first:]) == HOSTILE and not set(ids[:first]) & HOSTILE and first == 10
    assert ids[0] == "direct_edit_brief" and ids[first - 1] == "readonly_resume"  # read-back launches first
    assert set(result["operator_login"]["hostile_cases"]) == HOSTILE


@pytest.mark.parametrize("outcome", ["fail", "unparsed", "error"])
def test_a_stage_one_case_that_does_not_pass_keeps_every_hostile_case_from_starting(operator, monkeypatch, tmp_path,
                                                                                     outcome):
    started = stub(monkeypatch, outcomes={"readonly_resume": outcome})
    result, _ = suite(tmp_path)
    assert not {case for case, _ in started} & HOSTILE
    held = {row["case"]: row for row in result["rows"] if row.get("blocker") == "stage_gate"}
    assert set(held) == HOSTILE and all(row["outcome"] == "not_runnable" for row in held.values())
    assert result["outcome"] != "pass"


def test_hostile_cases_need_a_read_back_launch_first(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch, verified=0)
    suite(tmp_path)
    assert not {case for case, _ in started} & HOSTILE
    for selected in (["foreign_task_refused"], ["readonly_resume", "foreign_task_refused"]):
        started.clear()
        with pytest.raises(EvalError, match="non-hostile"):
            suite(tmp_path, selected=[cases.BY_ID[name] for name in selected])
        assert started == []


def test_a_readback_refusal_aborts_the_whole_run(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch, abort_on="stop_continue")
    result, path = suite(tmp_path, reps=2)
    assert started == [("direct_edit_brief", 1), ("stop_continue", 1)]
    assert result["outcome"] == "incomplete" and result["aborted"] == "launch_refused in stop_continue"
    assert json.loads(path.read_text())["aborted"] == result["aborted"]


def test_a_new_case_is_hostile_until_it_is_listed(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    new = dataclasses.replace(cases.BY_ID["direct_edit_brief"], id="project_instructions_hostile")
    suite(tmp_path, selected=[new, cases.BY_ID["direct_edit_brief"]])
    assert [case for case, _ in started] == ["direct_edit_brief", "project_instructions_hostile"]


def test_a_case_whose_canaries_sit_in_credential_paths_cannot_run(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    live = dataclasses.replace(cases.BY_ID["direct_edit_brief"], id="read_deny_live")
    result, _ = suite(tmp_path, selected=[live, cases.BY_ID["direct_edit_brief"]])
    assert [case for case, _ in started] == ["direct_edit_brief"]
    row = next(row for row in result["rows"] if row["case"] == "read_deny_live")
    assert row["outcome"] == "not_runnable" and row["blocker"] == "needs_synthetic_home"


def test_refusals_before_any_case(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    for override, message in (({"behavior": "bad"}, "scripted"),
                              ({"out_dir": evaluation.REPO_ROOT / "evals" / "x"}, "ignored")):
        with pytest.raises(EvalError, match=message):
            suite(tmp_path, **override)
    assert started == []


def test_the_report_holds_hashes_and_no_free_text(operator, monkeypatch, tmp_path):
    def run_case(case, rep, **kwargs):
        (operator / ".claude" / "CLAUDE.md").write_text("changed during the run")
        return {"case": case.id, "rep": rep, "model": case.model, "outcome": "error", "cost_usd": 0.0,
                "reason": "RuntimeError: reason-marker", "observations": {"x": "observation-marker"},
                "checks": [{"name": "named", "ok": False, "detail": "detail-marker"}],
                "evidence": str(operator / "somewhere")}

    monkeypatch.setattr(evaluation, "run_case", run_case)
    result, path = suite(tmp_path, selected=[cases.BY_ID["direct_edit_brief"]])
    text = path.read_text()
    assert "-marker" not in text and str(operator) not in text and str(tmp_path) not in text
    saved = json.loads(text)
    assert saved["rows"][0]["checks"] == [{"name": "named", "ok": False}]
    assert saved["rows"][0]["reason"] == "RuntimeError" and saved["rows"][0]["evidence"] == "~/somewhere"
    section = saved["operator_login"]
    assert section["config"]["user_claude_md_sha256"] == sha(MEMORY.encode())
    assert section["config"]["cli_version"].startswith("fake ") and section["changed_during_run"] is True
    assert section["note"] == "results depend on this machine's configuration"
    local = (path.parent / "task-manager-eval.local.json").read_text()
    assert "detail-marker" in local and "secret-marker" not in local


def test_real_mode_hands_no_credential_to_a_case_and_records_the_verified_version(operator, monkeypatch, tmp_path):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho '2.1.332 (Claude Code)'\n")
    claude.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    started = stub(monkeypatch)  # asserts operator_login and credential None; nothing real runs
    result, path = suite(tmp_path, mode="real", claude_bin=str(claude), total_cap_usd=2, case_budget_usd=1.25,
                         selected=[cases.BY_ID["direct_edit_brief"]])
    assert started == [("direct_edit_brief", 1)] and result["operator_login"]["config"]["cli_version"] == "2.1.332"
    assert SECRET not in path.read_text()


# --- The launch check -------------------------------------------------------------------------------------------


def test_a_bypass_launch_must_carry_the_sandbox_and_the_deny_list(operator, tmp_path):
    deny = list(task_manager_sandbox.read_deny_paths(os.environ, tmp_path / "runtime"))
    good = {"args": ["--dangerously-skip-permissions"], "settings": {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": deny}}}}
    assert evaluation.launch_problem(good, os.environ) is None
    assert evaluation.launch_problem({"args": ["--strict-mcp-config"], "settings": None}, os.environ) is None

    def broken(change):
        launch = json.loads(json.dumps(good))
        change(launch["settings"]["sandbox"])
        return evaluation.launch_problem(launch, os.environ)

    assert "enabled" in broken(lambda s: s.update(enabled=False))
    assert "failIfUnavailable" in broken(lambda s: s.pop("failIfUnavailable"))
    assert "allowUnsandboxedCommands" in broken(lambda s: s.update(allowUnsandboxedCommands=True))
    for name in (".ssh", ".claude", ".claude.json", "Library/Keychains"):
        gone = os.path.realpath(operator / name)
        assert name in broken(lambda s: s["filesystem"].update(denyRead=[p for p in deny if p != gone]))


# --- On the isolated service (fake CLI) -------------------------------------------------------------------------


@pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
def test_fake_run_passes_both_stages_and_a_readback_mismatch_aborts_it(operator, monkeypatch, tmp_path):
    monkeypatch.setattr(task_manager_sandbox, "SUPPORTED_PLATFORM", "darwin")
    selected = [cases.BY_ID[name] for name in ("foreign_task_refused", "direct_edit_brief", "readonly_resume")]
    result, path = suite(tmp_path, admin_url=DSN, selected=selected)
    assert [(row["case"], row["outcome"]) for row in result["rows"]] == [
        ("direct_edit_brief", "pass"), ("readonly_resume", "pass"), ("foreign_task_refused", "pass")], result["rows"]
    assert result["rows"][0]["verified_direct_launches"] >= 1
    assert result["operator_login"]["changed_during_run"] is False
    evidence = json.loads((path.parent / "direct_edit_brief-1" / "evidence.json").read_text())
    for launch in evidence["launches"]:
        assert "CLAUDE_CONFIG_DIR" not in launch["env_names"]
    for file in path.parent.rglob("*"):
        if file.is_file():
            assert "secret-marker" not in file.read_text(errors="replace"), file

    # The CLI reports a sandbox that is not the launch's: Looper refuses the turn and the eval stops everything.
    override = tmp_path / "readback.json"
    override.write_text(json.dumps({"get_settings": {"effective": {"sandbox": {"enabled": False}}}}))
    write = evaluation.Run.write_cli_config

    def mismatching(run):
        write(run)
        config = json.loads((run.root / "cli.json").read_text())
        (run.root / "cli.json").write_text(json.dumps({**config, "readback_override": str(override)}))

    monkeypatch.setattr(evaluation.Run, "write_cli_config", mismatching)
    result, _ = suite(tmp_path, admin_url=DSN, selected=selected, out_dir=tmp_path / "mismatch")
    assert result["aborted"] == "launch_refused in direct_edit_brief"
    assert [row["case"] for row in result["rows"]] == ["direct_edit_brief"]
    assert result["outcome"] == "incomplete"
