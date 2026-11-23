"""Account admission's observable ordering, freeze and disabled contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, attempts
from looper.agents.agent_settings import AgentType
from looper.agents.lanes import Lane

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


@pytest.fixture
def controller(monkeypatch):
    from looper.db import profile_registry, provider_limits, provider_admission

    monkeypatch.setattr(
        profile_registry,
        "get_provider",
        AsyncMock(
            return_value={
                "id": "account",
                "name": "Account",
                "max_concurrency": 1,
                "vendor_type": "anthropic",
            }
        ),
    )
    monkeypatch.setattr(provider_limits, "windows", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider_admission, "read_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider_admission, "save", AsyncMock(return_value=1))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    c = admission.Controller(now=lambda: NOW)
    c.active = True
    return c


async def tick():
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    "role,schedule_id,trigger,same,recovered,run_now,parent,expected",
    [
        (AgentType.WORKER, None, None, False, False, False, None, Lane.USER_TASK),
        (
            AgentType.REVIEWER,
            "schedule",
            None,
            False,
            False,
            False,
            None,
            Lane.BACKGROUND,
        ),
        (AgentType.WORKER, "schedule", None, False, False, True, None, Lane.USER_TASK),
        (
            AgentType.WORKER,
            "schedule",
            attempts.Trigger.RESTART,
            False,
            False,
            False,
            None,
            Lane.RECOVERY,
        ),
        (AgentType.WORKER, None, None, True, False, False, None, Lane.RECOVERY),
        (AgentType.WORKER, None, None, False, True, False, None, Lane.RECOVERY),
        (AgentType.PROCESSOR, None, None, False, False, False, None, Lane.BACKGROUND),
        (
            AgentType.ADVICE_CHECK,
            None,
            None,
            False,
            False,
            False,
            None,
            Lane.BACKGROUND,
        ),
        (AgentType.LEADS_CHECK, None, None, False, False, False, None, Lane.BACKGROUND),
        (
            AgentType.OUTBOUND_CHECK,
            None,
            None,
            False,
            False,
            False,
            None,
            Lane.BACKGROUND,
        ),
        (
            AgentType.ADVISOR,
            None,
            None,
            False,
            False,
            False,
            Lane.RECOVERY,
            Lane.RECOVERY,
        ),
    ],
)
def test_lane_mapping(
    role, schedule_id, trigger, same, recovered, run_now, parent, expected
):
    assert (
        admission.lane_for(
            role=role,
            schedule_id=schedule_id,
            trigger=trigger,
            same_attempt=same,
            recovered=recovered,
            run_now=run_now,
            parent_lane=parent,
        )
        is expected
    )


@pytest.mark.parametrize(
    "lane,seconds,expected",
    [
        (Lane.INTERACTIVE, 3600, Lane.INTERACTIVE),
        (Lane.BACKGROUND, 299, Lane.BACKGROUND),
        (Lane.BACKGROUND, 300, Lane.RECOVERY),
        (Lane.BACKGROUND, 600, Lane.USER_TASK),
        (Lane.RECOVERY, 300, Lane.USER_TASK),
        (Lane.BACKGROUND, 7200, Lane.USER_TASK),
    ],
)
def test_aging(lane, seconds, expected):
    assert admission.effective_lane(lane, seconds) is expected


def test_disabled_has_no_database_or_lock(monkeypatch):
    async def scenario():
        c = admission.Controller()
        c.lock = None  # Disabled baseline at 8516592: admission does not exist and takes no lock.
        monkeypatch.setattr(
            c, "_account", AsyncMock(side_effect=AssertionError("DB touched"))
        )
        async with c.slot("account", Lane.USER_TASK, owner="run") as grant:
            assert not grant.probe
        c._account.assert_not_called()

    asyncio.run(scenario())


def test_lane_order_and_fifo(controller):
    async def scenario():
        c = controller
        entered = []

        async def run(owner, lane):
            async with c.slot("account", lane, owner=owner):
                entered.append(owner)

        async with c.slot("account", Lane.USER_TASK, owner="held"):
            tasks = [
                asyncio.create_task(run(owner, lane))
                for owner, lane in [
                    ("background", Lane.BACKGROUND),
                    ("recovery", Lane.RECOVERY),
                    ("user1", Lane.USER_TASK),
                    ("user2", Lane.USER_TASK),
                ]
            ]
            await tick()
            assert entered == []
        await asyncio.gather(*tasks)
        assert entered == ["user1", "user2", "recovery", "background"]

    asyncio.run(scenario())


def test_nested_borrows_counts_and_kept_run(controller):
    async def scenario():
        c = controller
        await c.register("account", Lane.USER_TASK, "kept")
        async with c.slot(
            "account", Lane.BACKGROUND, owner="child", parent="kept"
        ) as grant:
            assert grant.borrowed
            assert (await c.snapshot("account"))["running"] == 2
        assert (await c.snapshot("account"))["running"] == 1
        await c.release("kept")
        assert (await c.snapshot("account"))["running"] == 0

    asyncio.run(scenario())


def test_drain_and_disable_wake_every_waiter(controller):
    async def scenario():
        c = controller
        draining = True
        entered = []

        async def run(i):
            async with c.slot(
                "account", Lane.USER_TASK, owner=str(i), draining=lambda: draining
            ):
                entered.append(i)

        tasks = [asyncio.create_task(run(i)) for i in range(3)]
        await tick()
        assert entered == []
        await c.set_enabled(False)
        await asyncio.gather(*tasks)
        assert entered == [0, 1, 2]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reset,known,minutes",
    [
        (NOW + timedelta(hours=1), True, 61),
        (NOW, False, 5),
        (NOW - timedelta(seconds=1), False, 5),
        (NOW + timedelta(days=8, seconds=1), False, 5),
        (None, False, 5),
    ],
)
def test_quota_freeze_reset_bounds(controller, reset, known, minutes):
    async def scenario():
        c = controller
        assert (
            await c.transition("account", admission.QuotaFailure(reset, None))
            is admission.AdmissionState.FROZEN
        )
        state = c.states["account"]
        assert state.until == NOW + timedelta(minutes=minutes)
        assert state.until_known is known

    asyncio.run(scenario())


def test_unknown_backoff_and_every_state_transition(controller):
    async def scenario():
        c = controller
        await c.transition("account", admission.QuotaFailure(None, None))
        for minutes in [5, 10, 20, 40, 60, 60]:
            now = c.now()
            assert c.states["account"].until == now + timedelta(minutes=minutes)
            assert (
                await c.transition("account", admission.UntilReached())
                is admission.AdmissionState.FROZEN
            )
            reached = c.states["account"].until
            c.now = lambda: reached
            assert (
                await c.transition("account", admission.UntilReached())
                is admission.AdmissionState.PROBING
            )
            assert (
                await c.transition("account", admission.ProbeEnded(False, True))
                is admission.AdmissionState.PROBING
            )
            assert (
                await c.transition("account", admission.ProbeEnded(True, False))
                is admission.AdmissionState.FROZEN
            )
        assert (
            await c.transition("account", admission.OperatorClear())
            is admission.AdmissionState.OPEN
        )
        await c.transition("account", admission.QuotaFailure(None, None))
        since = c.states["account"].since
        assert (
            await c.transition(
                "account", admission.AllowedSignal(since - timedelta(seconds=1))
            )
            is admission.AdmissionState.FROZEN
        )
        assert (
            await c.transition(
                "account", admission.AllowedSignal(since + timedelta(seconds=1))
            )
            is admission.AdmissionState.OPEN
        )
        await c.transition("account", admission.QuotaFailure(None, None))
        reached = c.states["account"].until
        c.now = lambda: reached
        await c.transition("account", admission.UntilReached())
        assert (
            await c.transition("account", admission.ProbeEnded(False, False))
            is admission.AdmissionState.OPEN
        )

    asyncio.run(scenario())


def test_one_longest_probe_then_rest(controller):
    async def scenario():
        c = controller
        await c.transition("account", admission.QuotaFailure(None, None))
        entered = []
        release = asyncio.Event()

        async def run(owner, lane):
            async with c.slot("account", lane, owner=owner) as grant:
                entered.append((owner, grant.probe))
                await release.wait()

        older = asyncio.create_task(run("old", Lane.BACKGROUND))
        await tick()
        newer = asyncio.create_task(run("new", Lane.USER_TASK))
        await tick()
        c.now = lambda: NOW + timedelta(minutes=5)
        await c.transition("account", admission.UntilReached())
        await tick()
        assert entered == [("old", True)]
        release.set()
        await asyncio.gather(older, newer)
        assert entered == [("old", True), ("new", False)]

    asyncio.run(scenario())


def test_restart_preserves_frozen_and_database_error_preserves_memory(
    controller, monkeypatch
):
    async def scenario():
        from looper.db import provider_admission

        c = controller
        await c.transition(
            "account", admission.QuotaFailure(NOW + timedelta(hours=1), None)
        )
        stored = {"provider_id": "account", **vars(c.states["account"])}
        monkeypatch.setattr(
            provider_admission, "read_all", AsyncMock(return_value=[stored])
        )
        other = admission.Controller(now=lambda: NOW)
        await other.load_states()
        assert other.states["account"].until == NOW + timedelta(hours=1, seconds=60)
        monkeypatch.setattr(
            provider_admission,
            "save",
            AsyncMock(side_effect=RuntimeError("unavailable")),
        )
        assert (
            await c.transition("account", admission.OperatorClear())
            is admission.AdmissionState.OPEN
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("utilization,blocked", [(0.94, False), (0.95, True)])
def test_background_tightening_only_background(
    controller, monkeypatch, utilization, blocked
):
    async def scenario():
        from looper.db import provider_limits, profile_registry

        c = controller
        profile_registry.get_provider.return_value["max_concurrency"] = None
        provider_limits.windows.return_value = [
            {"utilization": utilization, "resets_at": NOW + timedelta(hours=1)}
        ]
        entered = []

        async def run():
            async with c.slot("account", Lane.BACKGROUND, owner="background"):
                entered.append(True)

        task = asyncio.create_task(run())
        await tick()
        assert bool(entered) is not blocked
        async with c.slot("account", Lane.RECOVERY, owner="recovery"):
            pass
        provider_limits.windows.return_value = [
            {"utilization": 0.94, "resets_at": NOW + timedelta(hours=1)}
        ]
        await c.wake()
        await task
        assert entered == [True]

    asyncio.run(scenario())


def test_interactive_overadmits_only_when_open(controller):
    async def scenario():
        c = controller
        async with c.slot("account", Lane.USER_TASK, owner="held"):
            entered = []

            async def run():
                async with c.slot("account", Lane.INTERACTIVE, owner="interactive"):
                    entered.append(True)

            task = asyncio.create_task(run())
            await tick()
            assert not entered
            c.now = lambda: NOW + timedelta(minutes=2)
            await c.wake()
            await task
            assert entered == [True]
            assert c.daily_overadmissions() == 1
        await c.transition("account", admission.QuotaFailure(None, None))
        task = asyncio.create_task(run())
        await tick()
        entered.clear()
        c.now = lambda: NOW + timedelta(minutes=4)
        await c.wake()
        await tick()
        assert not entered
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_internal_error_fails_open(controller, monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            controller, "_account", AsyncMock(side_effect=RuntimeError("unavailable"))
        )
        async with controller.slot("account", Lane.USER_TASK, owner="run"):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "answer", [admission.Allowed(), admission.StillLimited(), admission.Unavailable()]
)
def test_typed_codex_probe(controller, monkeypatch, answer):
    async def scenario():
        from looper.db import profile_registry

        c = controller
        profile_registry.get_provider.return_value["vendor_type"] = "openai"
        profile_registry.get_provider.return_value["oauth_runtime_type"] = "codex"
        c.probe = AsyncMock(return_value=answer)
        await c.transition("account", admission.QuotaFailure(None, None))
        c.now = lambda: NOW + timedelta(minutes=5)
        await c.transition("account", admission.UntilReached())
        task = asyncio.create_task(c.acquire("account", Lane.RECOVERY, "run"))
        await tick()
        c.probe.assert_awaited_once()
        if isinstance(answer, admission.StillLimited):
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            grant = await task
            assert grant.probe is isinstance(answer, admission.Unavailable)
            await c.release("run")

    asyncio.run(scenario())


def test_unlimited_cap_still_honors_freeze_and_reports_activity(controller):
    async def scenario():
        from looper.db import profile_registry

        c = controller
        profile_registry.get_provider.return_value["max_concurrency"] = None
        await c.transition(
            "account", admission.QuotaFailure(NOW + timedelta(hours=1), None)
        )
        entered = []
        messages = []

        async def waiting(message):
            messages.append(message)

        async def run():
            async with c.slot("account", Lane.USER_TASK, owner="run", on_wait=waiting):
                entered.append(True)

        task = asyncio.create_task(run())
        await tick()
        assert not entered
        assert (
            len(messages) == 1
            and "out of quota" in messages[0]
            and (NOW + timedelta(hours=1)).astimezone().strftime("%Y-%m-%d %H:%M")
            in messages[0]
        )
        await c.transition("account", admission.OperatorClear())
        await task
        assert entered == [True]

    asyncio.run(scenario())


def test_signal_from_before_freeze_does_not_open_account(monkeypatch, controller):
    async def scenario():
        from looper.db import llm_records

        c = controller
        monkeypatch.setattr(admission, "_controller", c)
        monkeypatch.setattr(admission, "transition", c.transition)
        monkeypatch.setattr(admission, "wake", c.wake)
        await c.transition("account", admission.QuotaFailure(None, None))
        read = AsyncMock(return_value={"created_at": NOW - timedelta(seconds=1)})
        monkeypatch.setattr(llm_records, "get_record", read)
        await admission.observed("account", [("seven_day", {"status": "allowed"})], 1)
        assert c.states["account"].state is admission.AdmissionState.FROZEN
        read.return_value = {"created_at": NOW + timedelta(seconds=1)}
        await admission.observed("account", [("seven_day", {"status": "allowed"})], 2)
        assert c.states["account"].state is admission.AdmissionState.OPEN

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leak_slot(controller):
    async def scenario():
        c = controller
        async with c.slot("account", Lane.USER_TASK, owner="held"):
            task = asyncio.create_task(
                c.acquire("account", Lane.USER_TASK, "cancelled")
            )
            await tick()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not c.waiters
            assert (await c.snapshot("account"))["running"] == 1
        assert (await c.snapshot("account"))["running"] == 0

    asyncio.run(scenario())


def test_global_drain_blocks_unlimited_and_nested_runs(controller):
    async def scenario():
        c = controller
        c.draining = lambda: True
        await c.register("account", Lane.USER_TASK, "parent")
        task = asyncio.create_task(
            c.acquire("account", Lane.USER_TASK, "child", parent="parent")
        )
        await tick()
        assert not task.done()
        c.draining = lambda: False
        await c.wake()
        await c.release("parent")
        grant = await task
        assert grant.owner == "child"
        await c.release("child")

    asyncio.run(scenario())


def test_open_after_freeze_resumes_background_until_a_new_high_signal(
    controller, monkeypatch
):
    async def scenario():
        from looper.db import provider_limits

        c = controller
        provider_limits.windows.return_value = [
            {
                "utilization": 0.95,
                "resets_at": None,
                "observed_at": NOW - timedelta(seconds=1),
            }
        ]
        await c.transition("account", admission.QuotaFailure(None, None))
        await c.transition("account", admission.OperatorClear())
        async with c.slot("account", Lane.BACKGROUND, owner="resumed"):
            pass
        provider_limits.windows.return_value[0]["observed_at"] = NOW + timedelta(
            seconds=1
        )
        task = asyncio.create_task(c.acquire("account", Lane.BACKGROUND, "new-high"))
        await tick()
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_child_without_running_parent_inherits_recorded_recovery_lane(
    monkeypatch, controller
):
    async def scenario():
        from looper.db import llm_records

        c = controller
        monkeypatch.setattr(admission, "_controller", c)
        async with c.slot("account", Lane.RECOVERY, owner="parent"):
            metadata = admission.record_metadata()
            assert metadata == {"admission_lane": Lane.RECOVERY.value}
        monkeypatch.setattr(
            llm_records,
            "get_record",
            AsyncMock(
                return_value={
                    "agent_id": "parent",
                    "metadata": metadata,
                    "task_id": "task",
                }
            ),
        )
        owner, lane = await admission.parent_for(1)
        assert owner is None and lane is Lane.RECOVERY

    asyncio.run(scenario())


def test_disabled_run_metadata_has_no_admission_fields(monkeypatch, controller):
    monkeypatch.setattr(admission, "_controller", controller)
    controller.active = False
    assert admission.record_metadata() == {}
