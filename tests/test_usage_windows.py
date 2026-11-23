"""Usage-window holds: which signal waits, and which callers only record it.

``mode = off`` matches the recorded baseline: no window read, no deferral, an empty observation list.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg
from looper.agents import usage_windows as windows
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.usage_windows import Hold, HoldReason

NOW = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
PROVIDER = "prov-synthetic"
# Recorded while the guard is off: the same calls read nothing and change nothing.
OFF_MODE = {
    "before_run": None,
    "observe_schedule": None,
    "recent": [],
    "hold_reads": 0,
    "window_reads": 0,
}


def _window(*, status="allowed", utilization=0.1, resets_at=None, observed_at=None, name="five_hour"):
    return {"status": status, "utilization": utilization, "resets_at": resets_at,
            "observed_at": observed_at if observed_at is not None else NOW, "window_name": name}


def _failure(*, resets_at=None, finished_at=None, created_at=None, error="retry at 2099-01-01T00:00:00Z"):
    metadata = {"failure": {}}
    if resets_at is not None:
        metadata["failure"]["resets_at"] = resets_at
    return {"finished_at": finished_at, "created_at": created_at, "metadata": metadata, "error": error}


def test_off_mode_matches_the_recorded_baseline(monkeypatch):
    async def check():
        g = _guard(monkeypatch, Mode.OFF)
        reads = {"hold": 0, "windows": 0}

        async def hold_for(*_args, **_kwargs):
            reads["hold"] += 1
            raise AssertionError("off read a usage window")

        async def windows_read(*_args, **_kwargs):
            reads["windows"] += 1
            raise AssertionError("off read provider_limit_windows")

        monkeypatch.setattr(windows, "hold_for", hold_for)
        monkeypatch.setattr(windows.provider_limits, "windows", windows_read)
        before = await cg.before_run(launch=_launch(), run_kind="processor")
        observed = await cg.observe_schedule({"id": "schedule-1", "kind": "agent", "project_id": None,
                                               "run_budget_usd": 1})
        assert {
            "before_run": before,
            "observe_schedule": observed,
            "recent": g.snapshot()["recent"],
            "hold_reads": reads["hold"],
            "window_reads": reads["windows"],
        } == OFF_MODE
    asyncio.run(check())


def test_rejected_window_holds_until_its_reset_and_a_past_reset_holds_nothing():
    future = NOW + timedelta(hours=2)
    held = windows.hold_from([_window(status="rejected", resets_at=future)], None, background=False, now=NOW)
    assert held == Hold(future, HoldReason.REJECTED, True, "five_hour")
    later = windows.hold_from(
        [_window(status="rejected", resets_at=future, name="seven_day"),
         _window(status="rejected", resets_at=future + timedelta(hours=1), name=None)],
        None, background=False, now=NOW)
    assert later.until == future + timedelta(hours=1) and later.window is None
    assert windows.hold_from([_window(status="rejected", resets_at=NOW - timedelta(seconds=1))],
                             None, background=True, now=NOW) is None
    assert windows.hold_from([_window(status="rejected", resets_at=NOW)], None, background=True, now=NOW) is None


def test_high_utilization_holds_only_background_work_and_094_holds_nothing():
    future = NOW + timedelta(hours=1)
    high = _window(utilization=0.95, resets_at=future)
    assert windows.hold_from([high], None, background=True, now=NOW) == Hold(
        future, HoldReason.HIGH_UTILIZATION, True, "five_hour")
    assert windows.hold_from([high], None, background=False, now=NOW) is None
    assert windows.hold_from([_window(utilization=0.94, resets_at=future)], None, background=True, now=NOW) is None


def test_open_window_cooldown_comes_from_observed_at_and_is_not_extended():
    rejected = _window(status="rejected", resets_at=None, observed_at=NOW)
    opened = NOW + timedelta(minutes=1)
    held = windows.hold_from([rejected], None, background=False, now=opened)
    assert held == Hold(NOW + timedelta(minutes=5), HoldReason.REJECTED, False, "five_hour")
    for _ in range(10):
        again = windows.hold_from([rejected], None, background=False, now=opened + timedelta(seconds=30))
        assert again.until == held.until
    windows.clear_cache()
    assert windows.hold_from([rejected], None, background=False, now=opened).until == held.until
    assert windows.hold_from([rejected], None, background=False, now=held.until) is None
    high = _window(utilization=0.95, resets_at=None, observed_at=NOW)
    assert windows.hold_from([high], None, background=True, now=opened).reason is HoldReason.HIGH_UTILIZATION
    assert windows.hold_from([high], None, background=False, now=opened) is None


def test_quota_failure_uses_the_reset_field_and_otherwise_cools_down_from_the_end_time():
    reset = NOW + timedelta(hours=3)
    held = windows.hold_from(None, _failure(resets_at=reset.isoformat(), finished_at=NOW), background=False, now=NOW)
    assert held == Hold(reset, HoldReason.QUOTA_FAILURE, True, None)
    # The error text names another instant. The field is the clock.
    assert "2099" not in held.until.isoformat()
    cooled = windows.hold_from(None, _failure(finished_at=NOW, created_at=NOW - timedelta(hours=1)),
                               background=False, now=NOW + timedelta(minutes=1))
    assert cooled == Hold(NOW + timedelta(minutes=5), HoldReason.QUOTA_COOLDOWN, False, None)
    from_created = windows.hold_from(None, _failure(created_at=NOW), background=True, now=NOW)
    assert from_created.until == NOW + timedelta(minutes=5)
    assert windows.hold_from(None, _failure(finished_at=NOW - timedelta(minutes=5)), background=True, now=NOW) is None


def test_a_known_reset_notifies_only_when_rejected():
    future = NOW + timedelta(hours=1)
    rejected = Hold(future, HoldReason.REJECTED, True, "five_hour")
    high = Hold(future, HoldReason.HIGH_UTILIZATION, True, "five_hour")
    quota = Hold(future, HoldReason.QUOTA_FAILURE, True, None)
    cooled = Hold(future, HoldReason.QUOTA_COOLDOWN, False, None)
    assert windows.notifies(rejected) and windows.notifies(cooled)
    assert not windows.notifies(high) and not windows.notifies(quota)
    assert "将等待到" in windows.notice_text(PROVIDER, rejected) and "后台工作会等待" in windows.notice_text(PROVIDER, rejected)
    assert "重置时间未知" in windows.notice_text(PROVIDER, cooled) and PROVIDER in windows.notice_text(PROVIDER, cooled)


def _launch(provider_id=PROVIDER):
    return SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(id=provider_id, auth="oauth")),
                           max_budget_usd=1)


def _guard(monkeypatch, mode):
    g = cg.Guard(Config(mode=mode), clock=lambda: NOW)
    g.day = g.day_start(NOW)
    monkeypatch.setattr(cg.store, "history", AsyncMock(return_value=[]))
    monkeypatch.setattr(cg.store, "limits", AsyncMock(return_value={}))
    monkeypatch.setattr(cg, "_guard", g)
    return g


def test_off_reads_no_window_and_observe_records_without_holding(monkeypatch):
    async def check():
        g = _guard(monkeypatch, Mode.OFF)
        monkeypatch.setattr(windows, "hold_for", AsyncMock(side_effect=AssertionError("off read a usage window")))
        monkeypatch.setattr(windows.provider_limits, "windows",
                            AsyncMock(side_effect=AssertionError("off read provider_limit_windows")))
        assert await cg.before_run(launch=_launch(), run_kind="processor") is None
        assert await cg.observe_schedule({"id": "schedule-1", "kind": "agent", "project_id": None,
                                           "run_budget_usd": 1}) is None
        assert g.snapshot()["recent"] == [] and windows.hold_for.await_count == 0

        g.config = Config(mode=Mode.OBSERVE)
        future = NOW + timedelta(hours=1)
        monkeypatch.setattr(windows, "hold_for", AsyncMock(return_value=Hold(
            future, HoldReason.HIGH_UTILIZATION, True, "five_hour")))
        reservation = await cg.before_run(launch=_launch(), run_kind="processor")
        assert reservation is not None and reservation.observation["decision"] == "admit"
        assert reservation.observation["note"] == f"would hold until {future.isoformat()}"
        assert reservation.observation["would_hold_reason"] == "high_utilization"
    asyncio.run(check())


def test_enforce_holds_a_background_run_and_only_records_one_you_start(monkeypatch):
    async def check():
        g = _guard(monkeypatch, Mode.ENFORCE)
        future = NOW + timedelta(minutes=30)
        notes = []

        async def note(provider_id, hold):
            notes.append((provider_id, hold))

        monkeypatch.setattr(windows, "notify_hold", note)
        monkeypatch.setattr(windows, "hold_for", AsyncMock(return_value=Hold(
            future, HoldReason.HIGH_UTILIZATION, True, "five_hour")))
        with pytest.raises(cg.Held, match="usage window on hold until ") as raised:
            await cg.before_run(launch=_launch(), run_kind="outbound_check")
        assert raised.value.until == future and raised.value.reason == "high_utilization"
        assert notes and notes[0][0] == PROVIDER
        assert all(row["reserved_usd"] == 0 for row in g.snapshot()["counters"])

        notes.clear()
        started = await cg.before_run(launch=_launch(), run_kind="worker")
        assert started is not None and started.observation["decision"] == "admit"
        assert started.observation["note"].startswith("would hold until ")
        assert notes == []
    asyncio.run(check())


def test_a_schedule_hold_is_enforced_only_in_enforce(monkeypatch):
    from looper.agents import agent_settings
    from tests.runtime_model_helpers import builtin_launch

    async def check():
        g = _guard(monkeypatch, Mode.ENFORCE)
        future = NOW + timedelta(hours=1)

        async def load_settings(*_args, **_kwargs):
            return builtin_launch().settings

        monkeypatch.setattr(agent_settings, "load_agent_settings", load_settings)
        monkeypatch.setattr(windows, "hold_for", AsyncMock(return_value=Hold(
            future, HoldReason.REJECTED, True, "five_hour")))
        admission = await cg.observe_schedule({"id": "schedule-1", "kind": "agent", "project_id": None,
                                                "run_budget_usd": 1})
        assert admission.decision is cg.Decision.HOLD and admission.until == future
        assert admission.reservation is None and admission.provider_id == "prov-claude-subscription"
        assert g.snapshot()["recent"][0]["note"] == f"hold until {future.isoformat()}"

        g.config = Config(mode=Mode.OBSERVE)
        recorded = await cg.observe_schedule({"id": "schedule-1", "kind": "agent", "project_id": None,
                                               "run_budget_usd": 1})
        assert recorded.decision is cg.Decision.ADMIT
        assert recorded.reservation is None
        assert g.snapshot()["recent"][0]["note"].startswith("would hold until ")
    asyncio.run(check())


def test_limit_signals_off_skips_the_window_table_and_still_reads_a_quota_failure(monkeypatch):
    async def check():
        windows.clear_cache()
        monkeypatch.setattr(windows.limit_signals, "enabled", AsyncMock(return_value=False))
        monkeypatch.setattr(windows.provider_limits, "windows", AsyncMock(side_effect=AssertionError("window table")))
        failure = _failure(finished_at=NOW)
        monkeypatch.setattr(windows, "latest_quota_failure", AsyncMock(return_value=failure))
        held = await windows.hold_for(PROVIDER, background=False, now=NOW + timedelta(minutes=1))
        assert held.reason is HoldReason.QUOTA_COOLDOWN and held.until == NOW + timedelta(minutes=5)
        windows.provider_limits.windows.assert_not_called()
        windows.clear_cache()
        assert (await windows.hold_for(PROVIDER, background=False, now=NOW + timedelta(minutes=1))).until == held.until
    asyncio.run(check())


def test_usage_window_reads_stay_outside_the_admission_lock(monkeypatch):
    from looper.agents import agent_settings
    from tests.runtime_model_helpers import builtin_launch

    async def check():
        g = _guard(monkeypatch, Mode.ENFORCE)
        future = NOW + timedelta(minutes=10)
        seen: list[bool] = []

        async def hold_for(*_args, **_kwargs):
            seen.append(g._lock.locked())
            return Hold(future, HoldReason.REJECTED, True, "five_hour")

        async def load_settings(*_args, **_kwargs):
            return builtin_launch().settings

        monkeypatch.setattr(windows, "hold_for", hold_for)
        monkeypatch.setattr(windows, "notify_hold", AsyncMock())
        monkeypatch.setattr(agent_settings, "load_agent_settings", load_settings)
        admission = await cg.observe_schedule({"id": "schedule-1", "kind": "agent", "project_id": None,
                                                "run_budget_usd": 1})
        assert admission is not None and admission.decision is cg.Decision.HOLD
        with pytest.raises(cg.Held):
            await cg.before_run(launch=_launch(), run_kind="processor")
        assert seen == [False, False]
    asyncio.run(check())


def test_an_advisor_consult_returns_no_advice_and_does_not_run_again(monkeypatch, tmp_path):
    from looper.agents import advisor
    from looper.agents.advisor import Brief, Source
    from looper.agents.model_call import CallFailure
    from looper.agents.spawn import ChildRun, SpawnKind
    from looper.agents.system_prompt import SystemPrompt

    calls = []

    async def boom(*_args, **_kwargs):
        calls.append(1)
        raise cg.Held(NOW + timedelta(minutes=5), "quota_cooldown")

    async def check():
        monkeypatch.setattr(advisor, "call", boom)
        child = ChildRun(SpawnKind.ADVISOR, SystemPrompt("prompt"), "ask", 30)
        brief = Brief("task-1", "step-1", 1, Source.OPTIONAL)
        done, spent = await advisor._run(child, spawn_id=1, brief=brief, settings=SimpleNamespace(), cap_usd=1,
                                         runtime_root=tmp_path, claude_bin="claude", parse=lambda text: text)
        assert calls == [1] and spent == 0
        assert done.failure is CallFailure.UNAVAILABLE
        assert str(done.reason).startswith("usage window on hold until ")
    asyncio.run(check())


def test_an_advisor_retry_stops_when_the_usage_window_holds(monkeypatch, tmp_path):
    from looper.agents import advisor
    from looper.agents.advisor import Brief, Source
    from looper.agents.model_call import CallFailed, CallFailure, Execution
    from looper.agents.spawn import ChildRun, SpawnKind
    from looper.agents.system_prompt import SystemPrompt

    calls = []

    async def boom(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return CallFailed(execution=Execution(None, cost_usd=0.0, failure=CallFailure.RUNTIME, reason="runtime"),
                              failure=CallFailure.RUNTIME, reason="runtime")
        raise cg.Held(NOW + timedelta(minutes=5), "quota_cooldown")

    async def check():
        monkeypatch.setattr(advisor, "call", boom)
        child = ChildRun(SpawnKind.ADVISOR, SystemPrompt("prompt"), "ask", 30)
        brief = Brief("task-1", "step-1", 1, Source.OPTIONAL)
        done, spent = await advisor._run(child, spawn_id=1, brief=brief, settings=SimpleNamespace(), cap_usd=1,
                                         runtime_root=tmp_path, claude_bin="claude", parse=lambda text: text)
        assert calls == [1, 1] and spent == 0
        assert done.failure is CallFailure.UNAVAILABLE
        assert str(done.reason).startswith("usage window on hold until ")
    asyncio.run(check())


def test_cost_guard_get_lists_holds_only_while_the_guard_is_on(monkeypatch):
    from looper.agents import cost_guard_settings as settings
    from looper.agents.v1_domains import cost_guard as domain

    async def check():
        previous = settings._config
        try:
            settings._config = Config()
            off = await domain.run(None, domain.OPERATIONS[0], {}, None, None)
            assert "holds" not in off and off["mode"] == "off"
            settings._config = Config(mode=Mode.ENFORCE)
            monkeypatch.setattr(windows, "list_holds", AsyncMock(return_value=[{
                "provider": PROVIDER, "reason": "rejected", "until": NOW.isoformat(),
                "window": "five_hour", "reset_known": True}]))
            view = await domain.run(None, domain.OPERATIONS[0], {}, None, None)
            assert view["holds"] == [{
                "provider": PROVIDER, "reason": "rejected", "until": NOW.isoformat(),
                "window": "five_hour", "reset_known": True}]
        finally:
            settings._config = previous
    asyncio.run(check())


def test_a_manager_schedule_result_omits_a_usage_hold():
    from looper.agents.ops import Call
    from looper.agents.ops.schedules import _in_scope

    async def check():
        view = {"id": "schedule-1", "usage_hold": {"until": NOW.isoformat(), "reason": "rejected",
                                                   "reset_known": True, "window": "five_hour"}}
        call = Call({}, SimpleNamespace(), None, scope=SimpleNamespace(scoped=False, manager_id="manager"))
        shown = await _in_scope(call, view)
        assert "usage_hold" not in shown
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["manager", "planner", "worker", "reviewer", "consult"])
def test_self_started_runs_hold_on_rejection_but_not_high_utilization(monkeypatch, kind):
    from looper.agents import cost_manager, session_budget

    async def check():
        g = _guard(monkeypatch, Mode.ENFORCE)
        monkeypatch.setattr(cost_manager, "admit", AsyncMock(return_value=False))
        monkeypatch.setattr(session_budget, "turn_budget_usd", AsyncMock(return_value=None))
        monkeypatch.setattr(windows, "notify_hold", AsyncMock())
        future = NOW + timedelta(hours=1)
        rows = [_window(utilization=0.95, resets_at=future)]

        async def hold_for(provider_id, *, background, now, pool):
            return windows.hold_from(rows, None, background=background, now=now)

        monkeypatch.setattr(windows, "hold_for", hold_for)
        started = await cg.before_run(launch=_launch(), run_kind=kind)
        assert started is not None
        await cg.abandon(started)
        rows[0]["status"] = "rejected"
        with pytest.raises(cg.Held) as raised:
            await cg.before_run(launch=_launch(), run_kind=kind)
        assert raised.value.until == future and raised.value.reason == "rejected"
        assert not g._pending and all(value == 0 for value in g._reserved.values())
    asyncio.run(check())
