"""Frozen-turn callbacks and record-bound lending preserve account admission."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import admission, claude_cli, cost_guard
from looper.agents.lanes import Lane
from looper.db import llm_records, profile_registry, provider_admission, provider_limits

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(profile_registry, "get_provider", AsyncMock(return_value={
        "id": "account", "name": "Synthetic account", "max_concurrency": 1,
    }))
    monkeypatch.setattr(provider_limits, "windows", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider_admission, "save", AsyncMock(return_value=1))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    value = admission.Controller(now=lambda: NOW)
    value.active = True
    monkeypatch.setattr(admission, "_controller", value)
    return value


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.mark.parametrize("known", [False, True])
def test_frozen_notice_once_for_each_waiter_even_without_activity(controller, known):
    async def scenario():
        reset = NOW + timedelta(hours=1) if known else None
        provider_limits.windows.return_value = [
            {"window_name": "seven_day", "status": "rejected"},
            {"window_name": "five_hour", "status": "rejected"},
            {"window_name": "model", "status": "allowed"},
        ]
        await controller.transition("account", admission.QuotaFailure(reset, None))
        notices = [AsyncMock(), AsyncMock()]

        async def run(index):
            async with controller.slot("account", Lane.INTERACTIVE,
                                       owner=f"message-{index}", on_frozen=notices[index]):
                return index

        tasks = [asyncio.create_task(run(index)) for index in range(2)]
        try:
            await settle()
            for _ in range(2):
                await controller.wake()
                await settle()
            assert all(not task.done() for task in tasks)
            for notice in notices:
                notice.assert_awaited_once_with(admission.Frozen(
                    "Synthetic account", ("seven_day", "five_hour"), reset))
            await controller.transition("account", admission.OperatorClear())
            assert await asyncio.wait_for(asyncio.gather(*tasks), 1) == [0, 1]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await controller.stop()

    asyncio.run(scenario())


def test_frozen_callback_error_logs_once_and_does_not_fail_wait(controller, caplog):
    async def scenario():
        await controller.transition("account", admission.QuotaFailure(None, None))
        notice = AsyncMock(side_effect=RuntimeError("synthetic channel unavailable"))

        async def run():
            async with controller.slot("account", Lane.INTERACTIVE, owner="message",
                                       on_frozen=notice):
                return "answered"

        task = asyncio.create_task(run())
        try:
            await settle()
            await controller.wake()
            await settle()
            assert not task.done()
            notice.assert_awaited_once()
            assert "Could not send frozen account notice" in caplog.text
            await controller.transition("account", admission.OperatorClear())
            assert await asyncio.wait_for(task, 1) == "answered"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner,lane,child", [
    ("manager:routed-session", Lane.INTERACTIVE, "outbound-check"),
    ("planner-task", Lane.USER_TASK, "advisor-consult"),
])
def test_record_binding_lends_to_tool_child_without_context_or_database(
    controller, monkeypatch, owner, lane, child,
):
    async def scenario():
        read = AsyncMock(side_effect=AssertionError("bound record queried storage"))
        monkeypatch.setattr(llm_records, "get_record", read)
        async with controller.slot("account", lane, owner=owner):
            admission.bind_record(41)
            # An HTTP tool request has no parent's ContextVars. The record id is its link.
            context_token = admission._current_owner.set(None)
            try:
                parent, inherited_lane = await admission.parent_for(41)
                assert (parent, inherited_lane) == (owner, lane)
                async with controller.slot("account", inherited_lane, owner=child,
                                           parent=parent) as grant:
                    assert grant.borrowed
                    assert (await controller.snapshot("account"))["running"] == 2
                    assert not controller.waiters
            finally:
                admission._current_owner.reset(context_token)
            assert controller.record_owners == {41: owner}
        assert controller.record_owners == {}
        read.assert_not_awaited()

    asyncio.run(scenario())


def test_binding_precedes_unrelated_current_owner(controller, monkeypatch):
    async def scenario():
        await controller.register("account", Lane.INTERACTIVE, "manager:chat")
        await controller.register("other", Lane.BACKGROUND, "unrelated")
        marker = admission._current_owner.set("manager:chat")
        try:
            admission.bind_record(17)
            admission._current_owner.set("unrelated")
            assert await admission.parent_for(17) == ("manager:chat", Lane.INTERACTIVE)
        finally:
            admission._current_owner.reset(marker)
            await controller.release("manager:chat")
            await controller.release("unrelated")

    asyncio.run(scenario())


def test_record_binding_disabled_or_unowned_is_noop(controller, monkeypatch):
    controller.active = False
    marker = admission._current_owner.set("manager:chat")
    try:
        admission.bind_record(9)
    finally:
        admission._current_owner.reset(marker)
    controller.active = True
    admission.bind_record(10)
    admission.bind_record(None)
    assert controller.record_owners == {}


def test_disabling_admission_during_turn_still_drops_record_binding(controller):
    async def scenario():
        async with controller.slot("account", Lane.INTERACTIVE, owner="manager:chat"):
            admission.bind_record(31)
            controller.active = False
        assert controller.record_owners == {}

    asyncio.run(scenario())


def test_recorder_binds_new_record_before_tool_requests(controller, monkeypatch):
    async def scenario():
        monkeypatch.setattr(cost_guard, "before_run", AsyncMock(return_value=None))
        monkeypatch.setattr(llm_records, "start_record", AsyncMock(return_value=52))
        recorder = claude_cli.ClaudeRunRecorder(
            agent_id="manager", task_id=None, command=["synthetic-runtime"],
            cwd="/workspace", input_text="hello")
        monkeypatch.setattr(recorder, "_track", Mock())
        async with controller.slot("account", Lane.INTERACTIVE, owner="manager:chat"):
            await recorder.start()
            assert controller.record_owners == {52: "manager:chat"}
        assert controller.record_owners == {}

    asyncio.run(scenario())


def test_cancellable_slot_entered_in_another_task_can_exit(controller):
    async def scenario():
        async with AsyncExitStack() as stack:
            await asyncio.create_task(stack.enter_async_context(controller.slot(
                "account", Lane.INTERACTIVE, owner="manager:chat")))
            assert "manager:chat" in controller.running
            assert admission._current_owner.get() is None
        assert not controller.running

    asyncio.run(scenario())
