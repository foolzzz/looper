"""The task Manager eval runner: judges, credentials, budget, and the free fake pass. No model is called."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from evals.prompt_regression import task_manager_cases as cases
from evals.prompt_regression import task_manager_eval as evaluation
from evals.prompt_regression.runner import EvalError
from tests.db_support import DSN

SECRET = "sk-test-not-a-real-secret-value"


@pytest.fixture
def clean_environment(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --- Credentials ----------------------------------------------------------------------------------------------


def test_real_mode_refuses_without_an_explicit_credential_choice(clean_environment):
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    clean_environment.setenv("ANTHROPIC_API_KEY", SECRET)
    for choice in (None, "", "token"):
        with pytest.raises(EvalError, match="--credential"):
            evaluation.resolve_credential(choice)


def test_the_chosen_variable_is_the_only_one_read_and_there_is_no_fallback(clean_environment):
    clean_environment.setenv("ANTHROPIC_API_KEY", SECRET)
    with pytest.raises(EvalError, match="CLAUDE_CODE_OAUTH_TOKEN") as refused:
        evaluation.resolve_credential("oauth")
    assert SECRET not in str(refused.value)
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-value")
    assert evaluation.resolve_credential("oauth") == ("CLAUDE_CODE_OAUTH_TOKEN", "oauth-value")
    assert evaluation.resolve_credential("api-key") == ("ANTHROPIC_API_KEY", SECRET)


def test_isolated_environment_holds_only_synthetic_settings_and_the_chosen_credential(clean_environment, tmp_path):
    clean_environment.setenv("ANTHROPIC_API_KEY", SECRET)
    clean_environment.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "operator"))
    clean_environment.setenv("LOOPER_TEST_VALUE", "x")
    clean_environment.setenv("HOME", str(tmp_path / "operator-home"))
    before = dict(os.environ)
    with evaluation.isolated_environment(tmp_path / "case", ("CLAUDE_CODE_OAUTH_TOKEN", "oauth-value")) as home:
        assert home == tmp_path / "case" / "home" and os.environ["HOME"] == str(home)
        assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "case" / "claude-config")
        assert "ANTHROPIC_API_KEY" not in os.environ and "LOOPER_TEST_VALUE" not in os.environ
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-value"
        assert os.environ["PATH"] == evaluation.MINIMAL_PATH
        assert json.loads((tmp_path / "case" / "claude-config" / "settings.json").read_text()) == evaluation.SYNTHETIC_SETTINGS
        assert list(home.iterdir()) == []  # nothing is copied from the operator
    assert dict(os.environ) == before


PLANTED = {"SSH_AUTH_SOCK": "/tmp/agent.sock", "GH_TOKEN": "gh-secret", "AWS_ACCESS_KEY_ID": "aws-secret",
           "AWS_SECRET_ACCESS_KEY": "aws-secret", "NPM_TOKEN": "npm-secret", "ANTHROPIC_AUTH_TOKEN": "auth-secret",
           "MADE_UP_SERVICE_TOKEN": "made-up-secret", "GIT_SSH_COMMAND": "ssh -i key", "XDG_CONFIG_HOME": "/operator/xdg",
           "LOOPER_ANYTHING": "x"}


def test_environment_is_an_allowlist(clean_environment, tmp_path):
    for name, value in PLANTED.items():
        clean_environment.setenv(name, value)
    clean_environment.setenv("LC_ALL", "C")
    clean_environment.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    with evaluation.isolated_environment(tmp_path / "case", None):
        assert not set(PLANTED) & set(os.environ)
        assert os.environ["LC_ALL"] == "C" and os.environ["HTTPS_PROXY"] == "http://proxy.invalid:3128"
        extra = set(os.environ) - evaluation.KEPT_VARIABLES - {"HOME", "PATH", "CLAUDE_CONFIG_DIR", "GIT_TERMINAL_PROMPT",
                                                               "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "LC_ALL"}
        assert not extra, extra


# --- Budget -----------------------------------------------------------------------------------------------------


def test_ledger_persists_and_refuses_what_could_exceed_the_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = evaluation.Ledger(path, total_cap=5, opus_cap=3)
    first = ledger.reserve("direct_edit_brief", 1, "sonnet", 1.25)
    assert ledger.spent() == 1.25  # the reservation counts at once
    ledger.settle(first, 1.0, "pass")
    ledger.settle(ledger.reserve("opus_switch", 1, "opus", 3.0), 2.0, "pass")
    again = evaluation.Ledger(path, total_cap=5, opus_cap=3)  # a new process does not reset the cap
    assert again.spent() == 3.0 and again.spent("opus") == 2.0
    with pytest.raises(evaluation.BudgetRefusal, match="total cap"):
        again.reserve("x", 1, "sonnet", 2.5)
    with pytest.raises(evaluation.BudgetRefusal, match="Opus cap"):
        again.reserve("x", 1, "opus", 1.5)
    assert evaluation.Ledger(path, total_cap=5, opus_cap=3).spent() == 3.0  # a refusal writes nothing


def test_an_interrupted_case_keeps_its_reservation(clean_environment, tmp_path, monkeypatch):
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    arguments = real_arguments(tmp_path, selected=[cases.BY_ID["readonly_progress"]])

    def killed(*args, **kwargs):
        raise KeyboardInterrupt  # what Ctrl-C or a handled signal does; a SIGKILL leaves the reservation row too

    monkeypatch.setattr(evaluation, "run_case", killed)
    with pytest.raises(KeyboardInterrupt):
        evaluation.run_suite(**arguments)
    after = evaluation.Ledger(tmp_path / "ledger.jsonl", total_cap=20, opus_cap=6)
    assert after.spent() == 1.25  # the next command sees the spend
    # A case that raised an ordinary error is settled at no less than its reservation, too.
    monkeypatch.setattr(evaluation, "run_case", lambda *a, **k: {"outcome": "error", "cost_usd": 0.1})
    evaluation.run_suite(**arguments)
    assert evaluation.Ledger(tmp_path / "ledger.jsonl", total_cap=20, opus_cap=6).spent() == 2.5
    # The reservation is on disk before the case starts.
    seen = []
    monkeypatch.setattr(evaluation, "run_case", lambda *a, **k: seen.append(
        evaluation.Ledger(tmp_path / "ledger.jsonl", total_cap=20, opus_cap=6).spent()) or {"outcome": "pass", "cost_usd": 0.2})
    evaluation.run_suite(**arguments)
    assert seen == [3.75]
    assert evaluation.Ledger(tmp_path / "ledger.jsonl", total_cap=20, opus_cap=6).spent() == 2.7


def real_arguments(tmp_path, **override):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho '2.1.332 (Claude Code)'\n")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    return {"mode": "real", "admin_url": "postgresql://nobody@127.0.0.1:1/never", "out_dir": tmp_path / "out",
            "claude_bin": str(claude), "credential_choice": "oauth", "total_cap_usd": 20, "case_budget_usd": 1.25,
            "opus_case_budget_usd": 3, "ledger_path": tmp_path / "ledger.jsonl", **override}


def test_real_mode_refusals_happen_before_any_case_starts(clean_environment, tmp_path):
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    ok = real_arguments(tmp_path)
    for override, message in (
        ({"credential_choice": None}, "--credential"),
        ({"total_cap_usd": None}, "positive --total-cap-usd"),
        ({"case_budget_usd": float("nan")}, "positive --case-budget-usd"),
        ({"opus_case_budget_usd": None}, "positive --opus-case-budget-usd"),
        ({"claude_bin": None}, "--claude-bin"),
        ({"total_cap_usd": 10, "reps": 2}, "reserve"),  # 2 x (6 x 1.25 + 3) is more than 10
        ({"opus_cap_usd": 2}, "Opus cap"),
        ({"out_dir": evaluation.REPO_ROOT / "evals" / "runs" / "x"}, "outside the repository"),
        ({"behavior": "bad"}, "fake-mode control"),
        ({"dump_dir": tmp_path / "dump"}, "fake mode only"),
    ):
        with pytest.raises(EvalError, match=message) as refused:
            evaluation.run_suite(**{**ok, **override})
        assert SECRET not in str(refused.value)
    assert not (tmp_path / "ledger.jsonl").exists()  # nothing was spent or reserved


def test_real_mode_refuses_a_cli_the_task_Manager_would_refuse(clean_environment, tmp_path):
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    arguments = real_arguments(tmp_path)
    Path(arguments["claude_bin"]).write_text("#!/bin/sh\necho '2.1.333 (Claude Code)'\n")
    with pytest.raises(EvalError, match="2.1.333"):
        evaluation.run_suite(**arguments)


def test_a_case_that_would_exceed_the_remaining_cap_is_not_started(clean_environment, tmp_path, monkeypatch):
    clean_environment.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    arguments = real_arguments(tmp_path, selected=[cases.BY_ID["readonly_progress"]], total_cap_usd=1.5)
    (tmp_path / "ledger.jsonl").write_text(json.dumps({"run": "r1", "case": "x", "rep": 1, "model": "sonnet", "spent_usd": 0.5,
                                                         "outcome": "pass"}) + "\n")
    started = []
    monkeypatch.setattr(evaluation, "run_case", lambda *a, **k: started.append(a) or {})
    with pytest.raises(evaluation.BudgetRefusal):
        evaluation.run_suite(**arguments)
    assert started == []


# --- Cases that cannot run ------------------------------------------------------------------------------------


def test_cases_that_need_step_3_report_a_typed_reason_and_never_pass():
    tools = evaluation.tool_names()
    handoff = cases.blocker(cases.BY_ID["verbatim_handoff"], tools)
    gate = cases.blocker(cases.BY_ID["answer_gate"], tools)
    assert handoff and handoff[0] is cases.Blocker.STEP_3_NOT_MERGED and "step 3 not merged" in handoff[1]
    assert gate and gate[0] is cases.Blocker.TOOL_NOT_ON_MAIN
    # Once the tool exists, a case without a judge is still not runnable: it never turns into a silent pass.
    later = cases.blocker(cases.BY_ID["verbatim_handoff"], tools | {"task_supplement"})
    assert later and later[0] is cases.Blocker.JUDGE_MISSING
    assert all(cases.blocker(case, tools) is None for case in cases.CASES if case.requires is None)
    summary = evaluation.summarize([{"case": "a", "outcome": "pass", "cost_usd": 0},
                                    {"case": "b", "outcome": "not_runnable", "cost_usd": 0}],
                                   mode="fake", behavior="ok", reps=1, aborted=None, done=True, ledger=None, total_cap=None)
    assert summary["outcome"] == "incomplete"
    for outcome in ("unparsed", "error"):
        assert evaluation.summarize([{"case": "a", "outcome": outcome, "cost_usd": 0}], mode="fake", behavior="ok",
                                    reps=1, aborted=None, done=True, ledger=None, total_cap=None)["outcome"] == "incomplete"


def test_compare_inputs_reports_any_difference(tmp_path):
    left, right = tmp_path / "a", tmp_path / "b"
    for side in (left, right):
        side.mkdir()
        (side / "case.json").write_text('{"system_prompt": "one"}\n')
    assert evaluation.compare_inputs(left, right) == (True, "")
    (right / "case.json").write_text('{"system_prompt": "two"}\n')
    same, text = evaluation.compare_inputs(left, right)
    assert not same and '-{"system_prompt": "one"}' in text and '+{"system_prompt": "two"}' in text
    (right / "case.json").unlink()
    assert not evaluation.compare_inputs(left, right)[0]


def test_claim_readers():
    assert cases.reports_failure("It FAILED with an AssertionError.") and not cases.reports_failure("The test passed.")
    assert not cases.reports_failure("Ran 2 tests, no failures.") and cases.reports_success("Ran 2 tests: OK")
    assert not cases.reports_success("The tests still fail.")
    assert cases.says_cannot("I can't write here; this is read-only.") and not cases.says_cannot("Done.")
    assert cases.claims_done("I created notes.md") and not cases.claims_done("I did not create notes.md")


# --- The fake pass: record shape, judges, input dump ----------------------------------------------------------------

# Checks that only guard that the scenario happened or that code-owned behavior held; the scripted bad model cannot
# break them without Looper's own code misbehaving, so the bad arm leaves them passing.
GUARDS = {
    "direct_edit_brief": {"turn1_answered", "turn2_answered", "direct_tools_used", "calc_changed",
                          "first_test_run_recorded", "failing_command_ran"},
    "stop_continue": {"turn2_answered", "same_session_after_stop"},
    "readonly_progress": {"turn1_answered", "requirement_not_written"},
    "foreign_task_refused": {"turn1_answered", "events_parsed", "own_task_requirement_unchanged",
                             "no_tool_call_names_the_other_task"},
    "fresh_session_excerpt": {"turn1_answered", "turn2_answered", "excerpt_leaves_out_commands", "last_attempt_succeeded"},
    "readonly_resume": {"turn1_answered"},
    "opus_switch": {"turn1_answered", "turn2_answered", "turn3_answered", "turn4_answered", "cost_within_case_budget",
                    "task_binding_unchanged", "file_holds_only_the_other_value", "file_value_read_in_turn_2"},
}


def checks(row):
    return {check["name"]: check["ok"] for check in row["checks"]}


@pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
def test_fake_pass_judges_each_criterion_and_dumps_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)  # an operator's own variable never reaches a child
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    good, good_path = evaluation.run_suite(mode="fake", admin_url=DSN, out_dir=tmp_path / "ok",
                                           dump_dir=tmp_path / "dump-a")
    assert os.environ["ANTHROPIC_API_KEY"] == SECRET  # restored
    assert good["outcome"] == "incomplete"  # two cases cannot run on this checkout: never a pass
    rows = {row["case"]: row for row in good["rows"]}
    assert {c for c, r in rows.items() if r["outcome"] == "not_runnable"} == {"verbatim_handoff", "answer_gate"}
    assert rows["verbatim_handoff"]["blocker"] == "step_3_not_merged"
    runnable = [case for case in cases.CASES if case.requires is None]
    for case in runnable:
        row = rows[case.id]
        assert row["outcome"] == "pass", (case.id, [c for c in row["checks"] if not c["ok"]])
        assert all(row["checks"]), case.id

    # The recorded shape the judges read: what the stand-in wrote is what a judge parsed, call by call.
    for case in runnable:
        data = json.loads((tmp_path / "ok" / f"{case.id}-1" / "evidence.json").read_text())
        evidence = cases.Evidence(data)
        assert len(evidence.turns) == len(case.turns)
        assert all(t["final"] and t["sse"] for t in evidence.turns)
        for record in evidence.records:
            run = evidence.runs[record["id"]]
            if record["status"] == "completed":
                assert run.tools and any(name.startswith("mcp__looper__") for name in run.tools), case.id
                assert all(call.result is not None for call in run.calls), (case.id, run.calls)  # a stopped run keeps its open call
        assert evidence.launches and all("LOOPER_TASK_MANAGER" in launch["env_names"] for launch in evidence.launches)
        assert not any((set(PLANTED) | {"ANTHROPIC_API_KEY"}) & set(launch["env_names"]) for launch in evidence.launches)
        assert SECRET not in json.dumps(data) and SECRET not in (tmp_path / "ok" / f"{case.id}-1" / "server-0.log").read_text()
    for path in (tmp_path / "ok").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="replace"), path

    # Every criterion that a scripted bad model can break, breaks; the passing run passes all of them.
    bad, _ = evaluation.run_suite(mode="fake", admin_url=DSN, out_dir=tmp_path / "bad", behavior="bad",
                                  selected=runnable)
    bad_rows = {row["case"]: row for row in bad["rows"]}
    for case in runnable:
        row = bad_rows[case.id]
        assert row["outcome"] == "fail", case.id
        passing = {name for name, ok in checks(row).items() if ok}
        assert passing <= GUARDS[case.id], (case.id, sorted(passing - GUARDS[case.id]))
        assert set(checks(rows[case.id])) == set(checks(row))

    # A model that says the right thing and does the wrong one fails on the records, whatever its reply says.
    lie, _ = evaluation.run_suite(mode="fake", admin_url=DSN, out_dir=tmp_path / "lie", behavior="lie", selected=runnable)
    lied = {row["case"]: row for row in lie["rows"]}
    caught = {"direct_edit_brief": "tool_targets_inside_task_dir", "stop_continue": "sleep_started",
              "readonly_progress": "no_file_tools_that_write", "foreign_task_refused": "sentinel_file_unchanged",
              "fresh_session_excerpt": "session_fresh_resume_failed", "readonly_resume": "no_write_tool_called",
              "opus_switch": "models_follow_the_switch"}
    for case in runnable:
        assert lied[case.id]["outcome"] == "fail" and not checks(lied[case.id])[caught[case.id]], case.id
    # Mutation: a judge that read the reply instead of the records would pass the liar; the real one does not.
    liar = json.loads((tmp_path / "lie" / "readonly_progress-1" / "evidence.json").read_text())
    assert not cases.claims_done(liar["turns"][0]["final"]["text"])
    assert not checks(lied["readonly_progress"])["no_file_tools_that_write"]

    # The report names the build it evaluated.
    assert len(good["build"]["git_head"]) == 40 and isinstance(good["build"]["dirty"], bool)
    assert Path(good["build"]["looper_file"]).is_relative_to(evaluation.REPO_ROOT)

    # The unparsed path: a record the judge cannot read is neither a pass nor a fail, and the raw file stays.
    data = json.loads((tmp_path / "ok" / "readonly_progress-1" / "evidence.json").read_text())
    broken = json.loads(json.dumps(data))
    broken["records"][0]["events"].append({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_x", "name": "Bash"}]}})  # no input: not a call a model event can carry
    verdict = evaluation.judge(broken, cases.BY_ID["readonly_progress"])
    assert verdict["outcome"] == "unparsed" and "tool_use" in verdict["reason"] and not verdict["checks"]
    assert evaluation.judge({"turns": []}, cases.BY_ID["readonly_progress"])["outcome"] == "unparsed"

    # A judge that crashes after a paid run still leaves the raw records and the evidence on disk.
    def crash(self, data):
        raise RuntimeError("parser crashed")

    with monkeypatch.context() as patch:
        patch.setattr(cases.Evidence, "__init__", crash)
        row = evaluation.run_case(cases.BY_ID["readonly_progress"], 1, mode="fake", behavior="ok", admin_url=DSN,
                                  out_dir=tmp_path / "crash", claude_bin=None, credential=None, budget=1.25)
    folder = tmp_path / "crash" / "readonly_progress-1"
    assert row["outcome"] == "unparsed" and "parser crashed" in row["reason"]
    assert (folder / "raw-records.jsonl").read_text().strip() and (folder / "turn1.sse").read_text().strip()
    assert (folder / "evidence.json").exists()

    # The free input dump: same task, same inputs, byte for byte; a changed input shows as a diff.
    again, _ = evaluation.run_suite(mode="fake", admin_url=DSN, out_dir=tmp_path / "again", dump_dir=tmp_path / "dump-b",
                                    selected=[cases.BY_ID["readonly_progress"], cases.BY_ID["readonly_resume"]])
    for name in ("readonly_progress.json", "readonly_resume.json"):
        assert (tmp_path / "dump-a" / name).read_text() == (tmp_path / "dump-b" / name).read_text()
    dumped = json.loads((tmp_path / "dump-a" / "readonly_progress.json").read_text())
    launch = dumped["launches"][0]
    assert "You are this task's own Claude Code session" in launch["system_prompt"]
    assert dumped["turn_inputs"][0]["runs"] == [cases.BY_ID["readonly_progress"].turns[0].text]
    assert {tool["name"] for tool in dumped["tool_definitions"]} == {"task_status", "task_requirement_set"}
    assert dumped["init_tools"][0][0][-2:] == ["mcp__looper__task_status", "mcp__looper__task_requirement_set"]
    assert "<TASK>" in launch["system_prompt"] and "--dangerously-skip-permissions" in launch["args"]
    read_only = json.loads((tmp_path / "dump-a" / "readonly_resume.json").read_text())["launches"][0]["args"]
    assert "--strict-mcp-config" in read_only and "--dangerously-skip-permissions" not in read_only
    (tmp_path / "dump-b" / "readonly_progress.json").write_text(
        (tmp_path / "dump-b" / "readonly_progress.json").read_text().replace("Looper's task brief", "A changed brief"))
    same, text = evaluation.compare_inputs(tmp_path / "dump-a", tmp_path / "dump-b")
    assert not same and "A changed brief" in text
