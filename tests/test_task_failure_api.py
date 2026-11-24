"""Failure controls keep identity, confirmation and pagination at the public boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.ops import Call, ErrorCode, OpError, Turn


def test_failure_operations_are_closed_operator_only_and_versioned():
    from looper.agents.v1_domains import task_failures

    operations = {op.operation_id: op for op in task_failures.OPERATIONS}
    assert set(operations) == {"tasks.failure_history", "tasks.holds", "tasks.hold_release"}
    for op in operations.values():
        assert op.operator_only and op.level.value == "inside"
        assert op.request["additionalProperties"] is False
    release = operations["tasks.hold_release"]
    assert release.confirms and release.risk == "write"
    assert set(release.request["required"]) == {"task_id", "step_id", "failure_id", "action"}


def test_history_keeps_keyset_cursor_and_database_errors(monkeypatch):
    asyncio.run(_history(monkeypatch))


async def _history(monkeypatch):
    from looper.agents.v1_domains import task_failures

    monkeypatch.setattr(task_failures.tasks, "get_task", AsyncMock(return_value={"id": "task"}))
    rows = [{"id": number} for number in range(1, 4)]
    monkeypatch.setattr(task_failures.store, "list_failures", AsyncMock(return_value=rows))
    op = next(op for op in task_failures.OPERATIONS if op.operation_id == "tasks.failure_history")
    result = await task_failures.run(None, op, {"task_id": "task", "limit": 2}, None, {})
    assert result == {"items": rows[:2], "next_after_id": 2}
    task_failures.store.list_failures.assert_awaited_once_with("task", step_id=None, after_id=0, limit=3)
    monkeypatch.setattr(task_failures.store, "list_failures", AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(OpError) as error:
        await task_failures.run(None, op, {"task_id": "task"}, None, {})
    assert error.value.code is ErrorCode.UNAVAILABLE


def test_hold_tool_rechecks_mode_and_operator_identity(monkeypatch):
    asyncio.run(_hold_tool(monkeypatch))


async def _hold_tool(monkeypatch):
    from looper.agents.ops import task_hold
    from looper.agents import failures

    call = Call(arguments={"action": "retry", "task_id": "task", "step_id": "step", "failure_id": 4},
                turn=Turn(channel="web", chat="console", message_id="m"), platform=SimpleNamespace())
    monkeypatch.setattr(task_hold, "typing_mode", AsyncMock(return_value=failures.FailureTyping.OFF))
    with pytest.raises(OpError) as error:
        await task_hold.TOOL.run(call)
    assert error.value.code is ErrorCode.CONFLICT
    monkeypatch.setattr(task_hold, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
    monkeypatch.setattr(task_hold, "operator_call", AsyncMock(return_value=False))
    with pytest.raises(OpError) as error:
        await task_hold.TOOL.run(call)
    assert error.value.code is ErrorCode.FORBIDDEN


def test_only_operator_confirmed_restart_passes_a_reset_identity(monkeypatch):
    from looper.agents import failures
    from looper.agents.ops import tasks as task_tools, service

    async def body():
        monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
        monkeypatch.setattr(service, "operator_call", AsyncMock(return_value=True))
        monkeypatch.setattr(task_tools, "_restartable", AsyncMock(return_value={"id": "task", "name": "Test", "status": "stopped"}))
        controlled = AsyncMock()
        monkeypatch.setattr(task_tools, "control_task", controlled)
        call = Call(arguments={"action": "restart", "task_id": "task"},
            turn=Turn(channel="web", chat="console", message_id="m"), platform=SimpleNamespace(bus=object()))
        assert "清零" in await task_tools._confirming(call)
        await task_tools._control(call)
        assert controlled.call_args.kwargs["operator_actor"] == "console operator"
        monkeypatch.setattr(service, "operator_call", AsyncMock(return_value=False))
        other = Call(arguments=call.arguments, turn=Turn(channel="api", chat="client", message_id="m", client_id="client"), platform=call.platform)
        assert await task_tools._confirming(other) is None
        await task_tools._control(other)
        assert "operator_actor" not in controlled.call_args.kwargs
    asyncio.run(body())
