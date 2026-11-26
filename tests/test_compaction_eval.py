"""Compaction scenarios exercise the resident CLI transport with deterministic boundaries."""

import json
from types import SimpleNamespace

import pytest

from evals.prompt_regression import __main__ as cli, dialogs, fake_claude, runner, tasks


@pytest.mark.parametrize("command", ["run", "dialogs"])
def test_compaction_restore_is_an_explicit_eval_option(command):
    assert cli._parser().parse_args([command]).compaction_restore is False
    assert cli._parser().parse_args([command, "--compaction-restore"]).compaction_restore is True


def test_compaction_cases_include_real_mode_controls():
    gate, schedule = tasks.select(["compaction_gate", "compaction_schedule_keep"])
    assert gate.planner_env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"}
    assert gate.gate_script[1] == "<await-compaction>"
    assert len(gate.files["docs/REFERENCE.md"]) >= 200_000
    assert schedule.schedule_fires == 2
    for dialog in dialogs.select(["compaction_task_confirm", "compaction_ops_confirm",
                                  "compaction_ops_decline", "compaction_failed"]):
        assert dialog.compaction["session_context_max_tokens"] == 40000
        assert dialog.compaction["turn_budget_usd"] == 20


def test_compaction_row_fields_come_from_record_evidence():
    rows = [{"input_text": "/compact", "compactions": [{"trigger": "manual"}],
             "metadata": {"compaction_restore": {"state": "due"}}},
            {"input_text": "hello", "metadata": {"compaction_restore": {"state": "sent"}}}]
    assert runner.compaction_fields(rows) == {"compactions": 1, "restore_due": 1,
        "restore_offered": 0, "restore_sent": 1, "keep_compacts": 0}


def test_fake_boundary_has_native_stream_shape(monkeypatch):
    emitted = []
    monkeypatch.setattr(fake_claude, "_emit", emitted.append)
    fake_claude._compact_boundary("fake-manager", trigger="manual", pre_tokens=50001, post_tokens=1200)
    assert emitted == [{"type": "system", "subtype": "compact_boundary", "session_id": "fake-manager",
                        "compact_metadata": {"trigger": "manual", "pre_tokens": 50001, "post_tokens": 1200}}]


@pytest.mark.parametrize("failed", [False, True])
def test_fake_manager_compaction_prefix_emits_boundary_or_failure(monkeypatch, tmp_path, failed):
    emitted = []
    answers = []
    monkeypatch.setattr(fake_claude, "_emit", emitted.append)
    monkeypatch.setattr(fake_claude, "_answer", lambda *args, **kwargs: answers.append(args))
    monkeypatch.setattr(fake_claude, "_mcp", lambda _: None)
    scenarios = tmp_path / "scenarios.json"
    directive = "__compact_fail__" if failed else "__compact__"
    scenarios.write_text(json.dumps({"manager_prefix": {"/compact": [[directive,
        {} if failed else {"pre_tokens": 50001, "post_tokens": 1200}]]}}))

    class Input:
        lines = iter([json.dumps({"message": {"content": "/compact keep these facts"}})])

        def readline(self):
            return next(self.lines, None)

    fake_claude._manager(str(scenarios), [], Input())
    assert bool([event for event in emitted if event.get("subtype") == "compact_boundary"]) is not failed
    if failed:
        assert emitted[-1]["compact_result"] == "failed"
        assert emitted[-1]["compact_error"] == "too_few_groups"
    assert len(answers) == 1


def test_gate_compaction_timeout_cannot_be_exercised_by_a_later_boundary():
    task = tasks.select(["compaction_gate"])[0]
    windows = {"g1": {"after_record": 1, "sent_at": 10}}
    answered = {"g1": 1}
    posts = []

    class Api:
        def post(self, path, body):
            posts.append((path, body))
            return {"result": {"records": []}}

    detail = {"gates": [{"step": "g1", "waiting_on": "user", "opened_at": None}]}
    assert not runner.answer_gates(Api(), task, "task-a", detail, answered, now=309, compaction_windows=windows)
    assert runner.answer_gates(Api(), task, "task-a", detail, answered, now=310, compaction_windows=windows)
    assert windows["g1"]["timed_out"] is True
    assert posts[-1][1]["text"].startswith("File name:")
    assert all(body.get("text") != "<await-compaction>" for _, body in posts)


def test_dialog_filler_fits_the_chat_schema_and_is_about_forty_kilobytes():
    dialog = dialogs.select(["compaction_task_confirm"])[0]
    assert len(dialog.says[0].text) <= 32000
    assert 40000 <= len(dialog.says[0].text.encode()) <= 45000


def test_failed_compaction_evidence_skips_the_rotation_handoff():
    rows = [{"id": 1, "input_text": "/compact", "metadata": {}},
            {"id": 2, "input_text": "[HANDOFF] end this session", "metadata": {}},
            {"id": 3, "input_text": "[RESUME] new session", "metadata": {}}]
    assert dialogs._compaction_evidence(rows, enabled=False, failed=True) is None
    rows[-1]["input_text"] = "continue"
    assert dialogs._compaction_evidence(rows, enabled=False, failed=True) is not None


@pytest.mark.parametrize("fault", [None, "deleted", "confirm_error", "write"])
def test_compaction_decline_keeps_schedule_and_never_calls_confirm(monkeypatch, fault):
    monkeypatch.setattr(dialogs, "_schedule", lambda _: None if fault == "deleted" else {"enabled": True})
    actions = [{"tool": "looper_confirm", "risk": "read", "status": "error"}] if fault == "confirm_error" else []
    if fault == "write":
        actions = [{"tool": "looper_schedules", "risk": "high", "status": "needs_confirmation"}]
    assert (dialogs._compaction_declined(SimpleNamespace(turn_actions=lambda: actions)) is None) is (fault is None)


def test_gate_evidence_uses_the_script_window_boundary_when_a_later_turn_compacts():
    rows = [{"id": 1, "agent_id": "planner-a", "compactions": [{"trigger": "auto"}]},
            {"id": 2, "agent_id": "planner-a", "input_text": "[COMPACTED]\nGates and waits:\nFormat: TOML. [service]",
             "metadata": {"compaction_restore": {"state": "sent"}}},
            {"id": 3, "agent_id": "planner-a", "compactions": [{"trigger": "auto"}]}]
    assert runner.task_compaction_evidence(rows, enabled=True, gate=True, boundary_record_id=1) is None
