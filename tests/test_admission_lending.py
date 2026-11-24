"""Slot transfers only last for one parent's blocking explorer wait."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission
from looper.agents.lanes import Lane

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


@pytest.fixture
def controller(monkeypatch):
    from looper.db import profile_registry, provider_limits

    monkeypatch.setattr(
        profile_registry,
        "get_provider",
        AsyncMock(
            return_value={
                "id": "account",
                "name": "Account",
                "max_concurrency": 1,
            }
        ),
    )
    monkeypatch.setattr(provider_limits, "windows", AsyncMock(return_value=[]))
    c = admission.Controller(now=lambda: NOW)
    c.active = True
    return c


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


def test_one_transfer_per_wait_returns_directly_then_next_wait(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        first = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "first"))
        second = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "second"))
        await settle()
        async with c.lend("worker", ["second", "first"]):
            await settle()
            assert first.done() and not second.done()
            assert set(c.running) == {"first"}
            assert c.running["first"].lent_by == "worker"
            await c.release("first")
            await settle()
            assert set(c.running) == {"worker"}
            assert not second.done()
            assert not c.waiters[0].ready.done()
        async with c.lend("worker", ["second"]):
            await settle()
            assert second.done()
            await c.release("second")
        await c.release("worker")
        await asyncio.gather(first, second)
        assert not c.waiters and not c.running and not c.lending

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_timeout_reacquires_original_order_before_later_arrivals(controller):
    async def scenario():
        c = controller
        await c.acquire("account", Lane.USER_TASK, "worker")
        original = c.running["worker"]
        child = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "child"))
        await settle()
        entered = asyncio.Event()
        finish_wait = asyncio.Event()

        async def wait():
            async with c.lend("worker", ["child"]):
                entered.set()
                await finish_wait.wait()

        waiting = asyncio.create_task(wait())
        await entered.wait()
        await child
        later = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "later"))
        await settle()
        finish_wait.set()
        await settle()
        assert not waiting.done()
        assert c.running["child"].lent_by is None
        parent_waiter = next(w for w in c.waiters if w.owner == "worker")
        assert parent_waiter.sequence == original.sequence
        assert parent_waiter.arrived == original.arrived
        await c.release("child")
        await waiting
        assert "worker" in c.running and not later.done()
        await c.release("worker")
        await later
        await c.release("later")

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("missing", [None, "absent"])
def test_missing_parent_never_lends(controller, missing):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        child = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "child"))
        await settle()
        async with c.lend(missing, ["child"]):
            assert set(c.running) == {"worker"}
            assert not c.lending and not child.done()
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_disabled_lend_and_hold_take_no_lock_or_storage(monkeypatch):
    async def scenario():
        c = admission.Controller()
        c.lock = None
        c._account = AsyncMock(side_effect=AssertionError("storage touched"))
        c.states["account"] = admission.Account(state=admission.AdmissionState.FROZEN)
        async with c.lend("worker", ["child"]):
            assert c.hold("account") is None
        c._account.assert_not_called()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_frozen_hold_uses_reset_without_safety_margin_and_no_lending(controller):
    async def scenario():
        c = controller
        reset = NOW + timedelta(hours=1)
        c.states["account"] = admission.Account(
            state=admission.AdmissionState.FROZEN,
            until=reset + timedelta(seconds=60),
            until_known=True,
        )
        await c.register("account", Lane.USER_TASK, "worker")
        child = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "child"))
        await settle()
        async with c.lend("worker", ["child"]):
            assert c.hold("account") == admission.Hold("quota", reset)
            assert not child.done() and not c.lending
        c.states["account"].until_known = False
        assert c.hold("account") == admission.Hold("quota", None)
        c.states["account"].state = admission.AdmissionState.PROBING
        assert c.hold("account") is None
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_lending_never_crosses_accounts_or_targets_running_children(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        await c.register("other", Lane.USER_TASK, "asking")
        other = asyncio.create_task(c.acquire("other", Lane.USER_TASK, "other-child"))
        await settle()
        async with c.lend("worker", ["asking", "other-child"]):
            assert set(c.running) == {"worker", "asking"}
            assert not other.done()
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)
        await c.release("worker")
        await c.release("asking")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_intent_before_child_queues_still_lends(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        async with c.lend("worker", ["child"]):
            child = await c.acquire("account", Lane.USER_TASK, "child")
            assert child.lent_by == "worker"
            await c.release("child")
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_disable_drops_intents_and_grants_waiters(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        async with c.lend("worker", ["child"]):
            child = await c.acquire("account", Lane.USER_TASK, "child")
            other = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "other"))
            await settle()
            await c.set_enabled(False)
            await other
            assert not c.lending
            assert child.lent_by is None
            assert c.hold("account") is None
        await c.release("child")
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_lending_failure_leaves_parent_holding_and_child_queued(
    controller, monkeypatch, caplog
):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        child = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "child"))
        await settle()
        monkeypatch.setattr(
            c,
            "_lend_waiters",
            lambda *a: (_ for _ in ()).throw(RuntimeError("lend failed")),
        )
        async with c.lend("worker", ["child"]):
            assert set(c.running) == {"worker"}
            assert not child.done()
            assert not c.lending
        assert any(
            r.levelname == "ERROR" and "lend" in r.message.lower()
            for r in caplog.records
        )
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_parent_cancellation_drops_intent_without_reacquiring(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        borrowed = asyncio.Event()

        async def wait():
            async with c.lend("worker", ["child"]):
                borrowed.set()
                await asyncio.Event().wait()

        waiting = asyncio.create_task(wait())
        await borrowed.wait()
        await c.acquire("account", Lane.USER_TASK, "child")
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert not c.lending and not c.waiters
        assert set(c.running) == {"child"}
        assert c.running["child"].lent_by is None
        await c.release("child")
        assert not c.running

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_releasing_parent_during_loan_does_not_resurrect_it(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        async with c.lend("worker", ["child"]):
            await c.acquire("account", Lane.USER_TASK, "child")
            await c.release("worker")
            await c.release("child")
            assert not c.running
        assert not c.waiters and not c.lending

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_disable_during_loan_preserves_parent_for_reenable(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        async with c.lend("worker", ["child"]):
            await c.acquire("account", Lane.USER_TASK, "child")
            await c.set_enabled(False)
            assert set(c.running) == {"worker", "child"}
            await c.set_enabled(True)
            later = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "later"))
            await settle()
            assert not later.done()
            await c.release("child")
            await settle()
            assert not later.done()
        await c.release("worker")
        await later
        await c.release("later")
        assert not c.running and not c.waiters

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_wait_body_exception_propagates_after_returning_slot(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        with pytest.raises(ValueError, match="caller failed"):
            async with c.lend("worker", ["child"]):
                await c.acquire("account", Lane.USER_TASK, "child")
                await c.release("child")
                raise ValueError("caller failed")
        assert set(c.running) == {"worker"}
        assert not c.lending
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_cancel_while_parent_reacquires_does_not_leak(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        borrowed = asyncio.Event()

        async def wait():
            async with c.lend("worker", ["child"]):
                await c.acquire("account", Lane.USER_TASK, "child")
                borrowed.set()

        waiting = asyncio.create_task(wait())
        await borrowed.wait()
        await settle()
        assert any(w.owner == "worker" for w in c.waiters)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert not c.waiters and not c.lending
        assert set(c.running) == {"child"}
        await c.release("child")
        assert not c.running

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_cancelled_child_returns_lent_slot_to_parent(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "worker")
        entered = asyncio.Event()

        async def child_run():
            async with c.slot("account", Lane.USER_TASK, owner="child"):
                entered.set()
                await asyncio.Event().wait()

        child = asyncio.create_task(child_run())
        await settle()
        async with c.lend("worker", ["child"]):
            await entered.wait()
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            assert set(c.running) == {"worker"}
        assert not c.lending
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("paused", ["drain", "background"])
def test_lending_respects_drain_and_background_pause(controller, monkeypatch, paused):
    async def scenario():
        c = controller
        lane = Lane.BACKGROUND if paused == "background" else Lane.USER_TASK
        await c.register("account", lane, "worker")
        if paused == "drain":
            c.draining = lambda: True
        else:
            monkeypatch.setattr(c, "_background_paused", AsyncMock(return_value=True))
        child = asyncio.create_task(c.acquire("account", lane, "child"))
        await settle()
        async with c.lend("worker", ["child"]):
            assert not child.done()
            assert set(c.running) == {"worker"}
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        await c.release("worker")

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_parent_slot_finishes_probe_grant_acquired_after_lending(
    controller, monkeypatch
):
    async def scenario():
        c = controller
        transition = AsyncMock()
        monkeypatch.setattr(c, "transition", transition)
        async with c.slot("account", Lane.USER_TASK, owner="worker") as original:
            async with c.lend("worker", ["child"]):
                await c.acquire("account", Lane.USER_TASK, "child")
                c.states["account"] = admission.Account(
                    state=admission.AdmissionState.PROBING
                )
            assert not original.probe
            assert c.running["worker"].probe
            await c.release("child")
        transition.assert_awaited_once_with(
            "account", admission.ProbeEnded(False, False)
        )
        assert not c.running

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("aged_minutes", [0, 10])
def test_lending_follows_effective_lane_then_arrival_order(controller, aged_minutes):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "planner")
        earlier = asyncio.create_task(c.acquire("account", Lane.BACKGROUND, "earlier"))
        later = asyncio.create_task(c.acquire("account", Lane.USER_TASK, "later"))
        await settle()
        c.waiters[0].arrived -= timedelta(minutes=aged_minutes)
        winner, loser = (earlier, later) if aged_minutes else (later, earlier)
        winner_owner = "earlier" if aged_minutes else "later"
        try:
            async with c.lend("planner", ["earlier", "later"]):
                await settle()
                try:
                    assert winner.done() and not loser.done()
                    assert set(c.running) == {winner_owner}
                finally:
                    for child_owner in ("earlier", "later"):
                        await c.release(child_owner)
                assert set(c.running) == {"planner"}
        finally:
            for job in (earlier, later):
                job.cancel()
            await asyncio.gather(earlier, later, return_exceptions=True)
            await c.release("earlier")
            await c.release("later")
            await c.release("planner")
        assert not c.running and not c.waiters and not c.lending

    asyncio.run(asyncio.wait_for(scenario(), 5))
