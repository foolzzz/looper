"""Schedule admission and compatibility at the scheduler's launch boundary."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from looper.agents import cost_guard as cg, scheduler as sm, schedules
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.ops.schedules import TOOLS

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / 'fixtures' / 'cost_guard_background_off.json'


def row(kind='agent', equivalent=None):
    return dict(id='sched-fixed', project_id='project-fixed', project_name='Reports', manager_id='manager',
        name='Reports', cron='*/5 * * * *', timezone='UTC', requirement='Read reports', work_dir='/srv/reports',
        overlap='skip', max_parallel_runs=3, catch_up='once', run_timeout_mode='inherit',
        run_timeout_seconds=None, run_budget_usd=None, enabled=False, next_fire_at=None, last_fired_at=None,
        last_success_at=None, consecutive_failures=0, created_via='dashboard', created_at=NOW, updated_at=NOW,
        gate_command=None, daily_budget_usd=1, kind=kind, daily_budget_equivalent_usd=equivalent)


async def launch(schedule, *, spent=0, admission=None, operator=False, observe=False):
    scheduler = object.__new__(sm.Scheduler)
    scheduler._day_zone = timezone.utc
    scheduler._usage_block = AsyncMock(return_value=None)
    scheduler._create_run = AsyncMock(return_value=sm._Launched('started'))
    scheduler._start_subscription = AsyncMock(return_value=sm._Launched('started'))
    recorded = {}

    async def skip(_conn, status, _now, _common, **fields):
        recorded.update(status=status, error=fields['error'])
        return 'skipped'

    scheduler._skip = skip
    notices = []
    observer = AsyncMock(wraps=cg.observe_schedule) if observe else AsyncMock(return_value=admission)
    with patch.object(cg, 'observe_schedule', observer), \
         patch.object(sm.db_schedules, 'owner_error', AsyncMock(return_value=None)), \
         patch.object(sm.db_schedules, 'spent_since', AsyncMock(return_value=spent)), \
         patch.object(sm, 'effective_run_timeout', AsyncMock(return_value=None)):
        result = await scheduler._launch(None, schedule, sm.Firing(sm.Trigger.MANUAL, NOW), NOW,
                                         common={'schedule_id': schedule['id']}, notices=notices, operator=operator)
    return {**({'status': 'started', 'error': None} if result.run_id == 'started' else recorded)}, notices


@pytest.mark.parametrize('kind', ['agent', 'subscription'])
@pytest.mark.parametrize('mode', [Mode.OFF, Mode.OBSERVE])
@pytest.mark.parametrize('exceeded', [False, True])
def test_disabled_schedule_outcome_is_frozen(monkeypatch, kind, mode, exceeded):
    monkeypatch.setattr(cg, '_guard', cg.Guard(Config(mode=mode)))
    outcome, notices = asyncio.run(launch(row(kind, equivalent=5), spent=2 if exceeded else 0))
    assert outcome == json.loads(FIXTURE.read_text())['schedule_outcomes'][f'{kind}:{mode.value}:{exceeded}']
    assert notices == []


def test_null_equivalent_cap_keeps_view_keys():
    view = schedules.schedule_view(row())
    assert sorted(view) == sorted([*json.loads(FIXTURE.read_text())['schedule_view_keys'], 'cost_status'])
    assert view['cost_status'] == 'known'


@pytest.mark.parametrize('tool', TOOLS, ids=lambda tool: tool.name)
def test_manager_schedule_tool_properties_stay_frozen(tool):
    assert sorted(tool.schema['properties']) == json.loads(FIXTURE.read_text())['manager_schedule_tools'][tool.name]


@pytest.mark.parametrize('operator', [False, True])
def test_budget_denial_skips_manual_and_scheduled_launch(monkeypatch, operator):
    monkeypatch.setattr(cg, '_guard', cg.Guard(Config(mode=Mode.ENFORCE)))
    admission = cg.Admission(decision=cg.Decision.DENY, scope=cg.Scope(cg.BudgetScope.ROLE_DAY, 'processor', 1),
                             spent_usd=1.25, currency=cg.Currency.PROVIDER, action=cg.LimitAction.SKIP)
    outcome, notices = asyncio.run(launch(row('subscription'), admission=admission, operator=operator))
    assert outcome['status'] == 'skipped_budget'
    assert 'role_day' in outcome['error'] and '$1.2500' in outcome['error'] and '$1' in outcome['error']
    assert len(notices) == 1
    assert notices[0].schedule_id == 'sched-fixed'


@pytest.mark.parametrize('equivalent, expected', [(None, 'skipped_budget'), (5, 'started')])
def test_enforce_split_replaces_legacy_mixed_check(monkeypatch, equivalent, expected):
    monkeypatch.setattr(cg, '_guard', cg.Guard(Config(mode=Mode.ENFORCE)))
    outcome, _ = asyncio.run(launch(row(equivalent=equivalent), spent=3, admission=cg.Admission()))
    assert outcome['status'] == expected


@pytest.mark.parametrize('spent', [0, 3])
@pytest.mark.parametrize('defer_alerts', [False, True])
def test_schedule_accounting_exception_denies_and_keeps_legacy_cap(monkeypatch, spent, defer_alerts):
    g = cg.Guard(Config(mode=Mode.ENFORCE))
    monkeypatch.setattr(cg, '_guard', g)
    monkeypatch.setattr(cg, '_schedule_launch', AsyncMock(side_effect=OSError('synthetic launch failure')))
    monkeypatch.setattr(cg.cost_alerts, 'flush', AsyncMock())

    async def check():
        schedule = row(equivalent=5)
        admission = await cg.observe_schedule(schedule, defer_alerts=defer_alerts)
        assert admission.decision is cg.Decision.DENY
        assert admission.reason is cg.DenialReason.GUARD_ERROR
        assert admission.reservation is None and admission.problem
        outcome, notices = await launch(schedule, spent=spent, observe=True)
        assert outcome['status'] == 'skipped_budget'
        assert ('daily budget' if spent else 'guard_error') in outcome['error']
        assert any(a.kind is cg.cost_alerts.AlertKind.GUARD_ERROR for batch in notices for a in batch)
    asyncio.run(check())
