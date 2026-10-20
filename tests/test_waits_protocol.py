"""Wait steps, their replies and the system limits that check them."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from looper.agents import waits
from looper.agents.project_execution import ProjectExecution
from looper.agents.tiers.planner_protocol import (
    DecisionContext, PlanView, ProtocolError, ResultKind, TurnKind, WaitContext,
    parse_chat, parse_decision, parse_plan, parse_wait, reply_contract,
)
from looper.agents.user_gates import GatePolicy, GateState, GateView
from looper.agents.waits import (
    CommandCheck, GithubPrCheck, ReadyWhen, STEP_STATUS, Wait, WaitAction, WaitChangeAction,
    WaitLimits, WaitOutcome, WaitPolicy, WaitSpec, WaitState, WaitView,
    check_from_row, describe, effective_every, open_times,
)


def reply(data: Any) -> str:
    return '```json\n' + json.dumps(data) + '\n```'


def step(**changes: Any) -> dict[str, Any]:
    return {"id": "w1", "kind": "wait", "topic": "Author pushes fixes", "depends_on": [],
            "max_wait_seconds": 3600, "not_before_seconds": 30, **changes}


def checked(**changes: Any) -> dict[str, Any]:
    value = step(**{"check": {"type": "github_pr", "repo": "owner/name", "number": 12,
                              "ready_when": "head_changed"}, **changes})
    if "not_before_seconds" not in changes:
        value.pop("not_before_seconds")
    return value


def plan(value: dict[str, Any], policy: WaitPolicy = WaitPolicy()) -> Any:
    return parse_plan(reply({"steps": [value]}), wait_policy=policy)


@pytest.mark.parametrize("value", [step(), checked(), checked(every_seconds=600),
    step(not_before="2026-10-20T09:00:00+00:00"),
    checked(check={"type": "github_pr", "repo": "a.b/c-d", "number": 1, "ready_when": "checks_finished"}),
    checked(check={"type": "github_pr", "repo": "a_b/c.d", "number": 1, "ready_when": "new_comments"}),
])
def test_valid_wait_steps(value: dict[str, Any]) -> None:
    if "not_before" in value:
        value.pop("not_before_seconds")
        # Absolute validation uses the current clock; keep this case independent of calendar time.
        value["not_before"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    spec = plan(value).steps[0]
    assert spec.wait.topic == value["topic"]
    assert spec.instruction == spec.wait.topic and not spec.review and not spec.acceptance_criteria
    assert spec.gate is None


@pytest.mark.parametrize("key", ["id", "kind", "topic", "depends_on", "max_wait_seconds"])
def test_wait_required_fields(key: str) -> None:
    value = step()
    value.pop(key)
    with pytest.raises(ProtocolError, match=key if key != "kind" else "missing"):
        plan(value)


@pytest.mark.parametrize(("changes", "error"), [
    ({"id": []}, ".id must be a short label"),
    ({"topic": ""}, ".topic must be a non-empty string"),
    ({"topic": 7}, ".topic must be a non-empty string"),
    ({"topic": "x" * 201}, ".topic has 201 characters; at most 200"),
    ({"depends_on": "s1"}, ".depends_on must be a list"),
    ({"depends_on": [1]}, ".depends_on must be a list"),
    ({"depends_on": ["s1", "s1"]}, ".depends_on lists a step twice"),
    ({"instruction": "poll"}, "is a wait, which has only"),
    ({"review": False}, "not review"),
    ({"role": "poller"}, "not role"),
    ({"max_wait_seconds": True}, ".max_wait_seconds must be a whole number"),
    ({"max_wait_seconds": 0}, ".max_wait_seconds must be a whole number"),
    ({"max_wait_seconds": 1.5}, ".max_wait_seconds must be a whole number"),
    ({"not_before_seconds": True}, ".not_before_seconds must be a whole number"),
    ({"not_before_seconds": -1}, ".not_before_seconds must be a whole number"),
    ({"not_before_seconds": 3601}, ".not_before_seconds"),
    ({"not_before": "2026-10-20T09:00:00Z"}, "not_before_seconds and not_before"),
    ({"every_seconds": 300}, "every_seconds is only for a wait with a check"),
    ({"not_before_seconds": 0}, "a wait without a check needs not_before_seconds"),
    ({"check": None}, ".check must be an object"),
    ({"check": {}}, ".check.type"),
    ({"check": {"type": "http"}}, ".check.type"),
    ({"check": {"type": "command"}}, ".check.command"),
    ({"check": {"type": "command", "command": ""}}, ".check.command"),
    ({"check": {"type": "command", "command": "x" * 2001}}, "at most 2000"),
    ({"check": {"type": "command", "command": "true", "extra": 1}}, "unknown keys: extra"),
])
def test_bad_wait_fields(changes: dict[str, Any], error: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(error)):
        plan(step(**changes))


@pytest.mark.parametrize("value", [None, 1, "tomorrow", "2026-10-20T09:00:00"])
def test_absolute_time_needs_iso_string_and_timezone(value: Any) -> None:
    raw = step(not_before=value)
    raw.pop("not_before_seconds")
    with pytest.raises(ProtocolError, match="not_before"):
        plan(raw)


@pytest.mark.parametrize(("changes", "error"), [
    ({"repo": "owner/name/extra"}, ".check.repo"), ({"repo": "-x"}, ".check.repo"),
    ({"repo": []}, ".check.repo"), ({"number": True}, ".check.number"),
    ({"number": 0}, ".check.number"), ({"number": "12"}, ".check.number"),
    ({"ready_when": "merged"}, 'head_changed, checks_finished or new_comments'),
    ({"extra": True}, "unknown keys: extra"),
])
def test_bad_github_check_fields(changes: dict[str, Any], error: str) -> None:
    raw = checked()
    raw["check"].update(changes)
    with pytest.raises(ProtocolError, match=re.escape(error)):
        plan(raw)


@pytest.mark.parametrize("missing", ["repo", "number", "ready_when"])
def test_github_check_required_fields(missing: str) -> None:
    raw = checked()
    raw["check"].pop(missing)
    with pytest.raises(ProtocolError, match=missing):
        plan(raw)


POLICY = WaitPolicy(WaitLimits(3600, 60), commands=False)


@pytest.mark.parametrize(("raw", "error"), [
    (step(max_wait_seconds=3601), "3600"), (checked(every_seconds=59), "60"),
    (checked(every_seconds=3601), "3600"), (checked(every_seconds=True), "whole number"),
    (checked(every_seconds=0), "whole number"), (checked(every_seconds=1.5), "whole number"),
    (step(not_before=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat()), "3600"),
    (checked(check={"type": "command", "command": "true"}), WaitPolicy.command_refusal),
])
def test_wait_policy_limits(raw: dict[str, Any], error: str) -> None:
    if "not_before" in raw:
        raw.pop("not_before_seconds")
    with pytest.raises(ProtocolError, match=re.escape(error)):
        plan(raw, POLICY)


def test_command_checks_require_project_permission() -> None:
    raw = checked(check={"type": "command", "command": "test -f ready"})
    assert plan(raw, replace(POLICY, commands=True)).steps[0].wait.check == CommandCheck("test -f ready")
    assert not ProjectExecution().wait_commands_on
    assert ProjectExecution.from_row({"wait_commands": "on"}).wait_commands_on


OPEN = PlanView(started={"w1": ()}, pending={}, waits={"w1": WaitView(WaitState.WAITING)}, wait_policy=POLICY)
CANCEL = {"id": "w1", "action": "cancel", "resolution": "No longer needed"}
FINAL = {"delivered": "done", "how_to_use": "read", "verification": "checked", "open_issues": "none"}


@pytest.mark.parametrize("state", [s for s in WaitState if not s.closed])
def test_cancel_wait_counts_as_done_and_requires_final(state: WaitState) -> None:
    view = replace(OPEN, waits={"w1": WaitView(state)})
    data = {"reply": "ok", "action": "none", "waits": [CANCEL]}
    with pytest.raises(ProtocolError, match="these cancels leave no step open"):
        parse_chat(reply(data), view)
    result = parse_chat(reply({**data, "final": FINAL}), view)
    assert result.waits[0].action is WaitChangeAction.CANCEL and result.final is not None


@pytest.mark.parametrize(("changes", "error"), [
    (None, "waits must be a list"), ([None], "waits[0] must be an object"),
    ([{**CANCEL, "id": "x"}], "not a wait"), ([{**CANCEL, "id": []}], "not a wait"),
    ([CANCEL, CANCEL], "changed twice"), ([{**CANCEL, "action": "proceed"}], '.action must be "cancel"'),
    ([{"id": "w1", "action": "cancel"}], ".resolution must be a non-empty string"),
    ([{**CANCEL, "resolution": "x" * 1001}], "at most 1000"),
    ([{**CANCEL, "other": True}], "unknown keys: other"),
])
def test_bad_wait_changes(changes: Any, error: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(error)):
        parse_chat(reply({"reply": "ok", "action": "none", "waits": changes}), OPEN)


@pytest.mark.parametrize("state", [WaitState.DONE, WaitState.FAILED, WaitState.SKIPPED])
def test_closed_wait_cannot_be_cancelled(state: WaitState) -> None:
    with pytest.raises(ProtocolError, match="pending, waiting, ready, timed_out or cancel_requested"):
        parse_chat(reply({"reply": "ok", "action": "none", "waits": [CANCEL]}),
                   replace(OPEN, waits={"w1": WaitView(state)}))


def test_cancel_wait_and_gate_together_complete_a_decision() -> None:
    view = replace(OPEN, started={"w1": (), "g1": (), "s1": ()},
                   gates={"g1": GateView(GateState.OPEN)})
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0)
    data = {"decision": "accept", "waits": [CANCEL],
            "gates": [{"id": "g1", "action": "cancel", "resolution": "answered"}]}
    with pytest.raises(ProtocolError, match="this decision completes the task"):
        parse_decision(reply(data), context, view)
    result = parse_decision(reply({**data, "final": FINAL}), context, view)
    assert result.final is not None and result.waits and result.gates


def test_open_wait_replan_keeps_it_and_pending_ids_keep_kind() -> None:
    with pytest.raises(ProtocolError, match='wait w1 is open: a replan keeps it; end it with "waits"'):
        parse_chat(reply({"reply": "ok", "action": "replan", "steps": [step()]}), OPEN)
    work = {"id": "s2", "instruction": "go", "depends_on": ["w1"], "acceptance_criteria": [], "review": False}
    assert parse_chat(reply({"reply": "ok", "action": "replan", "steps": [work]}), OPEN).steps[0].depends_on == ("w1",)
    pending = replace(OPEN, started={}, pending={"w1": ()}, waits={"w1": WaitView(WaitState.PENDING)})
    work["id"], work["depends_on"] = "w1", []
    with pytest.raises(ProtocolError, match="step w1 is a wait; use a new id"):
        parse_chat(reply({"reply": "ok", "action": "replan", "steps": [work]}), pending)
    pending = replace(pending, waits={})
    with pytest.raises(ProtocolError, match="step w1 is a work step; use a new id"):
        parse_chat(reply({"reply": "ok", "action": "replan", "steps": [step()]}), pending)


@pytest.mark.parametrize(("wait", "action"), [
    ({"action": "extend", "max_wait_seconds": 3600}, WaitAction.EXTEND),
    ({"action": "proceed", "resolution": "go"}, WaitAction.PROCEED),
    ({"action": "fail", "reason": "cannot go"}, WaitAction.FAIL),
])
def test_wait_turn_actions(wait: dict[str, Any], action: WaitAction) -> None:
    # Another step stays open, so proceeding does not complete the task.
    view = replace(OPEN, pending={"s2": ()})
    result = parse_wait(reply({"reply": "ok", "action": "none", "wait": wait}),
                        WaitContext("w1", WaitState.TIMED_OUT), view)
    assert result.action is action and result.chat.reply == "ok"
    assert result.max_wait_seconds == wait.get("max_wait_seconds")
    assert result.resolution == wait.get("resolution", "") and result.reason == wait.get("reason", "")
    assert TurnKind.WAIT.value == "WAIT"


@pytest.mark.parametrize(("wait", "error"), [
    (None, "wait must be an object"), ({"action": "unknown"}, "wait.action"),
    ({"action": "extend", "max_wait_seconds": 3601}, "3600"),
    ({"action": "extend", "max_wait_seconds": True}, "whole number"),
    ({"action": "extend", "max_wait_seconds": 0}, "whole number"),
    ({"action": "extend"}, "max_wait_seconds"),
    ({"action": "proceed"}, "resolution"), ({"action": "fail"}, "reason"),
    ({"action": "proceed", "resolution": "x" * 1001}, "at most 1000"),
    ({"action": "fail", "reason": "x" * 1001}, "at most 1000"),
    ({"action": "extend", "max_wait_seconds": 60, "reason": "x"}, "unknown keys"),
    ({"action": "proceed", "resolution": "go", "max_wait_seconds": 60}, "unknown keys"),
    ({"action": "fail", "reason": "no", "resolution": "go"}, "unknown keys"),
])
def test_bad_wait_turns(wait: Any, error: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(error)):
        parse_wait(reply({"reply": "ok", "action": "none", "wait": wait}),
                   WaitContext("w1", WaitState.TIMED_OUT), OPEN)


def test_user_cancelled_wait_cannot_be_extended() -> None:
    with pytest.raises(ProtocolError, match="the user cancelled this wait: proceed, fail, or change the plan"):
        parse_wait(reply({"reply": "ok", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 60}}),
                   WaitContext("w1", WaitState.CANCEL_REQUESTED), OPEN)


def test_wait_final_only_when_proceeding_completes_task() -> None:
    data = {"reply": "ok", "action": "none", "wait": {"action": "proceed", "resolution": "go"}}
    context = WaitContext("w1", WaitState.TIMED_OUT, last_open=True)
    with pytest.raises(ProtocolError, match='proceeding without adding steps completes the task'):
        parse_wait(reply(data), context, OPEN)
    assert parse_wait(reply({**data, "final": FINAL}), context, OPEN).final is not None
    # The plan after proceed still has a step, so final is refused even though the turn was told it was last.
    staying = replace(OPEN, pending={"s2": ()})
    with pytest.raises(ProtocolError, match="final is only"):
        parse_wait(reply({**data, "final": FINAL}), context, staying)
    # Judged on the plan after proceed: an empty plan needs final even when last_open was false.
    with pytest.raises(ProtocolError, match='proceeding without adding steps completes the task'):
        parse_wait(reply(data), replace(context, last_open=False), OPEN)
    work = {"id": "s2", "instruction": "go", "depends_on": ["w1"], "acceptance_criteria": [], "review": False}
    assert parse_wait(reply({**data, "action": "add_steps", "steps": [work]}), context, OPEN).final is None


def test_proceed_plus_replan_that_leaves_nothing_requires_final() -> None:
    """A timed-out wait plus one pending step: proceed and a replan of no steps completes the task."""
    view = replace(OPEN, pending={"s2": ("w1",)}, waits={"w1": WaitView(WaitState.TIMED_OUT)})
    data = {"reply": "done waiting", "action": "replan", "steps": [],
            "wait": {"action": "proceed", "resolution": "the fix landed"}}
    context = WaitContext("w1", WaitState.TIMED_OUT, last_open=False)
    with pytest.raises(ProtocolError, match='proceeding without adding steps completes the task'):
        parse_wait(reply(data), context, view)
    result = parse_wait(reply({**data, "final": FINAL}), context, view)
    assert result.final is not None and result.action is WaitAction.PROCEED
    assert result.chat.action.value == "replan" and result.chat.steps == ()


def test_proceed_plus_replan_stop_that_leaves_nothing_requires_final() -> None:
    view = replace(OPEN, started={"w1": (), "s1": ()}, running=frozenset({"s1"}),
                   waits={"w1": WaitView(WaitState.TIMED_OUT)})
    data = {"reply": "stop it", "action": "replan", "steps": [], "stop": ["s1"],
            "wait": {"action": "proceed", "resolution": "go"}}
    context = WaitContext("w1", WaitState.TIMED_OUT, last_open=False)
    with pytest.raises(ProtocolError, match='proceeding without adding steps completes the task'):
        parse_wait(reply(data), context, view)
    assert parse_wait(reply({**data, "final": FINAL}), context, view).final is not None


def test_proceed_plus_replan_that_leaves_a_step_refuses_final() -> None:
    """Final follows the plan after proceed and the replan, including a step that was not stopped."""
    work = {"id": "s3", "instruction": "go", "depends_on": [], "acceptance_criteria": [], "review": False}
    view = replace(OPEN, pending={"s2": ("w1",)}, waits={"w1": WaitView(WaitState.TIMED_OUT)})
    data = {"reply": "keep going", "action": "replan", "steps": [work],
            "wait": {"action": "proceed", "resolution": "the fix landed"}, "final": FINAL}
    context = WaitContext("w1", WaitState.TIMED_OUT, last_open=True)
    with pytest.raises(ProtocolError, match="final is only"):
        parse_wait(reply(data), context, view)
    kept = parse_wait(reply({key: value for key, value in data.items() if key != "final"}), context, view)
    assert kept.final is None and kept.chat.steps[0].id == "s3"

    running = replace(OPEN, started={"w1": (), "s1": ()}, running=frozenset({"s1"}),
                      waits={"w1": WaitView(WaitState.TIMED_OUT)})
    empty = {"reply": "leave s1", "action": "replan", "steps": [],
             "wait": {"action": "proceed", "resolution": "go"}, "final": FINAL}
    with pytest.raises(ProtocolError, match="final is only"):
        parse_wait(reply(empty), context, running)
    left = parse_wait(reply({key: value for key, value in empty.items() if key != "final"}), context, running)
    assert left.final is None and left.chat.steps == () and "s1" not in left.chat.stop


def test_wait_contract_in_every_contract_with_conditional_command_and_live_limits() -> None:
    from looper.agents.tiers.planner_protocol import _WAIT_RULES
    assert _WAIT_RULES in reply_contract()
    assert '"kind": "wait"' in reply_contract(gates=True, roles=True, room=True)
    clause = ', or {"type": "command", "command": "..."}: a shell command run in the working directory, exit 0 is ready'
    assert clause not in reply_contract() and clause in reply_contract(wait_commands=True)
    text = reply_contract(wait_limits=WaitLimits(3600, 60))
    assert 'every_seconds (at least 60, default 600)' in text and '(at most 3600)' in text
    assert 'every_seconds (at least 900, default 900)' in reply_contract(wait_limits=WaitLimits(3600, 900))
    assert '[WAIT]:' not in text


@pytest.mark.parametrize(("spec", "limits", "expected"), [
    (WaitSpec("time", 60, not_before_seconds=10), WaitLimits(), None),
    (WaitSpec("check", 60, check=CommandCheck("true")), WaitLimits(), 300),
    (WaitSpec("check", 3600, check=CommandCheck("true")), WaitLimits(), 600),
    (WaitSpec("check", 3600, check=CommandCheck("true"), every_seconds=900), WaitLimits(), 900),
])
def test_effective_every(spec: WaitSpec, limits: WaitLimits, expected: int | None) -> None:
    assert effective_every(spec, limits) == expected


def test_wait_times_rows_descriptions_and_retry() -> None:
    opened = datetime(2026, 10, 19, tzinfo=timezone.utc)
    spec = WaitSpec("fixes", 3 * 86400, check=GithubPrCheck("owner/name", 12, ReadyWhen.HEAD_CHANGED))
    assert open_times(spec, opened) == (opened, opened + timedelta(days=3), opened)
    assert open_times(replace(spec, not_before_seconds=90), opened)[0] == opened + timedelta(seconds=90)
    absolute = opened + timedelta(hours=1)
    assert open_times(replace(spec, not_before_seconds=90, not_before=absolute), opened)[0] == absolute
    assert WaitSpec.from_row(spec.topic, spec.row()) == spec
    assert check_from_row(None) is None
    assert GithubPrCheck.from_row(spec.check.row()) == spec.check
    assert CommandCheck.from_row(CommandCheck("true").row()) == CommandCheck("true")
    assert CommandCheck("x" * 100).describe() == 'command `' + 'x' * 80 + '`'
    assert describe(spec, 600) == 'github_pr owner/name#12 head_changed every 10 min; at most 3 d'
    assert describe(WaitSpec("time", 86400, not_before=absolute), None) == 'until 2026-10-19 01:00 UTC; at most 1 d'
    wait = Wait(spec, state=WaitState.DONE, outcome=WaitOutcome.READY, opened_at=opened, closed_at=absolute)
    row = wait.row("step-id")
    assert row["spec"]["every_seconds"] == 600 and row["step_id"] == "step-id"
    restored = Wait.from_row(row)
    assert restored.spec == replace(spec, every_seconds=600) and restored.state is WaitState.DONE
    assert wait.retry_at() is None
    wait.failed_at, wait.failures = opened, 1
    assert wait.retry_at() == opened + timedelta(seconds=60)
    wait.failures = 2
    assert wait.retry_at() == opened + timedelta(seconds=300)
    wait.failures = 3
    assert wait.retry_at() is None


@pytest.mark.parametrize("state", list(WaitState))
def test_wait_states(state: WaitState) -> None:
    assert state.open == (state in (WaitState.WAITING, WaitState.READY, WaitState.TIMED_OUT, WaitState.CANCEL_REQUESTED))
    assert state.closed == (state in (WaitState.DONE, WaitState.FAILED, WaitState.SKIPPED))
    assert state.needs_planner == (state in (WaitState.READY, WaitState.TIMED_OUT, WaitState.CANCEL_REQUESTED))
    assert state.step_status == STEP_STATUS[state]
    assert state.step_status == ('pending' if state is WaitState.PENDING else 'verified' if state is WaitState.DONE
                                 else state.value if state.closed else 'waiting')


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    async def get(keys: Any) -> dict[str, str]:
        return {key: store[key] for key in keys if key in store}
    async def put(values: Any) -> None:
        for key, value in values.items():
            if value is None:
                store.pop(key, None)
            else:
                store[key] = value
    monkeypatch.setattr(waits.db_settings, "get_settings", get)
    monkeypatch.setattr(waits.db_settings, "put_settings", put)
    asyncio.run(waits.load())
    yield store
    store.clear()
    asyncio.run(waits.load())


def test_wait_settings_load_update_and_reset(settings: dict[str, str]) -> None:
    async def run() -> None:
        assert waits.limits() == WaitLimits()
        view = await waits.update({"max_wait_seconds": 3600, "min_check_seconds": 60})
        assert waits.limits() == WaitLimits(3600, 60)
        assert view["max_wait_seconds"] == {"value": 3600, "default": 604800, "min": 60, "max": 2592000, "source": "setting"}
        assert settings == {waits.MAX_WAIT_KEY: "3600", waits.MIN_CHECK_KEY: "60"}
        view = await waits.update({"min_check_seconds": None, "max_wait_seconds": None})
        assert waits.limits() == WaitLimits() and settings == {}
        assert view["min_check_seconds"]["source"] == "built-in"
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    {"unknown": 60}, {}, {"max_wait_seconds": True}, {"max_wait_seconds": 59},
    {"max_wait_seconds": 2592001}, {"max_wait_seconds": 60.5}, {"max_wait_seconds": "60"},
    {"min_check_seconds": 9}, {"min_check_seconds": 86401}, {"min_check_seconds": False},
    {"max_wait_seconds": 60, "min_check_seconds": 61},
])
def test_bad_wait_settings_store_nothing(settings: dict[str, str], body: dict[str, Any]) -> None:
    async def run() -> None:
        with pytest.raises(ValueError):
            await waits.update(body)
        assert settings == {} and waits.limits() == WaitLimits()
    asyncio.run(run())


def test_wait_settings_validate_the_result_including_unchanged_fields(settings: dict[str, str]) -> None:
    async def run() -> None:
        await waits.update({"max_wait_seconds": 3600, "min_check_seconds": 60})
        before = dict(settings)
        with pytest.raises(ValueError, match="min_check_seconds.*max_wait_seconds"):
            await waits.update({"min_check_seconds": 3601})
        assert settings == before
    asyncio.run(run())


@pytest.mark.parametrize("value", ["bad", "1.5", "True", "0", "2592001"])
def test_invalid_stored_wait_settings_fall_back_with_warning(settings: dict[str, str], caplog: Any, value: str) -> None:
    settings[waits.MAX_WAIT_KEY] = value
    asyncio.run(waits.load())
    assert waits.limits().max_wait_seconds == WaitLimits().max_wait_seconds
    assert waits.MAX_WAIT_KEY in caplog.text


@pytest.mark.parametrize("key", ["acceptance_criteria", "review_roles", "findings", "agenda"])
def test_wait_rejects_other_step_kinds_fields(key: str) -> None:
    with pytest.raises(ProtocolError, match="not " + key):
        plan(step(**{key: []}))


@pytest.mark.parametrize("raw", [None, "30", 1.5])
def test_relative_time_must_be_a_whole_number(raw: Any) -> None:
    with pytest.raises(ProtocolError, match="not_before_seconds must be a whole number"):
        plan(step(not_before_seconds=raw))


@pytest.mark.parametrize("raw", [None, True, 1, "   "])
def test_command_must_be_nonempty_text(raw: Any) -> None:
    with pytest.raises(ProtocolError, match="check.command must be a non-empty string"):
        plan(checked(check={"type": "command", "command": raw}), replace(POLICY, commands=True))


def test_check_and_time_only_wait_boundaries() -> None:
    assert plan(step(max_wait_seconds=1, not_before_seconds=1)).steps[0].wait.max_wait_seconds == 1
    assert plan(checked(max_wait_seconds=1, not_before_seconds=0)).steps[0].wait.not_before_seconds == 0
    assert len(plan(checked(check={"type": "command", "command": "x" * 2000}),
                    replace(POLICY, commands=True)).steps[0].wait.check.command) == 2000
    assert plan(step(topic="x" * 200)).steps[0].wait.topic == "x" * 200


@pytest.mark.parametrize("state", [WaitState.READY, WaitState.TIMED_OUT, WaitState.CANCEL_REQUESTED])
def test_a_wait_that_needs_the_planner_is_still_started(state: WaitState) -> None:
    with pytest.raises(ProtocolError, match='wait w1 is open'):
        parse_chat(reply({"reply": "ok", "action": "replan", "steps": [step()]}),
                   replace(OPEN, waits={"w1": WaitView(state)}))


def test_cancelling_a_pending_wait_lets_new_steps_depend_on_it() -> None:
    view = replace(OPEN, started={}, pending={"w1": ()}, waits={"w1": WaitView(WaitState.PENDING)})
    work = {"id": "s2", "instruction": "go", "depends_on": ["w1"], "acceptance_criteria": [], "review": False}
    parsed = parse_chat(reply({"reply": "go", "action": "replan", "steps": [work], "waits": [CANCEL]}), view)
    assert parsed.steps[0].depends_on == ("w1",) and parsed.final is None


@pytest.mark.parametrize("raw", [True, "yes", 1])
def test_bad_project_wait_command_setting_is_refused(raw: Any) -> None:
    from looper.agents.project_execution import execution_fields
    with pytest.raises(ValueError, match="wait_commands"):
        execution_fields({"wait_commands": raw})


def _run_policy(deadline: datetime | None) -> WaitPolicy:
    return WaitPolicy(WaitLimits(7 * 86400, 60), commands=False, run_deadline=deadline)


def test_scheduled_run_deadline_refuses_a_wait_that_outlasts_the_run() -> None:
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=1)
    policy = _run_policy(deadline)
    with pytest.raises(ProtocolError, match="scheduled run that is stopped at"):
        plan(step(max_wait_seconds=3 * 3600, not_before_seconds=30), policy)
    raw = step(max_wait_seconds=1800, not_before=(deadline + timedelta(minutes=5)).isoformat())
    raw.pop("not_before_seconds")
    with pytest.raises(ProtocolError, match="a wait may last at most"):
        plan(raw, policy)
    # not_before_seconds past the run, with a max wait that also passes the run (the earliest time is the refusal).
    with pytest.raises(ProtocolError, match="wait less, or decide without waiting"):
        plan(step(max_wait_seconds=7200, not_before_seconds=7000), policy)
    accepted = plan(step(max_wait_seconds=1800, not_before_seconds=30), policy)
    assert accepted.steps[0].wait.max_wait_seconds == 1800
    # No run deadline: the same long wait is only the system limit.
    assert plan(step(max_wait_seconds=3 * 3600, not_before_seconds=30), _run_policy(None)).steps[0].wait.max_wait_seconds == 3 * 3600


def test_extend_refuses_a_wait_past_the_scheduled_run_deadline() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
    view = replace(OPEN, wait_policy=_run_policy(deadline))
    data = {"reply": "more time", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 3600}}
    with pytest.raises(ProtocolError, match="scheduled run that is stopped at"):
        parse_wait(reply(data), WaitContext("w1", WaitState.TIMED_OUT), view)
    ok = {**data, "wait": {"action": "extend", "max_wait_seconds": 60}}
    assert parse_wait(reply(ok), WaitContext("w1", WaitState.TIMED_OUT), view).max_wait_seconds == 60
    # Without a run deadline the hour-long extend is within the policy limit.
    open_view = replace(OPEN, wait_policy=_run_policy(None))
    assert parse_wait(reply(data), WaitContext("w1", WaitState.TIMED_OUT), open_view).max_wait_seconds == 3600


# A fixed clock: the seconds left are an exact hour, so the boundary is not a second either way.
_RUN_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
_RUN_STOP = _RUN_NOW + timedelta(hours=1)


def _freeze_run_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.tiers import planner_protocol

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return _RUN_NOW if tz is None else _RUN_NOW.astimezone(tz)

    monkeypatch.setattr(planner_protocol, "datetime", Frozen)


def _absolute(moment: datetime, **extra: Any) -> dict[str, Any]:
    raw = step(not_before=moment.isoformat(), **extra)
    raw.pop("not_before_seconds")
    return raw


def test_run_deadline_bounds_max_wait_not_before_and_extend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run stop is a hard edge: the same second is allowed, one second past is refused."""
    _freeze_run_clock(monkeypatch)
    policy = _run_policy(_RUN_STOP)
    # max_wait_seconds and not_before_seconds may use the whole of what is left.
    exact = plan(step(max_wait_seconds=3600, not_before_seconds=3600), policy)
    assert exact.steps[0].wait.max_wait_seconds == 3600
    assert exact.steps[0].wait.not_before_seconds == 3600
    with pytest.raises(ProtocolError, match="at most 3600 s"):
        plan(step(max_wait_seconds=3601, not_before_seconds=30), policy)
    with pytest.raises(ProtocolError, match="stopped at 2026-10-19 13:00 UTC"):
        plan(step(max_wait_seconds=3601, not_before_seconds=3601), policy)
    # not_before may land on the stop and not one second after it, even when the max wait fits.
    assert plan(_absolute(_RUN_STOP, max_wait_seconds=3600), policy).steps[0].wait.not_before == _RUN_STOP
    with pytest.raises(ProtocolError, match="a wait may last at most 3600 s"):
        plan(_absolute(_RUN_STOP + timedelta(seconds=1), max_wait_seconds=1800), policy)
    view = replace(OPEN, wait_policy=policy)
    context = WaitContext("w1", WaitState.TIMED_OUT)
    extend = {"reply": "more time", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 3600}}
    assert parse_wait(reply(extend), context, view).max_wait_seconds == 3600
    over = {"reply": "more time", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 3601}}
    with pytest.raises(ProtocolError, match="at most 3600 s"):
        parse_wait(reply(over), context, view)


def test_run_deadline_of_zero_seconds_left_refuses_a_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that stops now, or already stopped, leaves nothing a wait can use."""
    _freeze_run_clock(monkeypatch)
    context = WaitContext("w1", WaitState.TIMED_OUT)
    extend = {"reply": "more time", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 1}}
    for stop in (_RUN_NOW, _RUN_NOW - timedelta(seconds=1)):
        policy = _run_policy(stop)
        with pytest.raises(ProtocolError, match="at most 0 s"):
            plan(step(max_wait_seconds=1, not_before_seconds=1), policy)
        with pytest.raises(ProtocolError, match="at most 0 s"):
            plan(_absolute(stop + timedelta(seconds=1), max_wait_seconds=1), policy)
        with pytest.raises(ProtocolError, match="at most 0 s"):
            parse_wait(reply(extend), context, replace(OPEN, wait_policy=policy))


def test_concurrent_wait_updates_cannot_store_an_invalid_pair(settings: dict[str, str],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_put(values: Any) -> None:
        # Yield so the two updates overlap between reading the limits and storing them.
        await asyncio.sleep(0)
        for key, value in values.items():
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value

    monkeypatch.setattr(waits.db_settings, "put_settings", slow_put)

    async def run() -> None:
        results = await asyncio.gather(
            waits.update({"max_wait_seconds": 3600}),
            waits.update({"min_check_seconds": 10000}),
            return_exceptions=True,
        )
        errors = [item for item in results if isinstance(item, ValueError)]
        assert len(errors) == 1 and len(results) - len(errors) == 1
        assert waits.limits().min_check_seconds <= waits.limits().max_wait_seconds
        stored_max = int(settings.get(waits.MAX_WAIT_KEY, WaitLimits().max_wait_seconds))
        stored_min = int(settings.get(waits.MIN_CHECK_KEY, WaitLimits().min_check_seconds))
        assert stored_min <= stored_max
    asyncio.run(run())


def test_invalid_stored_limits_combination_falls_back(settings: dict[str, str], caplog: Any) -> None:
    settings.update({waits.MAX_WAIT_KEY: "60", waits.MIN_CHECK_KEY: "300"})
    asyncio.run(waits.load())
    assert waits.limits() == WaitLimits()
    assert waits.MAX_WAIT_KEY in caplog.text and waits.MIN_CHECK_KEY in caplog.text


@pytest.mark.parametrize("raw", ["bad", "9", "86401"])
def test_invalid_stored_check_minimum_falls_back(settings: dict[str, str], caplog: Any, raw: str) -> None:
    settings[waits.MIN_CHECK_KEY] = raw
    asyncio.run(waits.load())
    assert waits.limits().min_check_seconds == WaitLimits().min_check_seconds
    assert waits.MIN_CHECK_KEY in caplog.text


def test_wait_cancel_and_replan_away_pending_steps_complete_the_decision() -> None:
    view = replace(OPEN, started={"s1": (), "w1": ()}, pending={"s2": ()})
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0)
    data = {"decision": "replan", "steps": [], "waits": [CANCEL]}
    with pytest.raises(ProtocolError, match="this decision completes the task"):
        parse_decision(reply(data), context, view)
    assert parse_decision(reply({**data, "final": FINAL}), context, view).final is not None


@pytest.mark.parametrize("wait_action", [
    {"action": "extend", "max_wait_seconds": 60}, {"action": "fail", "reason": "no"},
])
def test_wait_final_refused_for_other_actions(wait_action: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError, match="final is only"):
        parse_wait(reply({"reply": "ok", "action": "none", "wait": wait_action, "final": FINAL}),
                   WaitContext("w1", WaitState.TIMED_OUT, last_open=True), OPEN)


@pytest.mark.parametrize("original", ["gate", "wait"])
def test_pending_gate_and_wait_cannot_swap_kinds(original: str) -> None:
    gate = {"id": "w1", "kind": "gate", "topic": "choice", "depends_on": [], "agenda": [{"question": "which?"}]}
    view = replace(OPEN, started={}, pending={"w1": ()}, gate_policy=GatePolicy.ALLOWED,
                   gates={"w1": GateView(GateState.PENDING)} if original == "gate" else {},
                   waits={"w1": WaitView(WaitState.PENDING)} if original == "wait" else {})
    with pytest.raises(ProtocolError, match=f"step w1 is a {original}; use a new id"):
        parse_chat(reply({"reply": "ok", "action": "replan", "steps": [step() if original == "gate" else gate]}), view)
