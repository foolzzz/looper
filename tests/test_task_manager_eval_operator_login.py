"""``--credential operator-login`` of the task Manager eval: no token, the operator's own login. Fake mode and unit
tests only; no model is called. Every "operator home" here is a temporary directory."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from evals.prompt_regression import task_manager_cases as cases
from evals.prompt_regression import task_manager_eval as evaluation
from evals.prompt_regression.runner import EvalError
from looper import child_env
from looper.agents import task_manager_edit, task_manager_sandbox
from tests.db_support import DSN

SECRET = "sk-test-not-a-real-secret-value"
SETTINGS = {"env": {"MARKER_IN_SETTINGS": "settings-secret-marker"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hook-secret-marker"}]}]}}
MEMORY = "memory-secret-marker\n"
# The hostile cases of this checkout that an operator-login run can start (``read_deny_live`` plants files under HOME).
HOSTILE = {"foreign_task_refused", "readonly_progress", "project_instructions_hostile",
           "branch_kept_against_convention"}
EDIT_CASES = {f"{name}{arm}" for name in ("edit_multi_file", "edit_large_file_local", "edit_retry_after_mismatch")
              for arm in ("", "_bash")}


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
    monkeypatch.setattr(evaluation, "MANAGED_SETTINGS", (tmp_path / "managed" / "managed-settings.json",),
                        raising=False)
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
               "ANTHROPIC_AUTH_TOKEN": SECRET, "CLAUDE_CODE_OAUTH_TOKEN": SECRET, "XDG_CACHE_HOME": "/operator/cache",
               "LOOPER_ANYTHING": "x", "GIT_CONFIG_GLOBAL": "/operator/gitconfig", "ANTHROPIC_BASE_URL": "x",
               "XDG_DATA_HOME": "/operator/data"}
    logins = {name: f"/operator/{name.lower()}" for name in
              ("XDG_CONFIG_HOME", "CODEX_HOME", "GROK_HOME", "TRAE_HOME", "COPILOT_HOME")}
    planted.update(logins)
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    with evaluation.isolated_environment(tmp_path / "case", None, operator_login=True) as home:
        assert home == operator and os.environ["HOME"] == str(operator)
        assert os.environ.get("CLAUDE_CONFIG_DIR") == (str(tmp_path / "operator-config") if custom else None)
        assert os.environ["PATH"] == evaluation.MINIMAL_PATH
        root = tmp_path / "case"
        assert os.environ["GIT_CONFIG_GLOBAL"] == str(root / "gitconfig") and os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
        assert os.environ["TMPDIR"] == str(root / "tmp") and os.environ["XDG_CACHE_HOME"] == str(root / "cache")
        assert {name: os.environ[name] for name in logins} == logins  # the read deny list follows them
        extra = set(os.environ) - evaluation.KEPT_VARIABLES - set(logins) - {
            "HOME", "PATH", "CLAUDE_CONFIG_DIR", "GIT_TERMINAL_PROMPT", "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "XDG_CACHE_HOME"} - {
            n for n in os.environ if n.startswith("LC_")}
        assert not extra, extra
    assert dict(os.environ) == before
    assert not (tmp_path / "case" / "home").exists() and not (tmp_path / "case" / "claude-config").exists()
    assert (operator / ".claude" / "settings.json").read_text() == json.dumps(SETTINGS)  # no synthetic layer


def test_custom_login_homes_reach_the_read_deny_list(operator, monkeypatch, tmp_path):
    custom = {name: str(tmp_path / name.lower()) for name in ("XDG_CONFIG_HOME", "CODEX_HOME", "GROK_HOME")}
    for name, value in custom.items():
        monkeypatch.setenv(name, value)
    with evaluation.isolated_environment(tmp_path / "case", None, operator_login=True):
        denied = task_manager_sandbox.read_deny_paths(os.environ, tmp_path / "case" / "runtime")
    assert os.path.realpath(custom["XDG_CONFIG_HOME"] + "/gh") in denied
    assert os.path.realpath(custom["CODEX_HOME"]) in denied and os.path.realpath(custom["GROK_HOME"]) in denied
    # Default modes still drop them.
    with evaluation.isolated_environment(tmp_path / "other", None):
        assert not set(custom) & set(os.environ)


def test_git_reads_neither_the_user_ignore_nor_attributes_files(operator, tmp_path):
    (operator / ".config" / "git").mkdir(parents=True)
    (operator / ".config" / "git" / "ignore").write_text("secret.txt\n")
    (operator / ".config" / "git" / "attributes").write_text("*.txt marked\n")
    work = tmp_path / "work"
    work.mkdir()
    with evaluation.isolated_environment(tmp_path / "case", None, operator_login=True):
        def git(*args):
            return subprocess.run(["git", *args], cwd=work, capture_output=True, text=True, check=True).stdout

        git("init", "-q")
        (work / "secret.txt").write_text("x")
        assert "secret.txt" in git("status", "--porcelain")
        assert "marked" not in git("check-attr", "-a", "secret.txt")


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
    absent = {key: "absent" for key in ("user_claude_md_imports_sha256", "user_settings_local_sha256",
                                        "managed_settings_sha256", "plugin_list_sha256", "skill_list_sha256")}
    assert found == {"cli_version": "1.2.3",
                     "user_settings_sha256": sha(json.dumps(SETTINGS).encode()),
                     "user_hooks_sha256": sha(hooks), "user_claude_md_sha256": sha(MEMORY.encode()), **absent}
    assert tuple(found) == evaluation.HASHED
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


def configure(operator, tmp_path):
    """Every input the fingerprint covers, present."""
    claude = operator / ".claude"
    (operator / "notes").mkdir()
    (operator / "notes" / "shared.md").write_text("shared @../notes/deeper.md\n")
    (operator / "notes" / "deeper.md").write_text("deeper\n")
    (claude / "relative.md").write_text("relative\n")
    (claude / "CLAUDE.md").write_text("see @~/notes/shared.md and @relative.md.\n`@~/notes/in-code.md`\n"
                                      "```\n@~/notes/in-block.md\n```\nmail a@example.invalid\n")
    (claude / "settings.local.json").write_text("{}")
    (claude / "plugins").mkdir()
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"a@m": [{"version": "1", "gitCommitSha": "x", "lastUpdated": "t1"}]}}))
    (claude / "skills" / "one").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text("{}")
    return claude, managed


@pytest.mark.parametrize("change, key", [
    ("notes/shared.md", "user_claude_md_imports_sha256"),
    ("notes/deeper.md", "user_claude_md_imports_sha256"),
    (".claude/relative.md", "user_claude_md_imports_sha256"),
    (".claude/settings.local.json", "user_settings_local_sha256"),
    ("managed", "managed_settings_sha256"),
    ("managed-drop-in", "managed_settings_sha256"),
    ("plugin", "plugin_list_sha256"),
    ("skill", "skill_list_sha256"),
])
def test_every_covered_input_changes_its_hash_and_no_other(operator, tmp_path, change, key):
    claude, managed = configure(operator, tmp_path)
    before = evaluation.config_fingerprint(os.environ, "1")
    assert not {"absent", "unreadable"} & set(before.values())
    if change == "managed":
        (managed / "managed-settings.json").write_text('{"a": 1}')
    elif change == "managed-drop-in":
        (managed / "managed-settings.d").mkdir()
        (managed / "managed-settings.d" / "10.json").write_text("{}")
    elif change == "plugin":
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"a@m": [{"version": "2", "gitCommitSha": "x"}]}}))
    elif change == "skill":
        (claude / "skills" / "two").mkdir()
    else:
        (operator / change).write_text("changed @relative.md @~/notes/deeper.md @~/notes/shared.md\n"
                                       if change.endswith("shared.md") else "changed\n")
    after = evaluation.config_fingerprint(os.environ, "1")
    assert {name for name in before if before[name] != after[name]} == {key}


def test_a_plugin_timestamp_is_not_a_change(operator, tmp_path):
    claude, _ = configure(operator, tmp_path)
    before = evaluation.config_fingerprint(os.environ, "1")
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"a@m": [{"version": "1", "gitCommitSha": "x", "lastUpdated": "t2"}]}}))
    assert evaluation.config_fingerprint(os.environ, "1") == before


def test_imports_skip_code_and_name_what_they_could_not_cover(operator, tmp_path):
    claude, _ = configure(operator, tmp_path)
    (operator / "notes" / "in-code.md").write_text("never")
    (operator / "notes" / "in-block.md").write_text("never")
    (operator / ".claude.json").write_text("never")
    found, unresolved, unopened = evaluation.memory_imports(claude / "CLAUDE.md", operator)
    assert [written for written, _ in found] == ["~/notes/shared.md", "relative.md.", "../notes/deeper.md"]
    assert unresolved == unopened == 0
    (claude / "CLAUDE.md").write_text("@~/gone.md @~/.claude.json\n")
    assert evaluation.memory_imports(claude / "CLAUDE.md", operator)[1:] == (1, 1)
    listed = "\n".join(evaluation.not_covered(os.environ))
    assert "resolve to no file" in listed and "login file" in listed
    for word in ("plugins and skills", "rules", "managed CLAUDE.md", ".claude.json", "hooks", "read deny list"):
        assert word in listed


def test_fingerprint_opens_no_other_file(operator, tmp_path, monkeypatch):
    configure(operator, tmp_path)
    (operator / ".claude" / ".credentials.json").write_text("never-read")
    (operator / ".claude.json").write_text("never-read")
    opened = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(self.name) or real(self))
    evaluation.config_fingerprint(os.environ, "1")
    evaluation.not_covered(os.environ)
    assert sorted(set(opened)) == ["CLAUDE.md", "deeper.md", "installed_plugins.json", "managed-settings.json",
                                   "relative.md", "settings.json", "settings.local.json", "shared.md"]


# --- Billing ----------------------------------------------------------------------------------------------------


def billing_file(operator, tmp_path, which):
    path = {"settings.json": operator / ".claude" / "settings.json",
            "settings.local.json": operator / ".claude" / "settings.local.json",
            "managed-settings.json": tmp_path / "managed" / "managed-settings.json"}[which]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


FILES = ("settings.json", "settings.local.json", "managed-settings.json")


@pytest.mark.parametrize("which", FILES)
@pytest.mark.parametrize("content, key", [
    ({"apiKey": SECRET}, "apiKey"),
    ({"apiKeyHelper": SECRET}, "apiKeyHelper"),
    ({"env": {"ANTHROPIC_BASE_URL": SECRET}}, "env.ANTHROPIC_BASE_URL"),
    ({"env": {"ANTHROPIC_API_KEY": ""}}, "env.ANTHROPIC_API_KEY"),
    ({"env": {"ANTHROPIC_AUTH_TOKEN": SECRET}}, "env.ANTHROPIC_AUTH_TOKEN"),
    ({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}, "env.CLAUDE_CODE_USE_BEDROCK"),
])
def test_a_billing_setting_stops_the_run_before_any_cli_start(operator, monkeypatch, tmp_path, which, content, key):
    billing_file(operator, tmp_path, which).write_text(json.dumps(content))
    monkeypatch.setattr(evaluation, "check_real_claude", lambda path: pytest.fail("the CLI was started"))
    monkeypatch.setattr(evaluation, "probe_deny_read", lambda path: pytest.fail("the CLI was started"))
    monkeypatch.setattr(evaluation, "run_case", lambda *a, **k: pytest.fail("a case started"))
    with pytest.raises(evaluation.BillingRefusal) as refused:
        real_suite(tmp_path)
    assert f"{which}: {key}" in str(refused.value) and SECRET not in str(refused.value)


@pytest.mark.parametrize("which", FILES)
def test_a_settings_file_that_cannot_be_checked_stops_the_run(operator, tmp_path, which):
    billing_file(operator, tmp_path, which).write_text("not json")
    [problem] = evaluation.billing_problems(os.environ)
    assert problem.endswith(f"{which}: cannot be checked")


def test_a_billing_setting_added_during_the_run_aborts_it(operator, monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "fresh_database", lambda url: contextlib.nullcontext(("postgresql://x", None)))
    monkeypatch.setattr(evaluation.Run, "start_server", lambda self: pytest.fail("the server started"))
    monkeypatch.setattr(evaluation.Run, "case_cost", lambda self: 0.0)
    monkeypatch.setattr(evaluation, "read_records", lambda *a: [])
    calls = []
    real = evaluation.check_billing

    def check(environ):
        calls.append(1)
        if len(calls) > 1:  # after the check before the suite
            billing_file(operator, tmp_path, "settings.local.json").write_text(json.dumps({"apiKeyHelper": "x"}))
        real(environ)

    monkeypatch.setattr(evaluation, "check_billing", check)
    result, _ = suite(tmp_path)
    assert result["aborted"] == f"launch_refused in {next(r['case'] for r in result['rows'] if 'abort' in r)}"
    assert len([r for r in result["rows"] if r["rep"]]) == 1


def report(tmp_path, name, **operator_login):
    body = {"suite": "task-manager", "rows": [{"case": "direct_edit_brief", "rep": 1, "outcome": "pass"}]}
    if operator_login:
        body["operator_login"] = operator_login
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body))
    return path


def test_runs_with_different_hashes_are_not_directly_comparable(tmp_path):
    config = {key: "a" for key in evaluation.HASHED}
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
    assert set(ids[first:]) == HOSTILE and not set(ids[:first]) & HOSTILE and first == 22
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


def new_case(name):
    """A case someone adds without saying anything about hostility or HOME."""
    base = cases.BY_ID["direct_edit_brief"]
    return cases.Case(name, base.rfc, base.title, base.model, base.fixture, base.turns, base.judge, base.script)


def test_a_new_case_is_hostile_and_needs_a_synthetic_home_until_it_says_otherwise(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    new = new_case("someone_elses_new_case")
    assert new.hostile and new.needs_synthetic_home
    result, _ = suite(tmp_path, selected=[new, cases.BY_ID["direct_edit_brief"]])
    assert [case for case, _ in started] == ["direct_edit_brief"]
    row = next(row for row in result["rows"] if row["case"] == new.id)
    assert row["outcome"] == "not_runnable" and row["blocker"] == "needs_synthetic_home"
    started.clear()
    declared = dataclasses.replace(new, needs_synthetic_home=False)
    suite(tmp_path, selected=[declared, cases.BY_ID["direct_edit_brief"]], out_dir=tmp_path / "second")
    assert [case for case, _ in started] == ["direct_edit_brief", declared.id]


def test_the_cases_on_this_checkout_say_what_they_are():
    safe = {c.id for c in cases.CASES if not c.hostile}
    assert safe == {"direct_edit_brief", "stop_continue", "fresh_session_excerpt", "readonly_resume",
                    "opus_switch"} | EDIT_CASES
    assert {c.id for c in cases.CASES if not c.needs_synthetic_home} == safe | HOSTILE
    canaries = cases.BY_ID["read_deny_live"]
    assert canaries.hostile and canaries.needs_synthetic_home


def test_the_case_that_plants_files_under_home_never_starts(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    result, _ = suite(tmp_path, selected=[cases.BY_ID["direct_edit_brief"], cases.BY_ID["read_deny_live"]])
    assert [case for case, _ in started] == ["direct_edit_brief"]
    row = next(row for row in result["rows"] if row["case"] == "read_deny_live")
    assert row["outcome"] == "not_runnable" and row["blocker"] == "needs_synthetic_home"
    assert not (operator / ".git-credentials").exists() and not (operator / ".zsh_history").exists()


def test_the_edit_command_directory_of_a_direct_session_stays_under_the_run_root(operator, tmp_path):
    root = tmp_path / "case"
    with evaluation.isolated_environment(root, None, operator_login=True):
        environment = child_env.inherited()  # what the CLI's environment starts from
        directory = task_manager_edit.edit_bin(environment, forbidden=(
            str(root / "work"), environment["TMPDIR"],
            *task_manager_sandbox.read_deny_paths(environment, root / "runtime")))
    assert directory == root / "cache" / "looper" / "task-manager-bin" and (directory / "looper-edit").is_symlink()
    assert not (operator / ".cache").exists()


def test_refusals_before_any_case(operator, monkeypatch, tmp_path):
    started = stub(monkeypatch)
    for override, message in (({"behavior": "bad"}, "scripted"),
                              ({"out_dir": evaluation.REPO_ROOT / "evals" / "x"}, "ignored"),
                              ({"dump_dir": evaluation.REPO_ROOT / "evals" / "x"}, "ignored")):
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
    # (this synthetic home sits in the temporary directory, so that root is what gets replaced)
    assert saved["rows"][0]["reason"] == "RuntimeError" and saved["rows"][0]["evidence"].startswith("<eval-root>/")
    section = saved["operator_login"]
    assert section["config"]["user_claude_md_sha256"] == sha(MEMORY.encode())
    assert section["config"]["cli_version"].startswith("fake ") and section["changed_during_run"] is True
    assert section["note"] == "results depend on this machine's configuration"
    assert "session transcripts" in section["transcripts"] and "configuration directory" in section["transcripts"]
    assert "synthetic hostile content, not user data" in section["transcripts"]
    assert section["not_covered"] and section["deny_read_probe"]["status"] == "not_run"
    local = (path.parent / "task-manager-eval.local.json").read_text()
    assert "detail-marker" in local and "secret-marker" not in local


def stand_in_claude(tmp_path):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho '2.1.332 (Claude Code)'\n")
    claude.chmod(0o755)
    return str(claude)


def real_suite(tmp_path, **override):
    arguments = {"mode": "real", "claude_bin": stand_in_claude(tmp_path), "total_cap_usd": 18, "case_budget_usd": 1.25,
                 "opus_case_budget_usd": 3, **override}
    return suite(tmp_path, **arguments)


def probe_stub(monkeypatch, status="PASS"):
    calls = []
    monkeypatch.setattr(evaluation, "probe_deny_read", lambda binary: calls.append(binary) or {"status": status})
    return calls


def test_real_mode_hands_no_credential_to_a_case_and_records_the_verified_version(operator, monkeypatch, tmp_path):
    probe_stub(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    started = stub(monkeypatch)  # asserts operator_login and credential None; nothing real runs
    result, path = real_suite(tmp_path, total_cap_usd=2, selected=[cases.BY_ID["direct_edit_brief"]])
    assert started == [("direct_edit_brief", 1)] and result["operator_login"]["config"]["cli_version"] == "2.1.332"
    assert SECRET not in path.read_text()


# --- Stage 0 and the reservation --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["FAIL", "INVALID", "ALARM", "ABORTED"])
def test_no_case_starts_unless_the_read_deny_probe_passes(operator, monkeypatch, tmp_path, status):
    calls = probe_stub(monkeypatch, status)
    started = stub(monkeypatch)
    result, path = real_suite(tmp_path, total_cap_usd=20.5)  # every runnable case once
    assert len(calls) == 1 and started == []
    assert result["aborted"] == f"deny_read_probe {status}" and result["outcome"] == "incomplete"
    assert result["operator_login"]["deny_read_probe"]["status"] == status
    held = [row for row in result["rows"] if row.get("blocker") == "deny_read_probe"]
    assert {row["case"] for row in held} >= HOSTILE | {"direct_edit_brief"}
    assert result["ledger_spent_usd"] == 0


def test_the_probe_runs_once_before_the_first_case(operator, monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(evaluation, "probe_deny_read", lambda binary: order.append("probe") or {"status": "PASS"})
    started = stub(monkeypatch)
    real = evaluation.run_case
    monkeypatch.setattr(evaluation, "run_case", lambda case, rep, **k: order.append(case.id) or real(case, rep, **k))
    real_suite(tmp_path, selected=[cases.BY_ID["direct_edit_brief"]], reps=2)
    assert order == ["probe", "direct_edit_brief", "direct_edit_brief"] and len(started) == 2


def test_the_reservation_is_computed_from_the_cases_and_printed_before_the_run(operator, monkeypatch, tmp_path, capsys):
    probe_stub(monkeypatch)
    printed = []

    def run_case(case, rep, **kwargs):
        printed.append(capsys.readouterr().err)
        return {"case": case.id, "rep": rep, "model": case.model, "outcome": "pass", "cost_usd": 0.0,
                "verified_direct_launches": 1}

    monkeypatch.setattr(evaluation, "run_case", run_case)
    selected = [cases.BY_ID[name] for name in ("direct_edit_brief", "foreign_task_refused", "opus_switch")]
    result, _ = real_suite(tmp_path, selected=selected, reps=2)
    rows = result["operator_login"]["reservation"]
    assert [(r["case"], r["model"], r["reps"], r["unit_usd"], r["subtotal_usd"]) for r in rows] == [
        ("direct_edit_brief", "sonnet", 2, 1.25, 2.5), ("opus_switch", "opus", 2, 3, 6),
        ("foreign_task_refused", "sonnet", 2, 1.25, 2.5)]
    assert evaluation.reservation_table(rows) in printed[0] and "| total | | | | 11.00 |" in printed[0]


CEILING_USD = 23.5  # the approved ceiling of an operator-login run
# Runnable with operator-login but in neither proposed command: all of them together would exceed the ceiling.
NOT_PROPOSED = EDIT_CASES | {"project_instructions_hostile", "branch_kept_against_convention"}


def documented_reservation():
    """The README's table: every case an operator-login run can start, Opus once and the others twice."""
    runnable = [c for c in cases.CASES if c.judge is not None and c.requires is None and not c.needs_synthetic_home]
    budgets = {cases.SONNET: 1.25, cases.OPUS: 3.0}
    return (evaluation.reservation([c for c in runnable if c.model == cases.OPUS], 1, budgets)
            + evaluation.reservation([c for c in runnable if c.model == cases.SONNET], 2, budgets))


def test_the_readme_table_is_the_documented_commands_reservation():
    rows = documented_reservation()
    assert sum(row["subtotal_usd"] for row in rows) == 38.0 > CEILING_USD
    proposed = [row for row in rows if row["case"] not in NOT_PROPOSED]
    assert proposed == rows[:7] and sum(row["subtotal_usd"] for row in proposed) == 18.0 <= CEILING_USD
    readme = (evaluation.REPO_ROOT / "evals" / "README.md").read_text()
    assert evaluation.reservation_table(rows) in readme


def test_the_documented_commands_run_every_non_hostile_case_before_a_hostile_one():
    readme = (evaluation.REPO_ROOT / "evals" / "README.md").read_text()
    section = readme.split("### `--credential operator-login`")[1]
    block = section.split("# proposed, needs approval")[1].split("```")[0].replace("\\\n", " ")
    commands = [line for line in block.splitlines() if "--mode real" in line]
    selected = [re.search(r"--cases (\S+)", line)[1].split(",") for line in commands]
    assert selected[0] == ["opus_switch"] and not cases.BY_ID["opus_switch"].hostile
    assert commands[0].rstrip().endswith("&&")  # the Sonnet command runs only if the first passed
    assert "--total-cap-usd 3 " in commands[0] and "--total-cap-usd 18 " in commands[1]
    runnable = {c.id for c in cases.CASES if c.judge and not c.requires and not c.needs_synthetic_home}
    assert set(selected[0] + selected[1]) == runnable - NOT_PROPOSED


# --- The launch check -------------------------------------------------------------------------------------------


def test_a_refused_turn_is_a_launch_refusal(operator, tmp_path):
    run = bare_run(tmp_path)
    run.turns.append({"final": {"outcome": "refused", "text": "mismatch"}})
    with pytest.raises(evaluation.LaunchRefused, match="refused"):
        run.verify_launches()
    run.turns[0]["final"]["outcome"] = "success"
    assert run.verify_launches() == 0


def bare_run(tmp_path):
    return evaluation.Run(cases.BY_ID["direct_edit_brief"], 1, mode="fake", behavior="ok", url="", root=tmp_path / "run",
                          out=tmp_path, claude_bin=None, budget=1)


def good_launch(tmp_path):
    deny = list(task_manager_sandbox.read_deny_paths(os.environ, tmp_path / "runtime"))
    return {"args": ["--dangerously-skip-permissions"], "settings": {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": deny}}}}


def test_a_failed_turn_does_not_count_as_a_read_back_launch(operator, tmp_path):
    run = bare_run(tmp_path)
    run.capture.mkdir(parents=True)
    launch = json.dumps(good_launch(tmp_path))
    for name, outcome, count in (("1", "failed", 0), ("2", "failed", 0), ("3", "stopped", 1), ("4", "success", 2)):
        (run.capture / f"launch-{name}.json").write_text(launch)
        run.turns.append({"final": {"outcome": outcome}})
        assert run.verify_launches() == count, name


@pytest.mark.parametrize("broken", ["sandbox_off", "unreadable"])
@pytest.mark.parametrize("failure", [EvalError("a turn did not end in time"), RuntimeError("stream broke")])
def test_a_bad_launch_aborts_the_run_even_when_its_turn_raises(operator, monkeypatch, tmp_path, broken, failure):
    monkeypatch.setattr(evaluation, "fresh_database", lambda url: contextlib.nullcontext(("postgresql://x", None)))
    for name in ("build_files", "install", "start_server", "enable", "seed_tasks", "write_cli_config"):
        monkeypatch.setattr(evaluation.Run, name, lambda self: None)
    monkeypatch.setattr(evaluation.Run, "sentinel", lambda self: {})
    monkeypatch.setattr(evaluation.Run, "listing", lambda self: [])
    monkeypatch.setattr(evaluation.Run, "case_cost", lambda self: 0.0)
    monkeypatch.setattr(evaluation, "read_records", lambda *a: [])
    started = []

    def send(self, turn, index):
        started.append(self.case.id)
        self.capture.mkdir(parents=True)
        launch = good_launch(tmp_path)
        launch["settings"]["sandbox"]["enabled"] = False
        (self.capture / "launch-1.json").write_text(json.dumps(launch) if broken == "sandbox_off" else "{")
        raise failure

    monkeypatch.setattr(evaluation.Run, "send", send)
    result, _ = suite(tmp_path)
    assert len(started) == 1, started
    assert result["aborted"] == f"launch_refused in {started[0]}"
    assert result["rows"][-1]["abort"] == "launch_refused" and result["rows"][-1]["outcome"] == "error"


def test_a_turn_that_raises_after_a_good_launch_does_not_abort(operator, monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "fresh_database", lambda url: contextlib.nullcontext(("postgresql://x", None)))
    for name in ("build_files", "install", "start_server", "enable", "seed_tasks", "write_cli_config"):
        monkeypatch.setattr(evaluation.Run, name, lambda self: None)
    monkeypatch.setattr(evaluation.Run, "sentinel", lambda self: {})
    monkeypatch.setattr(evaluation.Run, "listing", lambda self: [])
    monkeypatch.setattr(evaluation.Run, "case_cost", lambda self: 0.0)
    monkeypatch.setattr(evaluation, "read_records", lambda *a: [])

    def send(self, turn, index):
        self.capture.mkdir(parents=True)
        (self.capture / "launch-1.json").write_text(json.dumps(good_launch(tmp_path)))
        raise EvalError("a turn did not end in time")

    monkeypatch.setattr(evaluation.Run, "send", send)
    result, _ = suite(tmp_path, selected=[cases.BY_ID["direct_edit_brief"]])
    assert result["aborted"] is None and result["rows"][0]["outcome"] == "error"


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
@pytest.mark.skipif(sys.platform != "darwin", reason="the isolated service opens a direct session on macOS only; "
                                                     "a skip is not a pass")
def test_fake_run_passes_both_stages_and_a_readback_mismatch_aborts_it(operator, monkeypatch, tmp_path):
    selected = [cases.BY_ID[name] for name in ("foreign_task_refused", "direct_edit_brief", "readonly_resume")]
    result, path = suite(tmp_path, admin_url=DSN, selected=selected)
    assert not (operator / ".cache").exists()  # every cache of the run went under its own root, looper-edit's too
    assert [(row["case"], row["outcome"]) for row in result["rows"]] == [
        ("direct_edit_brief", "pass"), ("readonly_resume", "pass"), ("foreign_task_refused", "pass")], result["rows"]
    assert result["rows"][0]["verified_direct_launches"] >= 1
    assert result["operator_login"]["changed_during_run"] is False
    evidence = json.loads((path.parent / "direct_edit_brief-1" / "evidence.json").read_text())
    for launch in evidence["launches"]:
        assert "CLAUDE_CONFIG_DIR" not in launch["env_names"]
    assert any(launch["looper_edit_on_path"] for launch in evidence["launches"])  # so that directory was made
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
