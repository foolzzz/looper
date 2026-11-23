"""Background and schedule caps preserve disabled paths and reserve before running."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode, RoleLimits

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
FROZEN = json.loads((Path(__file__).parent / 'fixtures/cost_guard_background_off.json').read_text())


def configured(monkeypatch, mode=Mode.ENFORCE):
    g = cg.Guard(Config(mode=mode, daily_usd=0, daily_equivalent_usd=0,
                       roles={kind.value: RoleLimits(0, 0) for kind in cg.BACKGROUND}), clock=lambda: NOW)
    g.day = g.day_start(NOW)
    g._limits = {'schedules': {'schedule-a': {'usd': 0, 'equivalent_usd': None}}}
    monkeypatch.setattr(cg, '_guard', g)
    monkeypatch.setattr(cg.store, 'context', AsyncMock(return_value={'tasks': {}, 'schedules': g._limits['schedules']}))
    return g


def launch():
    return SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='api_key')), max_budget_usd=1)


@pytest.mark.parametrize('kind', sorted(cg.BACKGROUND, key=lambda k: k.value))
@pytest.mark.parametrize('mode', [Mode.OFF, Mode.OBSERVE])
def test_each_disabled_background_admission_matches_recorded_item(monkeypatch, kind, mode):
    async def check():
        g = configured(monkeypatch, mode)
        flush = AsyncMock()
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        if mode is Mode.OFF:
            g.clock = lambda: pytest.fail('off read clock')
            cg.store.context.side_effect = AssertionError('off read database')
            monkeypatch.setattr(cg.store, 'history', AsyncMock(side_effect=AssertionError('off history read')))
        result = await cg.before_run(launch=launch(), run_kind=kind.value, schedule_id='schedule-a')
        assert type(result).__name__ == FROZEN['before_run'][mode.value][kind.value]
        assert all(not call.args[0] for call in flush.await_args_list)
        if mode is Mode.OFF:
            cg.store.context.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize('kind', sorted(cg.BACKGROUND, key=lambda k: k.value))
@pytest.mark.parametrize('scope', [cg.BudgetScope.SCHEDULE_DAY, cg.BudgetScope.ROLE_DAY, cg.BudgetScope.GLOBAL_DAY])
def test_background_denial_is_typed_silent_and_does_not_reserve(monkeypatch, kind, scope):
    async def check():
        g = configured(monkeypatch)
        chain = g.scopes(kind, None, 'schedule-a', cg.Currency.PROVIDER)
        chain = tuple(cg.Scope(s.scope, s.scope_ref, 0 if s.scope is scope else None) for s in chain)
        monkeypatch.setattr(g, 'scopes', lambda *a, **kw: chain)
        flush = AsyncMock()
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        with pytest.raises(cg.Denied) as denied:
            await cg.before_run(launch=launch(), run_kind=kind.value, schedule_id='schedule-a')
        result = denied.value.admission
        assert denied.value.reply is None
        assert result.scope.scope is scope and result.decision is cg.Decision.DENY
        assert result.action is cg.LimitAction.STOP_ROLE
        assert not g._pending
        flush.assert_awaited_once_with(result.alerts)
        assert any(a.kind is cg.cost_alerts.AlertKind.LIMIT for a in result.alerts)
        assert not g._lock.locked()
    asyncio.run(check())


@pytest.mark.parametrize('equivalent,mode,expected', [(None, Mode.ENFORCE, cg.Decision.DENY),
    (5, Mode.ENFORCE, cg.Decision.ADMIT), (5, Mode.OBSERVE, cg.Decision.ADMIT)])
def test_schedule_split_is_enforce_only(monkeypatch, equivalent, mode, expected):
    async def check():
        g = configured(monkeypatch, mode)
        g.config = Config(mode=mode)
        g._limits['schedules']['schedule-a'] = {'usd': 1, 'equivalent_usd': equivalent}
        g._spent[cg.BudgetScope.SCHEDULE_DAY, 'schedule-a', cg.Currency.EQUIVALENT] = 3
        scopes = g.scopes(cg.RunKind.SCHEDULE, None, 'schedule-a', cg.Currency.EQUIVALENT)
        result = await g.admit(cg.RunKind.SCHEDULE, scopes, cg.Currency.EQUIVALENT, .5, reserve=False)
        assert result.decision is expected
        assert result.would is (cg.Decision.ADMIT if equivalent and mode is Mode.ENFORCE else cg.Decision.DENY)
    asyncio.run(check())


def test_schedule_reservations_reject_second_firing_and_subscription_checks_processor(monkeypatch):
    async def check():
        g = configured(monkeypatch)
        g.config = Config(mode=Mode.ENFORCE, roles={'processor': RoleLimits(1)})
        g._limits['schedules']['schedule-a'] = {'usd': 1, 'equivalent_usd': None}
        chain = g.scopes(cg.RunKind.SCHEDULE, None, 'schedule-a', cg.Currency.PROVIDER, role=cg.RunKind.PROCESSOR)
        assert [s.scope for s in chain] == [cg.BudgetScope.SCHEDULE_DAY, cg.BudgetScope.ROLE_DAY, cg.BudgetScope.GLOBAL_DAY]
        assert (await g.admit(cg.RunKind.PROCESSOR, chain, cg.Currency.PROVIDER, .75)).decision is cg.Decision.ADMIT
        result = await g.admit(cg.RunKind.SCHEDULE, chain, cg.Currency.PROVIDER, .75, reserve=False)
        assert result.decision is cg.Decision.DENY and result.scope.scope is cg.BudgetScope.SCHEDULE_DAY
        assert result.spent_usd == .75 and result.action is cg.LimitAction.SKIP
    asyncio.run(check())


def test_near_limit_is_per_admission_and_flush_is_outside_lock(monkeypatch):
    async def check():
        g = configured(monkeypatch)
        g.config = Config(mode=Mode.ENFORCE, roles={'processor': RoleLimits(1)})
        g._spent[cg.BudgetScope.ROLE_DAY, 'processor', cg.Currency.PROVIDER] = .7
        value = launch()
        value.max_budget_usd = .1
        seen = []
        async def flush(alerts):
            assert not g._lock.locked()
            seen.extend(alerts)
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        await cg.before_run(launch=value, run_kind='processor')
        assert len(seen) == 1 and seen[0].kind is cg.cost_alerts.AlertKind.NEAR_LIMIT
        assert seen[0].scope.scope is cg.BudgetScope.ROLE_DAY
        assert seen[0].spent_usd == pytest.approx(.8)
    asyncio.run(check())


def test_accounting_failure_emits_guard_error_after_unlock_and_keeps_classifier_available(monkeypatch):
    async def check():
        g = configured(monkeypatch)
        g.day = None
        monkeypatch.setattr(cg.store, 'history', AsyncMock(side_effect=OSError('synthetic read failure')))
        seen = []
        async def flush(alerts):
            assert not g._lock.locked()
            seen.extend(alerts)
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        assert await cg.before_run(launch=launch(), run_kind='outbound_check') is None
        assert len(seen) == 1 and seen[0].kind is cg.cost_alerts.AlertKind.GUARD_ERROR
        assert seen[0].currency is None and seen[0].day == g.day_start(NOW)
    asyncio.run(check())


def test_deferred_schedule_accounting_error_returns_alert_without_sending(monkeypatch):
    async def check():
        configured(monkeypatch)
        monkeypatch.setattr(cg, '_schedule_launch', AsyncMock(side_effect=OSError('synthetic profile read failure')))
        flush = AsyncMock()
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        result = await cg.observe_schedule({'id': 'schedule-a', 'kind': 'agent'}, defer_alerts=True)
        assert result.decision is cg.Decision.ADMIT and result.problem
        assert len(result.alerts) == 1 and result.alerts[0].kind is cg.cost_alerts.AlertKind.GUARD_ERROR
        flush.assert_not_awaited()
    asyncio.run(check())


def test_refresh_day_rolls_alerts_even_after_an_admission_rebuilt_the_day(monkeypatch):
    from looper.agents import cost_holds
    async def check():
        g = configured(monkeypatch)
        # The new day has already been loaded by before_run; refresh_day must still service yesterday's alerts.
        rollover = AsyncMock()
        monkeypatch.setattr(cg.cost_alerts, 'rollover', rollover)
        monkeypatch.setattr(cg.cost_alerts, 'flush', AsyncMock())
        monkeypatch.setattr(cost_holds, 'expire', AsyncMock())
        await cg.refresh_day()
        rollover.assert_awaited_once_with(g.day)
    asyncio.run(check())


def test_operator_manager_spend_exception_never_announces_a_refusal(monkeypatch):
    from looper.agents import cost_manager, session_budget
    async def check():
        configured(monkeypatch)
        monkeypatch.setattr(cost_manager, 'admit', AsyncMock(return_value=True))
        monkeypatch.setattr(session_budget, 'turn_budget_usd', AsyncMock(return_value=None))
        flush = AsyncMock()
        monkeypatch.setattr(cg.cost_alerts, 'flush', flush)
        assert await cg.before_run(launch=launch(), run_kind='manager') is not None
        assert all(a.kind is not cg.cost_alerts.AlertKind.LIMIT for a in flush.await_args.args[0])
    asyncio.run(check())


@pytest.mark.parametrize('kind', [cg.RunKind.ADVISOR, cg.RunKind.ADVICE_CHECK])
@pytest.mark.parametrize('scope', [cg.BudgetScope.ROLE_DAY, cg.BudgetScope.GLOBAL_DAY])
def test_linked_task_cap_cannot_mask_a_background_role_or_global_cap(monkeypatch, kind, scope):
    async def check():
        g = configured(monkeypatch)
        g.config = Config(mode=Mode.ENFORCE, daily_usd=0,
                         roles={kind.value: RoleLimits(0)} if scope is cg.BudgetScope.ROLE_DAY else {})
        cg.store.context.return_value = {'tasks': {'task-a': {'cap': 0, 'schedule_id': None}}, 'schedules': {}}
        monkeypatch.setattr(cg.cost_alerts, 'flush', AsyncMock())
        with pytest.raises(cg.Denied) as denied:
            await cg.before_run(launch=launch(), run_kind=kind.value, task_id='task-a')
        assert denied.value.admission.scope.scope is scope
        assert denied.value.admission.action is cg.LimitAction.STOP_ROLE
    asyncio.run(check())


@pytest.mark.parametrize('mode,expected', [(Mode.ENFORCE, cg.Decision.DENY), (Mode.OBSERVE, cg.Decision.ADMIT)])
def test_real_task_dispatch_reservation_counts_toward_its_next_schedule_firing(monkeypatch, mode, expected):
    from looper.agents.agent_settings import AgentType
    async def check():
        g = configured(monkeypatch, mode)
        g.config = Config(mode=mode)
        cg.store.context.return_value = {
            'tasks': {'task-a': {'cap': 10, 'schedule_id': 'schedule-a'}},
            'schedules': {'schedule-a': {'usd': 1, 'equivalent_usd': None}}}
        value = launch()
        value.max_budget_usd = .75
        monkeypatch.setattr(cg.cost_alerts, 'flush', AsyncMock())
        monkeypatch.setattr(cg, '_schedule_launch', AsyncMock(return_value=(value, AgentType.PLANNER)))
        if mode is Mode.ENFORCE:
            first = await cg.task_gate('task-a', cg.RunKind.WORKER, value, reserve=True)
            assert first.decision is cg.Decision.ADMIT
            assert g._reserved[cg.BudgetScope.SCHEDULE_DAY, 'schedule-a', cg.Currency.PROVIDER] == .75
        else:
            await cg.before_run(launch=value, run_kind='worker', task_id='task-a')
        result = await cg.observe_schedule({'id': 'schedule-a', 'kind': 'agent',
            'run_budget_usd': .75, 'daily_budget_usd': 1}, defer_alerts=True)
        assert result.decision is expected
        assert result.would is cg.Decision.DENY
        assert result.spent_usd == .75
    asyncio.run(check())


def test_restart_counts_schedule_reservations_written_before_schedule_enforcement(monkeypatch):
    async def check():
        g = configured(monkeypatch)
        g.config = Config(mode=Mode.ENFORCE)
        limits = {'tasks': {'task-a': {'cap': 10, 'schedule_id': 'schedule-a'}},
                  'schedules': {'schedule-a': {'usd': 1, 'equivalent_usd': None}}}
        raw = {'usd': .75, 'currency': cg.Currency.PROVIDER.value, 'scopes': [
            {'scope': 'task', 'scope_ref': 'task-a'}, {'scope': 'global_day', 'scope_ref': 'global'}]}
        monkeypatch.setattr(cg.store, 'limits', AsyncMock(return_value=limits))
        monkeypatch.setattr(cg.store, 'history', AsyncMock(return_value=[{
            'id': 1, 'run_kind': 'worker', 'task_id': 'task-a', 'schedule_id': 'schedule-a',
            'created_at': NOW, 'status': 'running', 'cost_usd': None, 'currency': cg.Currency.PROVIDER.value,
            'metadata': {'reservation': raw}}]))
        await g.rebuild()
        assert g._reserved[cg.BudgetScope.SCHEDULE_DAY, 'schedule-a', cg.Currency.PROVIDER] == .75
        result = await g.admit(cg.RunKind.SCHEDULE,
            g.scopes(cg.RunKind.SCHEDULE, None, 'schedule-a', cg.Currency.PROVIDER), cg.Currency.PROVIDER, .75,
            reserve=False)
        assert result.decision is cg.Decision.DENY and result.spent_usd == .75
        g.finish(1, .1)
        assert g._reserved[cg.BudgetScope.SCHEDULE_DAY, 'schedule-a', cg.Currency.PROVIDER] == 0
        assert g._spent[cg.BudgetScope.SCHEDULE_DAY, 'schedule-a', cg.Currency.PROVIDER] == .1
    asyncio.run(check())
