"""The turn binding of tool calls in its two modes (chat-stop-v2 §10).

Stop off (the default): the binding only observes. A call runs in the turn running when it comes, as it did before
turns were bound; each disagreement of its binding is one structured warning and one count in ``binding_report``.
Stop on: the binding is enforced, and the same calls are refused; they are logged and counted all the same.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings
from looper.agents.ops import Call, Risk, Tool, Turn
from looper.agents.ops import service
from looper.agents.ops.service import ManagerOps, Platform


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)
    monkeypatch.setattr(service.db_actions, "record", AsyncMock())


def _ops() -> tuple[ManagerOps, list[dict[str, Any]]]:
    ran: list[dict[str, Any]] = []

    async def write(call: Call) -> dict[str, Any]:
        ran.append(dict(call.arguments))
        return {"ok": True}

    async def read(call: Call) -> dict[str, Any]:
        return {"ok": True}

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     tools=(Tool("t_write", "writes", schema, Risk.WRITE, write),
                            Tool("t_read", "reads", schema, Risk.READ, read)))
    return ops, ran


def _turn(message_id: str, *, resident: bool = True) -> Turn:
    return Turn("web", "chat", message_id=message_id, require_match=resident, runtime="claude-code")


def _status(answer: dict[str, Any]) -> str:
    return answer["status"] if answer["status"] != "error" else answer["error"]["code"]


def _mode(monkeypatch: pytest.MonkeyPatch, enforced: bool) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", enforced)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [record.binding_mismatch for record in caplog.records if hasattr(record, "binding_mismatch")]


async def _settled(ops: ManagerOps) -> None:
    """The observe-only lookups started beside the calls are done."""
    while ops._observing:
        await asyncio.gather(*ops._observing)


@pytest.mark.parametrize("enforced", [False, True])
def test_a_resident_write_with_no_matching_tool_use(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, enforced: bool) -> None:
    _mode(monkeypatch, enforced)
    caplog.set_level(logging.WARNING, logger=service.logger.name)

    async def run() -> tuple[str, float, dict[str, Any]]:
        ops, ran = _ops()
        ops.begin_turn(_turn("m1"), binding="b1")
        loop = asyncio.get_running_loop()
        began = loop.time()
        answer = await ops.call("t_write", {"n": 1}, binding="b1")
        took = loop.time() - began
        await _settled(ops)
        return _status(answer), took, {"ran": ran, **ops.binding_report()}

    status, took, report = asyncio.run(run())
    by = [{"reason": "not_this_turn", "runtime": "claude-code", "tool": "t_write", "count": 1}]
    assert report["enforced"] is enforced and report["mismatches"] == 1 and report["by"] == by
    assert _warnings(caplog) == [{"reason": "not_this_turn", "runtime": "claude-code", "tool": "t_write",
                                  "enforced": enforced}]
    if enforced:
        assert status == "not_this_turn" and report["ran"] == []
    else:
        assert status == "ok" and report["ran"] == [{"n": 1}]
        assert took < service.MATCH_WAIT_SEC  # observing never holds the call up


@pytest.mark.parametrize("enforced", [False, True])
def test_a_matching_write_is_no_mismatch(monkeypatch: pytest.MonkeyPatch, enforced: bool) -> None:
    _mode(monkeypatch, enforced)

    async def run() -> tuple[str, dict[str, Any]]:
        ops, _ = _ops()
        ops.begin_turn(_turn("m1"), binding="b1")
        ops.expect("b1", "t_write", {"n": 1}, call_id="tu1")
        answer = await ops.call("t_write", {"n": 1}, binding="b1", call_id="tu1")
        read = await ops.call("t_read", {}, binding="b1")
        await _settled(ops)
        assert _status(read) == "ok"
        return _status(answer), ops.binding_report()

    status, report = asyncio.run(run())
    assert status == "ok" and report["mismatches"] == 0 and report["by"] == []


@pytest.mark.parametrize("enforced", [False, True])
def test_a_user_turns_write_without_a_binding(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                              enforced: bool) -> None:
    # A bridge that sends no X-Looper-Turn (the Pi bridge before it did).
    _mode(monkeypatch, enforced)
    caplog.set_level(logging.WARNING, logger=service.logger.name)

    async def run() -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        ops, ran = _ops()
        ops.begin_turn(_turn("m1", resident=False), binding="b1")
        answer = await ops.call("t_write", {"n": 1})
        return _status(answer), ran, ops.binding_report()

    status, ran, report = asyncio.run(run())
    if enforced:
        assert status == "needs_user_turn" and ran == []
    else:
        assert status == "ok" and ran == [{"n": 1}]
    assert report["by"] == [{"reason": "no_binding", "runtime": "claude-code", "tool": "t_write", "count": 1}]
    assert [w["reason"] for w in _warnings(caplog)] == ["no_binding"]


@pytest.mark.parametrize("enforced", [False, True])
def test_a_late_call_of_an_ended_turn_while_the_next_runs(monkeypatch: pytest.MonkeyPatch, enforced: bool) -> None:
    _mode(monkeypatch, enforced)

    async def run() -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        ops, ran = _ops()
        first = _turn("m1", resident=False)
        ops.begin_turn(first, binding="b1")
        ops.end_turn(first)
        ops.begin_turn(_turn("m2", resident=False), binding="b2")
        answer = await ops.call("t_write", {"n": 1}, binding="b1")
        return _status(answer), ran, ops.binding_report()

    status, ran, report = asyncio.run(run())
    if enforced:
        assert status == "expired" and ran == []
    else:
        assert status == "ok" and ran == [{"n": 1}]  # as before turns were bound: the running turn's
    assert [(m["reason"], m["count"]) for m in report["by"]] == [("other_turn", 1)]


@pytest.mark.parametrize("enforced", [False, True])
def test_refusals_that_came_before_the_binding_stay_in_both_modes(monkeypatch: pytest.MonkeyPatch,
                                                                   enforced: bool) -> None:
    _mode(monkeypatch, enforced)

    async def run() -> list[str]:
        ops, ran = _ops()
        answers = [await ops.call("t_write", {"n": 1})]  # no turn runs: Claude's own, which only reads
        stopped = _turn("m1", resident=False)
        ops.begin_turn(stopped, binding="b1")
        ops.stop_turn(stopped)
        answers.append(await ops.call("t_write", {"n": 2}, binding="b1"))  # the stopped turn's
        ops.end_turn(stopped)
        answers.append(await ops.call("t_write", {"n": 3}, binding="b1"))  # it ended, and no turn runs
        assert ran == []
        return [_status(a) for a in answers]

    expected = ["needs_user_turn", "stopped", "stopped" if enforced else "needs_user_turn"]
    assert asyncio.run(run()) == expected


def test_observe_only_never_refuses_what_main_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every disagreement at once, stop off: each call runs exactly as without the binding, and each is counted."""
    _mode(monkeypatch, False)

    async def run() -> tuple[list[str], dict[str, Any]]:
        ops, ran = _ops()
        ops.begin_turn(_turn("m1"), binding="b1")
        answers = [await ops.call("t_write", {"n": 1}, binding="b1"),       # no tool_use streamed
                   await ops.call("t_write", {"n": 2}),                     # no binding
                   await ops.call("t_write", {"n": 3}, binding="nobody"),   # a binding no turn has
                   await ops.call("t_read", {}, binding="nobody")]
        await _settled(ops)
        assert ran == [{"n": 1}, {"n": 2}, {"n": 3}]
        return [_status(a) for a in answers], ops.binding_report()

    statuses, report = asyncio.run(run())
    assert statuses == ["ok"] * 4
    assert {(m["reason"], m["tool"]): m["count"] for m in report["by"]} == {
        ("not_this_turn", "t_write"): 1, ("no_binding", "t_write"): 1,
        ("unknown_binding", "t_write"): 1, ("unknown_binding", "t_read"): 1}
    assert report["mismatches"] == 4 and report["enforced"] is False
