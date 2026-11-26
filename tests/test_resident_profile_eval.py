"""Synthetic resident-profile dialogs and their budgeted evaluation matrix; no paid calls."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.prompt_regression import resident_profile as profile
from evals.prompt_regression import dialogs
from evals.prompt_regression import runner
from evals.prompt_regression import __main__ as cli
from tests.db_support import DSN


@pytest.mark.parametrize(("question", "answer", "known", "passes"), [
    ("identity", "You are Mira Vale.", True, True),
    ("project", "You are working on Lantern Atlas.", True, True),
    ("restriction", "No proactive messages from 22:00 to 08:00 in Pacific/Honolulu.", True, True),
    ("identity", "你叫 Mira Vale。", True, True),
    ("identity", "You're Mira Vale.", True, True),
    ("restriction", "Proactive messages must not be sent between 22:00 and 08:00 in Pacific/Honolulu.", True, True),
    ("identity", "You are someone else.", True, False),
    ("identity", "You are not Mira Vale.", True, False),
    ("project", "You are working on another project.", True, False),
    ("restriction", "No proactive messages from 08:00 to 22:00 in Pacific/Honolulu.", True, False),
    ("restriction", "No proactive messages from 22:00 to 08:00 in UTC.", True, False),
    ("restriction", "Proactive messages can't be sent between 22:00 and 08:00.", True, True),
    ("restriction", "Not between **22:00 and 08:00**; those are your quiet hours.", True, True),
    ("restriction", "Quiet hours: 22:00–08:00.", True, True),
    ("restriction", "22:00-08:00", True, True),
    ("restriction", "Between 22:00 and 08:00.", True, True),
    ("restriction", "From 22:00 to 08:00 in Pacific/Honolulu time.", True, True),
    ("restriction", "22:00—08:00 Pacific/Honolulu", True, True),
    ("restriction", "Don't send proactive messages from 22:00 to 8:00.", True, True),
    ("restriction", "22:00-08:00 不得主动发送消息。", True, True),
    ("identity", "Name: Mira Vale", True, True),
    ("project", "Current project: Lantern Atlas", True, True),
    ("identity", "Mira Vale", True, True),
    ("project", "Lantern Atlas.", True, True),
    ("restriction", "Proactive messages are allowed between 22:00 and 08:00.", True, False),
    ("restriction", "Your quiet hours are not 22:00-08:00.", True, False),
    ("restriction", "You have no quiet hours; 22:00-08:00 is fine to message.", True, False),
    ("restriction", "你没有免打扰时间，22:00-08:00 可以发消息", True, False),
    ("restriction", "Quiet hours: 22:00-08:00; messages are allowed then.", True, False),
    ("restriction", "22:00–08:00 免打扰，可以发消息。", True, False),
    ("restriction", "Your quiet hours are 22:00-08:00.", True, True),
    ("restriction", "22:00-08:00 are your quiet hours.", True, True),
    ("restriction", "Quiet hours: not 22:00-08:00.", True, False),
    ("restriction", "22:00-08:00 is not your quiet period.", True, False),
    ("restriction", "No proactive messages from 22:00 to 08:00; outside those hours messages are allowed.", True, True),
    ("restriction", "22:00–08:00 免打扰，不发消息。", True, True),
    ("restriction", "22:00–08:00 免打扰，之外可以发消息。", True, True),
    ("restriction", "22:00 到 08:00 不接收消息。", True, True),
    ("identity", "我不知道你是谁", False, True),
    ("identity", "我不知道你是谁。", False, True),
    ("identity", "我不清楚你是谁", False, True),
    ("identity", "我没有你的画像", False, True),
    ("identity", "I don't know who you are", False, True),
    ("identity", "你是陈亮", False, False),
    ("identity", "你是北辰项目的负责人", False, False),
    ("identity", "You're Alex Chen", False, False),
    ("identity", "我不知道你是谁。你是陈亮。", False, False),
    ("identity", "I don't know who you are.", False, True),
    ("identity", "I don’t know who you are.", False, True),
    ("project", "I do not know what you are working on. Please tell me.", False, True),
    ("restriction", "我不知道你的沟通限制。", False, True),
    ("identity", "You are Mira Vale.", False, False),
    ("identity", "I don't know your name. You are Alex.", False, False),
    ("identity", "I don't know your name, but you are Alex.", False, False),
    ("identity", "Your account has an email attached. No personal profile is available to me.", False, True),
    ("identity", "I have neither a name nor personal details for you. You use this dashboard.", False, True),
    ("identity", "I cannot tell who you are personally. You are the user of this dashboard.", False, True),
    ("identity", "我没有你的姓名资料，只知道你在使用控制台。", False, True),
    ("project", "There are zero Looper tasks. The registered project is eval-calc.", False, True),
    ("project", "Your one project is eval-calc; it contains no tasks.", False, True),
    ("project", "eval-calc is registered, but I cannot see work you are doing elsewhere.", False, True),
    ("project", "Looper 项目列表只有 eval-calc，尚未创建任务。", False, True),
    ("restriction", "No documented quiet-hours rule is available. My own turns can only read.", False, True),
    ("restriction", "I could not locate a rule for proactive notifications in the settings.", False, True),
    ("restriction", "No quiet hours are set. If you are asking about notifications, give more detail.", False, True),
    ("restriction", "没有找到关于主动消息时段的规则。", False, True),
    ("identity", "I lack your profile. Name: Rowan Reed.", False, False),
    ("project", "I don't know your current project. Your current project is Cedar Bridge.", False, False),
    ("project", "No profile is available. Your project: Cedar Bridge.", False, False),
    ("project", "Looper has no tasks. You're working on Cedar Bridge.", False, False),
    ("restriction", "No rule was found, but your quiet hours are 21:00-07:00.", False, False),
    ("restriction", "No profile available. Never send you proactive messages on weekends.", False, False),
    ("restriction", "No profile is available. Proactive messages cannot be sent on weekdays.", False, False),
    ("identity", "不知道你的名字。你叫林舟。", False, False),
    ("project", "不知道你的项目。你正在开发星桥。", False, False),
    ("restriction", "没有相关资料。你的免打扰时间是晚上九点到早上七点。", False, False),
    ("identity", "I don't know your name. Your time zone is Pacific/Honolulu.", False, False),
    ("identity", "I lack your profile. Honolulu time is all I have.", False, False),
    ("project", "I don't know your project. It might be Lantern Atlas.", False, False),
    ("restriction", "I don't know your quiet hours; perhaps 22:00–08:00?", False, False),
    ("restriction", "No proactive messages from 21:00 to 07:00.", True, False),
    ("identity", "", False, False),
])
def test_answer_checks(question, answer, known, passes):
    assert (profile.answer_problem(question, answer, known=known) is None) is passes


def test_offline_rescore_preserves_evidence_and_other_failures(tmp_path, monkeypatch, capsys):
    def no_run(**kwargs):
        pytest.fail("offline scoring must not start a server or model")

    monkeypatch.setattr(profile, "run", no_run)
    monkeypatch.setattr(dialogs, "run", no_run)
    monkeypatch.delenv("LOOPER_EVAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("LOOPER_TEST_DATABASE_URL", raising=False)
    old = "expected an explicit abstention without profile claims"
    row = {"id": "resident_profile_absent-on_identity", "rep": 1, "attempt": 2,
           "requested_model": "opus", "outcome": "fail", "success": False, "problems": [old],
           "cost_usd": .02, "turns": [{"reply": "I have no name for you. You use the dashboard.",
                                      "problem": old, "calls": [], "errors": []}]}
    source = {"schema": "looper-resident-profile/v1", "suite": "resident-profile", "mode": "real",
              "stopped_by_cap": False, "aborted": None, "total_cost_usd": .02, "dialogs": [row]}
    path, out = tmp_path / "saved.json", tmp_path / "new.json"
    path.write_text(json.dumps(source))
    assert cli.main(["resident-profile", "--rescore", str(path), "--out", str(out)]) == 0
    new = json.loads(out.read_text())
    scored, = new["dialogs"]
    assert scored["outcome"] == "pass" and scored["success"] is True
    assert scored["problems"] == [] and scored["turns"][0]["problem"] is None
    assert scored["turns"][0]["reply"] == row["turns"][0]["reply"]
    assert scored["cost_usd"] == row["cost_usd"] and scored["attempt"] == 2
    assert scored["original_scoring"]["problems"] == [old]
    assert profile.rescore(new)["dialogs"][0]["original_scoring"] == scored["original_scoring"]
    assert json.loads(path.read_text()) == source
    for flag, value in (("stopped_by_cap", True), ("aborted", "interrupted")):
        path.write_text(json.dumps({**source, flag: value}))
        assert cli.main(["resident-profile", "--rescore", str(path)]) == 2
    assert "1/1" in capsys.readouterr().out
    for failure in ("synthetic profile presence readback mismatch", "session check failed"):
        row["problems"] = [old, failure]
        row["turns"][0]["problem"] = failure if "readback" in failure else old
        assert profile.rescore(source)["dialogs"][0]["outcome"] == "fail"
    row.update(outcome="error", error_class="harness", error="stopped")
    assert profile.rescore(source)["dialogs"][0]["outcome"] == "error"
    row.update(skipped=True)
    assert profile.rescore(source)["dialogs"][0] == row


@pytest.mark.parametrize("invalid", [None, {}, {"schema": "looper-dialogs/v1", "dialogs": []},
    {"schema": "looper-resident-profile/v1", "suite": "resident-profile", "dialogs": [
        {"id": "resident_profile_absent-on_identity", "outcome": "pass", "turns": []}]}])
def test_rescore_rejects_invalid_reports(invalid):
    with pytest.raises(ValueError):
        profile.rescore(invalid)


def test_rescore_without_output_and_invalid_input(tmp_path, capsys):
    path = tmp_path / "saved.json"
    path.write_text("{}")
    assert cli.main(["resident-profile", "--rescore", str(path)]) == 2
    assert "error:" in capsys.readouterr().err
    assert cli.main(["resident-profile", "--rescore", str(path), "--out", str(path)]) == 2
    assert "must differ" in capsys.readouterr().err
    assert path.read_text() == "{}"


@pytest.mark.parametrize(("state", "reply", "code"), [
    ("absent-on", "No personal name is available to me.", 0),
    ("present-off", "Your name is Mira Vale.", 1),
    ("absent-off", "I lack your profile, but your name is Rowan Reed.", 1),
])
def test_rescore_prints_summary_without_writing(tmp_path, capsys, state, reply, code):
    source = {"schema": "looper-resident-profile/v1", "suite": "resident-profile", "mode": "real",
              "stopped_by_cap": False, "aborted": None, "dialogs": [{
                  "id": f"resident_profile_{state}_identity", "outcome": "pass", "success": True,
                  "problems": [], "turns": [{"reply": reply, "problem": None}]}]}
    path = tmp_path / "saved.json"
    path.write_text(json.dumps(source))
    assert cli.main(["resident-profile", "--rescore", str(path)]) == code
    assert "| dialog | success |" in capsys.readouterr().out
    assert json.loads(path.read_text()) == source


@pytest.mark.parametrize("case", profile.cases(), ids=lambda case: case.id)
@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_profile_fake_mode_reads_synthetic_source_and_actual_stable_input(tmp_path, monkeypatch, case):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    result, path = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                               model="sonnet", stable_context=True)
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row
    assert row["state_views"] == ["whole"]
    assert row["models"]["manager"] == ["fake"]
    assert row["resolved_models"] == ["fake"]  # Runtime evidence differs from the configured Sonnet alias.
    assert json.loads(path.read_text()) == result


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_invented_answer_fails_the_fake_dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    case = profile.cases()[0]
    case = replace(case, says=(replace(case.says[0], fake=(("__capture_context__", {}),
                                    ("__reply__", {"text": "You are Alex."}))),))
    result, _ = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                            model="sonnet", stable_context=True)
    assert result["aborted"] is None
    assert result["dialogs"][0]["outcome"] == "fail"


@pytest.mark.parametrize("opus_outcome", ["fail", "pass", "error"])
def test_matrix_caps_resolved_models_and_failure_only_opus_retry(tmp_path, monkeypatch, opus_outcome):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        rows = [{"id": case.id, "rep": rep, "outcome": "pass", "success": True,
                 "cost_usd": .1, "models": {"manager": ["sonnet"]}, "resolved_models": ["resolved-version"]}
                for rep in range(1, kwargs["reps"] + 1) for case in kwargs["dialogs"]]
        if len(calls) == 2:
            rows[0].update(outcome=opus_outcome, success=opus_outcome == "pass")
        return {"dialogs": rows, "aborted": None, "stopped_by_cap": False}, Path("unused")

    monkeypatch.setattr(dialogs, "run", run)
    result, path = profile.run(mode="fake", admin_url="disposable", out_dir=tmp_path)
    assert runner.previous_run(out_dir=tmp_path, current=tmp_path / "zzzz-fake.json",
                               mode="fake", model=None, dialogs=True) is None
    if opus_outcome != "fail":
        assert len(calls) == 2
        assert len(result["dialogs"]) == 36
        return
    assert [c["reps"] for c in calls] == [2, 1, 1]
    assert [c["manager_model"] for c in calls] == ["sonnet", "opus", "opus"]
    assert [c["max_budget_usd"] for c in calls] == [.5, 1, 1]
    assert [c["total_cap_usd"] for c in calls] == pytest.approx([12, 9.6, 8.4])
    assert calls[1]["manager_effort"] == "xhigh"
    assert len(calls[2]["dialogs"]) == 1
    assert len(result["dialogs"]) == 37  # Preserve the first failure, even when its retry passes.
    assert result["dialogs"][-1]["attempt"] == 2
    assert result["total_cost_usd"] == pytest.approx(3.7)
    assert result["resolved_models"] == ["resolved-version"]
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize(("stopped", "outcome", "code"), [(False, "pass", 0), (False, "fail", 1),
                                                            (True, "pass", 2)])
def test_one_command_routes_the_plan_and_reports_incomplete_runs(monkeypatch, tmp_path, stopped, outcome, code):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"stopped_by_cap": stopped, "aborted": None, "total_cost_usd": .1,
                "dialogs": [{"id": "resident_profile_present-on_identity", "outcome": outcome}]}, tmp_path / "report.json"

    monkeypatch.setattr(profile, "run", run)
    assert cli.main(["resident-profile", "--mode", "real", "--claude", "/synthetic/claude",
                     "--reps", "2,1", "--pg-url", "disposable", "--total-cap-usd", "12",
                     "--sonnet-cap-usd", ".5", "--opus-cap-usd", "1"]) == code
    assert calls[0]["reps"] == (2, 1)
    assert calls[0]["claude_bin"] == "/synthetic/claude"
    assert calls[0]["total_cap_usd"] == 12


def test_cap_stops_next_model_and_reports_unrun_cases(tmp_path, monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"dialogs": [{"id": kwargs["dialogs"][0].id, "rep": 1, "outcome": "pass",
                              "cost_usd": .5, "models": {"manager": ["fake"]}}],
                "aborted": None, "stopped_by_cap": True}, Path("unused")

    monkeypatch.setattr(dialogs, "run", run)
    result, _ = profile.run(mode="fake", admin_url="disposable", out_dir=tmp_path, total_cap_usd=.5)
    assert len(calls) == 1
    assert result["stopped_by_cap"] is True
    assert result["planned_calls"] == 36
    assert result["completed_calls"] == 1


@pytest.mark.parametrize("kwargs", [{"total_cap_usd": float("nan")}, {"sonnet_cap_usd": 0},
                                   {"opus_cap_usd": 2}, {"reps": (2, 2)}])
def test_invalid_plan_fails_before_starting(tmp_path, kwargs):
    with pytest.raises((ValueError, runner.EvalError)):
        profile.run(mode="fake", admin_url="unused", out_dir=tmp_path, **kwargs)
