"""Admission observes reservations without changing the execution contract."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode

NOW = datetime(2026, 11, 22, 18, tzinfo=timezone.utc)


def guard(monkeypatch, *, rows=(), cap=None):
    monkeypatch.setattr(cg.store, "history", AsyncMock(return_value=list(rows)))
    monkeypatch.setattr(cg.store, "limits", AsyncMock(return_value={}))
    return cg.Guard(Config(mode=Mode.OBSERVE, daily_usd=cap), clock=lambda: NOW)


def test_off_admission_performs_no_io_or_clock_read(monkeypatch):
    async def check():
        g = cg.Guard(Config(), clock=lambda: pytest.fail("off read the clock"))
        monkeypatch.setattr(cg.store, "history", AsyncMock(side_effect=AssertionError("off read history")))
        monkeypatch.setattr(cg.store, "limits", AsyncMock(side_effect=AssertionError("off read limits")))
        await g.rebuild()
        result = await g.admit(cg.RunKind.WORKER, (), cg.Currency.PROVIDER, 1)
        assert result.decision is cg.Decision.ADMIT and result.reservation is None
        assert g.snapshot()["counters"] == []
    asyncio.run(check())


def test_three_parallel_workers_observe_the_third_and_settle_actual(monkeypatch):
    async def check():
        g = guard(monkeypatch)
        await g.rebuild()
        scopes = (cg.Scope(cg.BudgetScope.TASK, "task-a", 2), cg.Scope(cg.BudgetScope.GLOBAL_DAY, "global", None))
        admissions = await asyncio.gather(*(g.admit(cg.RunKind.WORKER, scopes, cg.Currency.PROVIDER, 1)
                                           for _ in range(3)))
        assert [a.decision for a in admissions] == [cg.Decision.ADMIT] * 3
        assert [a.would for a in admissions] == [cg.Decision.ADMIT, cg.Decision.ADMIT, cg.Decision.ASK]
        assert g.snapshot()["counters"][0]["reserved_usd"] == 3
        for record_id, admission in enumerate(admissions, 1):
            g.bind(admission.reservation, record_id)
            g.finish(record_id, .25)
        task = next(c for c in g.snapshot()["counters"] if c["scope"] == "task")
        assert task["spent_usd"] == .75 and task["reserved_usd"] == 0
        assert len(g.snapshot()["recent"]) == 3
    asyncio.run(check())


def test_rebuild_p95_currency_and_cold_start_estimates(monkeypatch):
    async def check():
        rows = [dict(id=i, run_kind="worker", task_id=None, schedule_id=None, created_at=NOW-timedelta(days=2),
                     status="completed", cost_usd=value, currency="provider_priced_usd", metadata={})
                for i, value in enumerate([.1, .2, 1, 2], 1)]
        rows += [dict(rows[0], id=9, currency="equivalent_usd", cost_usd=5)]
        g = guard(monkeypatch, rows=rows)
        await g.rebuild()
        amount, source = g.estimate(cg.RunKind.WORKER, cg.Currency.PROVIDER, None)
        assert amount == pytest.approx(1.85) and source is cg.EstimateSource.P95
        assert g.estimate(cg.RunKind.WORKER, cg.Currency.EQUIVALENT, None) == (5, cg.EstimateSource.P95)
        assert g.estimate(cg.RunKind.OUTBOUND_CHECK, cg.Currency.PROVIDER, None) == (.05, cg.EstimateSource.FLOOR_NO_SAMPLES)
        assert g.estimate(cg.RunKind.WORKER, cg.Currency.PROVIDER, .01) == (.01, cg.EstimateSource.RUN_CAP)
        assert g.snapshot()["recommended_daily_usd"] is None  # recommendations never manufacture a default cap
    asyncio.run(check())


def test_restart_recovers_running_reservations_and_ignores_finished_reservations(monkeypatch):
    async def check():
        reservation = {"usd": 1, "currency": "provider_priced_usd", "scopes": [
            {"scope": "task", "scope_ref": "task-a", "limit_usd": 2},
            {"scope": "global_day", "scope_ref": "global", "limit_usd": None}]}
        rows = [dict(id=1, run_kind="worker", task_id="task-a", schedule_id=None, created_at=NOW,
                     status="running", cost_usd=None, currency="provider_priced_usd",
                     metadata={"reservation": reservation}),
                dict(id=2, run_kind="worker", task_id="task-a", schedule_id=None, created_at=NOW,
                     status="completed", cost_usd=.2, currency="provider_priced_usd",
                     metadata={"reservation": reservation})]
        g = guard(monkeypatch, rows=rows)
        await g.rebuild()
        task = next(c for c in g.snapshot()["counters"] if c["scope"] == "task")
        assert task["reserved_usd"] == 1 and task["spent_usd"] == .2
        g.finish(1, .3)
        g.finish(1, .3)  # a retried close cannot count twice
        task = next(c for c in g.snapshot()["counters"] if c["scope"] == "task")
        assert task["reserved_usd"] == 0 and task["spent_usd"] == .5
    asyncio.run(check())


def test_scheduler_day_boundary_is_reused_and_daily_p95_refreshed(monkeypatch):
    from looper.agents.scheduler import Scheduler
    async def check():
        now = [datetime(2026, 11, 23, 7, 59, tzinfo=timezone.utc)]
        scheduler = Scheduler(bus=SimpleNamespace(), day_zone="America/Los_Angeles")
        g = guard(monkeypatch)
        g.clock = lambda: now[0]
        g.day_start = scheduler.day_start
        await g.rebuild()
        assert g.day == datetime(2026, 11, 22, tzinfo=ZoneInfo("America/Los_Angeles"))
        await g.admit(cg.RunKind.WORKER, (), cg.Currency.PROVIDER, 1)
        now[0] += timedelta(minutes=2)
        await g.admit(cg.RunKind.WORKER, (), cg.Currency.PROVIDER, 1)
        assert cg.store.history.await_count == 2
        assert g.day == datetime(2026, 11, 23, tzinfo=ZoneInfo("America/Los_Angeles"))
    asyncio.run(check())


def test_projection_removes_only_guard_internal_fields():
    from looper.db.llm_records import normalize_record_metadata
    original = {"command": ["claude"], "reservation": {"usd": 1}, "cost_guard": {"decision": "ask"}}
    projected = normalize_record_metadata(original)
    assert "reservation" not in projected and "cost_guard" not in projected
    assert original["reservation"] == {"usd": 1}
    assert projected["command"] == ["claude"]


@pytest.mark.parametrize("auth,currency", [("oauth", cg.Currency.EQUIVALENT), ("api_key", cg.Currency.PROVIDER)])
def test_profile_auth_determines_currency_before_running(auth, currency):
    launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth=auth)))
    assert cg.currency_of(launch) is currency


@pytest.mark.parametrize('run_kind', ['worker', 'outbound_check'])
def test_guard_failure_keeps_review_exception_boundary(monkeypatch, caplog, run_kind):
    async def check():
        g = guard(monkeypatch)
        monkeypatch.setattr(cg, '_guard', g)
        cg.store.history.side_effect = OSError('synthetic accounting failure')
        launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='api_key')), max_budget_usd=1)
        assert await cg.before_run(launch=launch, run_kind=run_kind) is None
        assert g.problem and g.day is None
        assert 'synthetic accounting failure' in caplog.text
    asyncio.run(check())


@pytest.mark.parametrize('failure', ['limits', 'context', 'reservation'])
def test_observe_rebuild_and_context_failures_return_without_reserving(monkeypatch, failure):
    async def check():
        g = guard(monkeypatch)
        monkeypatch.setattr(cg, '_guard', g)
        if failure == 'limits':
            cg.store.limits.side_effect = OSError('synthetic limits failure')
        elif failure == 'context':
            monkeypatch.setattr(cg.store, 'context', AsyncMock(side_effect=OSError('synthetic context failure')))
        else:
            cg.store.history.return_value = [dict(id=1, run_kind='outbound_check', task_id=None,
                schedule_id=None, created_at=NOW, status='running', cost_usd=None,
                currency='provider_priced_usd', metadata={'reservation': {'usd': 'broken'}})]
        launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='api_key')), max_budget_usd=1)
        for _ in range(2):
            assert await cg.before_run(launch=launch, run_kind='outbound_check', task_id='task-a') is None
            assert g.problem
        assert g.snapshot()['counters'] == []
    asyncio.run(check())


@pytest.mark.parametrize('run_kind', ['worker', 'explorer'])
def test_admission_reads_new_task_cap_and_observes_a_profile_switch_in_its_new_currency(monkeypatch, run_kind):
    async def check():
        g = guard(monkeypatch)
        monkeypatch.setattr(cg, '_guard', g)
        monkeypatch.setattr(cg.store, 'context', AsyncMock(return_value={
            'tasks': {'task-new': {'cap': .5, 'schedule_id': None}}, 'schedules': {}}))
        await g.rebuild()
        launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='api_key')), max_budget_usd=1)
        first = await cg.before_run(launch=launch, run_kind=run_kind, task_id='task-new')
        assert first.observation['decision'] == 'ask'
        launch.profile.provider.auth = 'oauth'
        second = await cg.before_run(launch=launch, run_kind=run_kind, task_id='task-new')
        assert first.currency is cg.Currency.PROVIDER and second.currency is cg.Currency.EQUIVALENT
        assert len([c for c in g.snapshot()['counters'] if c['scope'] == 'global_day']) == 2
    asyncio.run(check())


def test_manager_uses_existing_turn_cap_without_changing_its_launch(monkeypatch):
    from looper.agents import session_budget
    async def check():
        g = guard(monkeypatch)
        monkeypatch.setattr(cg, '_guard', g)
        monkeypatch.setattr(session_budget, 'turn_budget_usd', AsyncMock(return_value=.2))
        launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='oauth')), max_budget_usd=None)
        reservation = await cg.before_run(launch=launch, run_kind='manager')
        assert reservation.usd == .2 and reservation.observation['estimate_source'] == 'run_cap'
        assert launch.max_budget_usd is None
    asyncio.run(check())


def test_pending_reservation_visible_in_rebuild_is_not_counted_twice(monkeypatch):
    async def check():
        g = guard(monkeypatch)
        await g.rebuild()
        scope = cg.Scope(cg.BudgetScope.GLOBAL_DAY, 'global', None)
        reservation = (await g.admit(cg.RunKind.WORKER, (scope,), cg.Currency.PROVIDER, 1)).reservation
        cg.store.history.return_value = [dict(id=1, run_kind='worker', task_id=None, schedule_id=None,
            created_at=NOW, status='running', cost_usd=None, currency='provider_priced_usd', metadata=reservation.metadata())]
        await g.rebuild()
        assert g.snapshot()['counters'][0]['reserved_usd'] == 1
        g.bind(reservation, 1)
        g.finish(1, None)
        assert g.snapshot()['counters'][0]['reserved_usd'] == 0
    asyncio.run(check())


def test_schedule_observation_does_not_double_reserve_its_model_run(monkeypatch):
    from looper.agents import agent_settings
    from tests.runtime_model_helpers import builtin_launch
    async def check():
        g = guard(monkeypatch)
        g.config = Config(mode=Mode.OBSERVE, daily_equivalent_usd=.1)
        monkeypatch.setattr(cg, '_guard', g)

        async def load_settings(*args, **kwargs):
            assert not g._lock.locked()
            return builtin_launch().settings

        monkeypatch.setattr(agent_settings, 'load_agent_settings', load_settings)
        await cg.observe_schedule({'id': 'schedule-new', 'kind': 'agent', 'project_id': None, 'run_budget_usd': 1})
        recent = g.snapshot()['recent'][0]
        assert recent['decision'] == 'deny' and recent['action'] == 'skip'
        assert all(c['reserved_usd'] == 0 for c in g.snapshot()['counters'])
    asyncio.run(check())


@pytest.mark.parametrize('currency', list(cg.Currency))
def test_schedule_observes_its_existing_shared_daily_cap_without_combining_global_currencies(monkeypatch, currency):
    async def check():
        rows = [dict(id=i, run_kind='planner', task_id=None, schedule_id='schedule-a', created_at=NOW,
                     status='completed', cost_usd=6, currency=c.value, metadata={})
                for i, c in enumerate(cg.Currency, 1)]
        g = guard(monkeypatch, rows=rows, cap=10)
        g.config = Config(mode=Mode.OBSERVE, daily_usd=10, daily_equivalent_usd=10)
        cg.store.limits.return_value = {'schedules': {'schedule-a': 10}}
        await g.rebuild()
        scopes = g.scopes(cg.RunKind.SCHEDULE, None, 'schedule-a', currency)
        result = await g.admit(cg.RunKind.SCHEDULE, scopes, currency, 1, reserve=False)
        assert result.decision is cg.Decision.ADMIT and result.would is cg.Decision.DENY
        assert g.snapshot()['recent'][0]['scope'] == 'schedule_day'
        assert g.snapshot()['recent'][0]['action'] == 'skip'
        global_scopes = g.scopes(cg.RunKind.WORKER, None, None, currency)
        result = await g.admit(cg.RunKind.WORKER, global_scopes, currency, 1, reserve=False)
        assert result.would is cg.Decision.ADMIT
    asyncio.run(check())


@pytest.mark.parametrize('mode', [Mode.OBSERVE, Mode.ENFORCE])
@pytest.mark.parametrize('kind', [cg.RunKind.WORKER, cg.RunKind.PROCESSOR])
def test_unknown_spend_alerts_every_affected_cap_on_rebuild_and_admission(monkeypatch, caplog, mode, kind):
    from looper.agents.cost_guard_settings import RoleLimits

    async def check():
        rows = [dict(id=1, run_kind=kind.value, task_id='task-a', schedule_id='schedule-a', created_at=NOW,
                     model='claude-haiku-5-5', status='completed', cost_usd=None,
                     currency=cg.Currency.PROVIDER.value, metadata={})]
        g = guard(monkeypatch, rows=rows)
        g.config = Config(mode=mode, daily_usd=10, roles={'processor': RoleLimits(daily_usd=10)})
        cg.store.limits.return_value = {'tasks': {'task-a': {'cap': 10, 'schedule_id': 'schedule-a'}},
                                       'schedules': {'schedule-a': 10}}
        await g.rebuild()
        snapshot = g.snapshot()
        assert 'claude-haiku-5-5' in snapshot['problem']
        assert all(row['cost_status'] == 'unknown' and row['unknown_models'] == ['claude-haiku-5-5']
                   for row in snapshot['counters'])
        assert {row['scope'] for row in snapshot['counters']} == (
            {'task', 'schedule_day', 'global_day', 'role_day'} if kind is cg.RunKind.PROCESSOR else
            {'task', 'schedule_day', 'global_day'})
        caplog.clear()
        await g.admit(kind, g.scopes(kind, 'task-a', 'schedule-a', cg.Currency.PROVIDER),
                      cg.Currency.PROVIDER, .1)
        assert 'claude-haiku-5-5' in caplog.text and 'cannot verify' in caplog.text
        assert all(record.levelname == 'WARNING' for record in caplog.records)
    asyncio.run(check())


def test_unknown_settlement_retains_visible_gap_until_rebuild(monkeypatch, caplog):
    async def check():
        g = guard(monkeypatch)
        await g.rebuild()
        scopes = (cg.Scope(cg.BudgetScope.TASK, 'task-a', 10),)
        reservation = (await g.admit(cg.RunKind.WORKER, scopes, cg.Currency.PROVIDER, 1)).reservation
        g.bind(reservation, 1)
        g.finish(1, None, model='new-unpriced-model')
        row = g.snapshot()['counters'][0]
        assert row['spent_usd'] == 0 and row['reserved_usd'] == 0
        assert row['cost_status'] == 'unknown' and row['unknown_models'] == ['new-unpriced-model']
        assert 'new-unpriced-model' in g.snapshot()['problem']
        assert 'new-unpriced-model' in caplog.text
        # An unrelated refresh failure must still be visible beside the price gap.
        g.problem = 'Synthetic refresh failure'
        assert 'Synthetic refresh failure' in g.snapshot()['problem']
        assert 'new-unpriced-model' in g.snapshot()['problem']
    asyncio.run(check())


def test_unknown_equivalent_cost_does_not_warn_for_separate_provider_daily_cap(monkeypatch, caplog):
    async def check():
        rows = [dict(id=1, run_kind='worker', task_id=None, schedule_id=None, created_at=NOW,
                     model='unpriced-subscription', status='completed', cost_usd=None,
                     currency=cg.Currency.EQUIVALENT.value, metadata={})]
        g = guard(monkeypatch, rows=rows, cap=1)
        await g.rebuild()
        caplog.clear()
        scopes = g.scopes(cg.RunKind.WORKER, None, None, cg.Currency.PROVIDER)
        await g.admit(cg.RunKind.WORKER, scopes, cg.Currency.PROVIDER, .1)
        assert not caplog.records
        assert next(row for row in g.snapshot()['counters'] if row['currency'] == cg.Currency.PROVIDER.value)[
            'cost_status'] == 'known'
    asyncio.run(check())


def test_unknown_auxiliary_prices_survive_settlement_and_rebuild(monkeypatch, caplog):
    async def check():
        g = guard(monkeypatch, cap=1)
        await g.rebuild()
        scopes = g.scopes(cg.RunKind.WORKER, None, None, cg.Currency.PROVIDER)
        reservation = (await g.admit(cg.RunKind.WORKER, scopes, cg.Currency.PROVIDER, .1)).reservation
        g.bind(reservation, 1)
        unknown = ['aux-unpriced-a', 'aux-unpriced-b']
        g.finish(1, None, model='known-main', unknown_models=unknown)
        assert g.snapshot()['counters'][0]['unknown_models'] == unknown
        assert all(model in caplog.text for model in unknown)
        cg.store.history.return_value = [dict(id=1, run_kind='worker', task_id=None, schedule_id=None,
            created_at=NOW, status='completed', model='known-main', cost_usd=None,
            currency=cg.Currency.PROVIDER.value, metadata={'unknown_price_models': unknown})]
        await g.rebuild()
        assert g.snapshot()['counters'][0]['unknown_models'] == unknown
        assert 'known-main' not in g.snapshot()['problem']
    asyncio.run(check())
