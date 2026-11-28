"""The task Manager sandbox alert goes to the operator's direct chat every time, with no value in it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from looper.agents import chat_scope_alerts, operator_identity, task_manager_alerts


@pytest.fixture(autouse=True)
def _reset():
    chat_scope_alerts.reset()
    yield
    chat_scope_alerts.reset()


def _target(monkeypatch, target):
    async def operator_dm_target():
        return target

    monkeypatch.setattr(operator_identity, "operator_dm_target", operator_dm_target)


def test_every_violation_is_sent_to_the_operator_chat(monkeypatch) -> None:
    sent = []

    async def sender(channel_id, chat, text, **kwargs):
        sent.append((channel_id, chat, text))
        return True, None

    chat_scope_alerts.configure(sender)
    _target(monkeypatch, SimpleNamespace(channel_id="synthetic-channel", chat="synthetic-chat"))

    async def run():
        for _ in range(2):
            await task_manager_alerts.invariant_violation("task-one", "launch_setting_mismatch", "F3")

    asyncio.run(run())
    assert [(c, chat) for c, chat, _ in sent] == [("synthetic-channel", "synthetic-chat")] * 2
    assert "task-one" in sent[0][2] and "launch_setting_mismatch" in sent[0][2] and "F3" in sent[0][2]


def test_without_a_target_or_sender_nothing_is_sent_and_nothing_raises(monkeypatch) -> None:
    _target(monkeypatch, None)
    asyncio.run(task_manager_alerts.invariant_violation("task-one", "mcp_set_mismatch", "set"))

    async def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(operator_identity, "operator_dm_target", broken)
    asyncio.run(task_manager_alerts.invariant_violation("task-one", "readback_unavailable", "mcp_status"))


def test_a_sender_that_fails_does_not_raise(monkeypatch) -> None:
    async def sender(*args, **kwargs):
        raise RuntimeError("down")

    chat_scope_alerts.configure(sender)
    _target(monkeypatch, SimpleNamespace(channel_id="c", chat="d"))
    asyncio.run(task_manager_alerts.invariant_violation("task-one", "mcp_set_mismatch", "set"))
