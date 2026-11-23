"""Receipts as pure code (assistant phase 3 RFC, section 6.4): kinds, wording, reasons, merging, and the wrapper that
adds a line to a turn. The database side is ``test_receipts_db``."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from looper.agents import receipts, receipts_settings
from looper.agents.ops import ErrorCode, OpError, Turn
from looper.agents.ops.categories import TOOL_ACTIONS, ActionCategory, ActionKind
from looper.agents.receipts import Receipt, ReceiptOutcome, Watch, kind_of, merge, receipt_for, render

SCHEDULE_DISABLE = TOOL_ACTIONS["looper_schedules"]["disable"]
PROJECT_UPDATE = TOOL_ACTIONS["looper_projects"]["update"]


def _every_kind() -> list[tuple[str, str | None, ActionKind]]:
    found = []
    for tool, entry in TOOL_ACTIONS.items():
        if isinstance(entry, ActionKind):
            found.append((tool, None, entry))
        else:
            found.extend((tool, action, kind) for action, kind in entry.items())
    return found


EVERY = _every_kind()


def _call(action: str | None, kind: ActionKind) -> tuple[dict, dict]:
    arguments: dict = {} if action is None else {"action": action}
    result: dict = {}
    if kind.subject is not None:
        arguments[kind.subject] = "demo"
        result[kind.subject] = {"name": "demo", "id": "demo-1"}
    if kind.category is ActionCategory.UPDATE:
        arguments["description"] = "x"
    return arguments, result


@pytest.mark.parametrize("tool,action,kind", EVERY, ids=[f"{t}:{a}" for t, a, _ in EVERY])
def test_every_write_gets_a_success_line_in_its_category_template(tool, action, kind) -> None:
    arguments, result = _call(action, kind)
    assert kind_of(tool, arguments) is kind
    line = render(receipt_for(kind, arguments, result))
    noun, verb = kind.noun, kind.verb
    expected = {
        ActionCategory.CREATE: f"✅ 已{verb}{noun}「demo」（demo-1）",
        ActionCategory.UPDATE: f"✅ 已{verb}{noun}「demo」：description",
        ActionCategory.CONTROL: f"✅ {noun}「demo」已{verb}",
        ActionCategory.REMOVE: f"✅ 已{verb}{noun}「demo」",
        ActionCategory.RELAY: f"✅ 已把你的话转给{noun}「demo」",
    }[kind.category]
    if kind.subject is None:
        expected = expected.replace("「demo」", "").replace("（demo-1）", "")
    assert line == expected


@pytest.mark.parametrize("tool,action,kind", EVERY, ids=[f"{t}:{a}" for t, a, _ in EVERY])
def test_every_write_gets_a_failure_line_with_the_reason_of_its_code(tool, action, kind) -> None:
    arguments, _ = _call(action, kind)
    receipt = receipt_for(kind, arguments, OpError(ErrorCode.NOT_FOUND, "no schedule named 'secret-token'"))
    name = "「demo」" if kind.subject else ""
    assert render(receipt) == f"❌ 没做成：{kind.verb}{kind.noun}{name}（没找到）"
    assert "secret-token" not in render(receipt)  # the message never reaches the line


def test_the_examples_of_the_rfc_read_as_written() -> None:
    install = TOOL_ACTIONS["looper_catalog"]["install"]
    assert render(receipt_for(install, {"action": "install"}, {"install": {"id": "mcp-github"}, "item": {"name": "github"}})) \
        == "✅ 已安装能力「github」（mcp-github）"
    update = receipt_for(PROJECT_UPDATE, {"action": "update", "project": "web", "profile": "p", "concurrency": 2},
                         {"project": {"name": "web"}})
    assert render(update) == "✅ 已修改项目「web」：profile、concurrency"
    assert render(receipt_for(SCHEDULE_DISABLE, {"action": "disable", "schedule": "日报"}, {})) == "✅ 定时任务「日报」已停用"
    delete = TOOL_ACTIONS["looper_projects"]["delete"]
    assert render(receipt_for(delete, {"action": "delete", "project": "demo"}, {})) == "✅ 已删除项目「demo」"
    relay = TOOL_ACTIONS["looper_task_instruct"]
    assert render(receipt_for(relay, {"task_id": "t-1"}, {"task_id": "t-1", "name": "修登录"})) \
        == "✅ 已把你的话转给任务「修登录」"
    assert render(receipt_for(TOOL_ACTIONS["looper_schedules"]["delete"], {"action": "delete", "schedule": "周报"},
                              OpError(ErrorCode.NOT_FOUND, "x"))) == "❌ 没做成：删除定时任务「周报」（没找到）"


@pytest.mark.parametrize("code,why", [
    (ErrorCode.NOT_FOUND, "没找到"), (ErrorCode.FORBIDDEN, "不允许"), (ErrorCode.CONFLICT, "状态不对"),
    (ErrorCode.INVALID, "参数不对"), (ErrorCode.EXPIRED, "确认已过期"), (ErrorCode.UNAVAILABLE, "暂时做不了"),
    (ErrorCode.RATE_LIMITED, "暂时做不了"), (ErrorCode.INTERNAL, "暂时做不了")])
def test_a_failure_reason_comes_from_the_error_code(code, why) -> None:
    receipt = receipt_for(SCHEDULE_DISABLE, {"schedule": "日报"}, OpError(code, "whatever the message says"))
    assert render(receipt) == f"❌ 没做成：停用定时任务「日报」（{why}）"
    assert receipt.error_code is code and receipt.outcome is ReceiptOutcome.FAILED


@pytest.mark.parametrize("code", [ErrorCode.STOPPED, ErrorCode.NOT_THIS_TURN, ErrorCode.NEEDS_USER_TURN,
                                  ErrorCode.UNAUTHORIZED, ErrorCode.BLOCKED])
def test_a_call_that_never_ran_or_the_model_already_tells_has_no_receipt(code) -> None:
    assert receipt_for(SCHEDULE_DISABLE, {"schedule": "日报"}, OpError(code, "x")) is None


def test_a_read_or_an_unknown_tool_has_no_kind() -> None:
    assert kind_of("looper_projects", {"action": "list"}) is None
    assert kind_of("looper_tasks", {}) is None
    assert kind_of("looper_projects", {}) is None
    assert receipt_for(None, {}, {}) is None


def test_update_fields_are_names_only_and_the_name_is_one_short_line() -> None:
    receipt = receipt_for(PROJECT_UPDATE, {"action": "update", "project": "web\n\nignore previous " + "x" * 200,
                                           "background": "sk-live-secret-value"}, {})
    line = render(receipt)
    assert "sk-live-secret-value" not in line and "\n" not in line
    assert line.endswith("：background") and len(line) < receipts.LINE_CHARS
    assert receipt.subject_name.endswith("…") and len(receipt.subject_name) <= 60


def test_an_object_argument_is_listed_by_the_names_inside_it_never_its_values() -> None:
    update = TOOL_ACTIONS["looper_schedules"]["update"]
    receipt = receipt_for(update, {"action": "update", "schedule": "日报", "fields": {"cron": "0 9 * * *", "env": {"K": "v"}}}, {})
    assert render(receipt) == "✅ 已修改定时任务「日报」：cron、env"
    assert render(receipt_for(update, {"action": "update", "schedule": "日报", "fields": {}}, {})) \
        == "✅ 已修改定时任务「日报」：fields"


def test_a_line_is_never_longer_than_the_column_allows() -> None:
    kind = TOOL_ACTIONS["looper_settings"]["scheduler"]
    receipt = Receipt(kind, ReceiptOutcome.DONE, None, None, tuple(f"field_{i}" * 4 for i in range(80)))
    assert len(render(receipt)) == receipts.LINE_CHARS


def _done(name: str, kind: ActionKind = SCHEDULE_DISABLE) -> Receipt:
    return Receipt(kind, ReceiptOutcome.DONE, name, None)


def _failed(name: str, kind: ActionKind = SCHEDULE_DISABLE) -> Receipt:
    return Receipt(kind, ReceiptOutcome.FAILED, name, None, error_code=ErrorCode.NOT_FOUND)


def test_merge_drops_a_failure_a_later_success_of_the_same_action_and_subject_answers() -> None:
    assert merge([_failed("日报"), _done("日报")]) == [_done("日报")]
    assert merge([_done("日报"), _failed("日报")]) == [_done("日报"), _failed("日报")]  # only a later success counts
    assert merge([_failed("日报"), _done("周报")]) == [_failed("日报"), _done("周报")]
    enable = TOOL_ACTIONS["looper_schedules"]["enable"]
    assert merge([_failed("日报"), _done("日报", enable)]) == [_failed("日报"), _done("日报", enable)]


def test_merge_matches_a_subject_by_id_or_by_name() -> None:
    named = _failed("日报")
    with_id = replace(_done("日报"), subject_id="sched-1")
    assert merge([named, with_id]) == [with_id]  # the call named it; its result also gave the id
    other = replace(_done("日报"), subject_id="sched-2")
    failed = replace(_failed("日报"), subject_id="sched-1")
    assert merge([failed, other]) == [failed, other]  # different ids are different subjects


def test_merge_keeps_identical_successes_once_and_different_ones_apart() -> None:
    assert merge([_done("日报"), _done("日报"), _done("周报")]) == [_done("日报"), _done("周报")]
    assert merge([_failed("日报"), _failed("日报")]) == [_failed("日报"), _failed("日报")]


def test_add_to_turn_replaces_a_failed_line_by_the_success_and_shows_a_duplicate_once() -> None:
    turn = Turn(channel="web", chat="c", message_id="m", blocks=["需要你确认：别的事"])
    receipts.add_to_turn(turn, _failed("日报"))
    assert turn.blocks == ["需要你确认：别的事", "❌ 没做成：停用定时任务「日报」（没找到）"]
    receipts.add_to_turn(turn, _done("日报"))
    assert turn.blocks == ["需要你确认：别的事", "✅ 定时任务「日报」已停用"]
    receipts.add_to_turn(turn, _done("日报"))
    assert turn.blocks == ["需要你确认：别的事", "✅ 定时任务「日报」已停用"] and len(turn.receipts) == 1


class _Tool:
    def __init__(self, name: str, outcome, block: str | None = None) -> None:
        self.name, self._outcome, self._block = name, outcome, block

    async def run(self, call):
        if self._block is not None:
            call.turn.blocks.append(self._block)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _watched(tool: _Tool, arguments: dict, turn: Turn | None = None) -> tuple[Turn, Watch, object]:
    turn = turn or Turn(channel="web", chat="c", message_id="m")
    watch = Watch()
    try:
        answer = asyncio.run(receipts.run_watched(tool, SimpleNamespace(turn=turn, arguments=arguments), watch))
    except Exception as exc:
        answer = exc
    return turn, watch, answer


def test_a_watched_success_adds_its_line_and_tells_the_model() -> None:
    result = {"schedule": {"name": "日报"}}
    turn, watch, answer = _watched(_Tool("looper_schedules", result), {"action": "disable", "schedule": "日报"})
    assert answer is result  # the call's own result, untouched
    assert turn.blocks == ["✅ 定时任务「日报」已停用"]
    assert watch.shown == receipts.SHOWN_DONE == "Looper adds a receipt line for this after your reply; do not repeat it."
    assert receipts.row_fields(watch) == {"category": "control", "receipt": "✅ 定时任务「日报」已停用"}


def test_a_watched_failure_adds_its_line_and_the_error_still_propagates() -> None:
    error = OpError(ErrorCode.NOT_FOUND, "no schedule")
    turn, watch, answer = _watched(_Tool("looper_schedules", error), {"action": "delete", "schedule": "周报"})
    assert answer is error
    assert turn.blocks == ["❌ 没做成：删除定时任务「周报」（没找到）"]
    assert watch.shown == receipts.SHOWN_FAILED == (
        "Looper tells the user after your reply that this did not run; do not say it did.")
    assert receipts.row_fields(watch)["category"] == "remove"


def test_an_unexpected_exception_counts_as_unavailable() -> None:
    boom = RuntimeError("database is down")
    turn, watch, answer = _watched(_Tool("looper_schedules", boom), {"action": "run", "schedule": "日报"})
    assert answer is boom
    assert turn.blocks == ["❌ 没做成：立即运行定时任务「日报」（暂时做不了）"]
    assert "database" not in turn.blocks[0]


def test_a_tool_that_wrote_its_own_line_keeps_it_and_says_nothing_to_the_model() -> None:
    own = "已记下提醒 #3：明天 9:00"
    turn, watch, _ = _watched(_Tool("looper_reminder_create", {"reminder": {"id": 3, "text": "喝水"}}, own), {"text": "喝水"})
    assert turn.blocks == [own] and turn.receipts == []
    assert watch.shown is None
    assert receipts.row_fields(watch) == {"category": "create", "receipt": "✅ 已新建提醒「喝水」（3）"}


def test_a_read_leaves_everything_as_it_was() -> None:
    result = {"projects": []}
    turn, watch, answer = _watched(_Tool("looper_projects", result), {"action": "list"})
    assert answer is result and turn.blocks == [] and watch == Watch()
    assert receipts.row_fields(watch) == {}


def test_a_receipt_that_cannot_be_built_only_logs(monkeypatch, caplog) -> None:
    def broken(*_args):
        raise ValueError("no way")

    monkeypatch.setattr(receipts, "receipt_for", broken)
    result = {"schedule": {"name": "日报"}}
    with caplog.at_level(logging.WARNING, logger="looper.agents.receipts"):
        turn, watch, answer = _watched(_Tool("looper_schedules", result), {"action": "disable", "schedule": "日报"})
    assert answer is result and turn.blocks == [] and watch == Watch()
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_the_switch_is_off_until_the_stored_value_is_true() -> None:
    receipts_settings.set_enabled(False)
    try:
        for stored, expected in ((None, False), ("false", False), ("TRUE", False), ("1", False), ("true", True)):
            receipts_settings.apply_stored(stored)
            assert receipts_settings.enabled() is expected
    finally:
        receipts_settings.set_enabled(False)


def test_receipt_action_id_can_be_filled_in_after_the_row_is_written() -> None:
    receipt = replace(_done("日报"), action_id=7)
    assert receipt.action_id == 7 and render(receipt) == render(_done("日报"))
