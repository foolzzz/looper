"""Background admission preserves each caller's failure contract and outbound audit boundary."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg, cost_guard_settings as settings
from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')
FIXTURE = Path(__file__).parent / 'fixtures' / 'cost_guard_background_callers_off.json'
STAND_IN = '''
import json, sys
sys.stdin.readline()
text = json.dumps({'decision':'allow','reason':'The operator requested this action.','signals':['operator_asked']})
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text,'total_cost_usd':0.001,'session_id':'synthetic'}), flush=True)
'''


async def _configure(pool, monkeypatch, mode, role):
    from looper.agents import cost_holds
    monkeypatch.setattr(cost_holds, '_question', AsyncMock())
    await pool.execute("INSERT INTO system_settings(key,value) VALUES ('cost_guard.mode',$1), "
                       "('cost_guard.daily_usd','0'),('cost_guard.daily_equivalent_usd','0'),"
                       "($2,'0'),($3,'0')", mode,
                       f'cost_guard.roles.{role}.daily_usd', f'cost_guard.roles.{role}.daily_equivalent_usd')
    monkeypatch.setattr(settings, '_config', settings.Config())
    monkeypatch.setattr(cg, '_guard', cg.Guard(settings.Config()))
    await cg.initialize(day_start=lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0), pool=pool)


async def _caller(role, tmp_path):
    from looper.agents import advisor, outbound, processor
    from looper.agents.agent_settings import AgentType, load_agent_settings
    from looper.agents.spawn import ChildRun, SpawnKind
    from looper.agents.system_prompt import SystemPrompt
    command = fake_claude(tmp_path / 'stand-in', STAND_IN)
    if role in ('processor', 'leads_check'):
        return await processor.run(owner='synthetic', instructions='Read the batch.', input_text='Build passed.',
            input_ref={'kind': 'synthetic'}, quiet_word='SKIP', budget_usd=0.1, timeout_s=10,
            runtime_root=tmp_path / 'runtime', claude_bin=command, role=AgentType(role))
    if role in ('advisor', 'advice_check'):
        child = ChildRun(SpawnKind.ADVISOR,
                         SystemPrompt('Read the request.'), 'Synthetic request', 10, role=AgentType(role), run_kind=role)
        result, _ = await advisor._run(child, spawn_id=123, brief=advisor.Brief(None, None, 1, advisor.Source.OPTIONAL),
            settings=await load_agent_settings(AgentType(role)), cap_usd=0.1, runtime_root=tmp_path / 'runtime',
            claude_bin=command, parse=lambda text: text)
        return result
    return await outbound.review(outbound.Action('synthetic_send', outbound.ActionLevel.EXTERNAL, 'chat',
            'Synthetic build', 'Post the requested status.', content='Build passed.'),
        outbound.Asker(outbound.Who.OPERATOR, 'web', 'Post the status.'),
        outbound.Origin('manager', outbound.Source.TOOL), runtime_root=tmp_path / 'runtime', claude_bin=command)


def _shape(result):
    failure = getattr(result, 'failure', None)
    return {'type': type(result).__name__, 'failure': failure.value if failure is not None else None}


@pytest.mark.parametrize('mode', ['off', 'observe'])
@pytest.mark.parametrize('role', settings.ROLES)
def test_background_callers_match_the_recorded_off_and_observe_result(monkeypatch, tmp_path, mode, role):
    async def body(pool):
        await _configure(pool, monkeypatch, mode, role)
        from looper.agents import cost_alerts
        sender = AsyncMock()
        monkeypatch.setattr(cost_alerts, '_sender', sender)
        result = await _caller(role, tmp_path)
        sender.assert_not_awaited()
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_alert:%'") == 0
        expected = json.loads(FIXTURE.read_text())
        assert expected['recorded_at'] == '3c718301c47d3e246ffb3342d7c2bb0f47d6daed'
        assert _shape(result) == expected['callers'][f'{mode}:{role}']
    run_db(body, monkeypatch, timeout=30)


@pytest.mark.parametrize('role', settings.ROLES)
def test_background_role_denials_use_budget_failure_without_starting_a_model(monkeypatch, tmp_path, role):
    async def body(pool):
        from looper.agents import cost_holds
        await _configure(pool, monkeypatch, 'enforce', role)
        result = await _caller(role, tmp_path)
        assert result.failure.value == 'budget'
        assert await pool.fetchval('SELECT count(*) FROM llm_records') == 0
        if role == 'outbound_check':
            assert result.decision.value == 'block'
            assert await pool.fetchval("SELECT count(*) FROM outbound_reviews WHERE failure='budget'") == 1
            assert await pool.fetchval("SELECT count(*) FROM cost_holds WHERE scope='role_day' "
                "AND scope_ref='outbound_check' AND reason='review_budget'") == 1
            cost_holds._question.assert_awaited_once()
    run_db(body, monkeypatch, timeout=30)


def test_review_hold_failure_keeps_the_action_blocked_and_audited(monkeypatch, tmp_path):
    async def body(pool):
        from looper.agents import cost_holds
        await _configure(pool, monkeypatch, 'enforce', 'outbound_check')
        monkeypatch.setattr(cost_holds, 'create_hold', AsyncMock(side_effect=RuntimeError('synthetic hold failure')))
        result = await _caller('outbound_check', tmp_path)
        assert result.failure.value == 'budget' and result.decision.value == 'block'
        assert await pool.fetchval("SELECT count(*) FROM outbound_reviews WHERE verdict='block'") == 1
    run_db(body, monkeypatch, timeout=30)


def test_review_accounting_failure_runs_classifier_and_still_requires_audit(monkeypatch, tmp_path):
    async def body(pool):
        from looper.db import outbound_reviews
        await _configure(pool, monkeypatch, 'enforce', 'outbound_check')
        cg.current().day = None
        failed_read = AsyncMock(side_effect=RuntimeError('synthetic accounting failure'))
        monkeypatch.setattr(cg.store, 'history', failed_read)
        allowed = await _caller('outbound_check', tmp_path)
        assert allowed.decision.value == 'allow'
        assert await pool.fetchval('SELECT count(*) FROM llm_records') == 1
        assert await pool.fetchval("SELECT count(*) FROM outbound_reviews WHERE verdict='allow'") == 1
        failed_read.assert_awaited_once()
        monkeypatch.setattr(outbound_reviews, 'insert', AsyncMock(side_effect=RuntimeError('synthetic audit failure')))
        blocked = await _caller('outbound_check', tmp_path)
        assert blocked.decision.value == 'block'
    run_db(body, monkeypatch, timeout=30)


def test_review_continued_role_hold_allows_a_fresh_review_without_replaying_the_action(monkeypatch, tmp_path):
    async def body(pool):
        from looper.agents import cost_holds
        await _configure(pool, monkeypatch, 'enforce', 'outbound_check')
        await pool.execute("DELETE FROM system_settings WHERE key IN ('cost_guard.daily_usd', "
                           "'cost_guard.daily_equivalent_usd')")
        await cg.refresh()
        blocked = await _caller('outbound_check', tmp_path)
        assert blocked.failure.value == 'budget'
        hold = await pool.fetchrow("SELECT * FROM cost_holds WHERE reason='review_budget'")
        with pytest.raises(ValueError, match='上限'):
            await cost_holds.decide(hold['id'], 'continue', 0.01, via='dashboard', pool=pool)
        await cost_holds.decide(hold['id'], 'continue', 1, via='dashboard', pool=pool)
        assert await pool.fetchval("SELECT value FROM system_settings "
            "WHERE key='cost_guard.roles.outbound_check.daily_equivalent_usd'") == '1.0'
        assert await pool.fetchval('SELECT count(*) FROM llm_records') == 0
        allowed = await _caller('outbound_check', tmp_path)
        assert allowed.decision.value == 'allow'
        assert await pool.fetchval('SELECT count(*) FROM outbound_reviews') == 2
    run_db(body, monkeypatch, timeout=30)


def test_lead_check_budget_denial_keeps_original_items_unchecked(monkeypatch, tmp_path):
    async def body(pool):
        from looper.agents import extraction, leads_check
        await _configure(pool, monkeypatch, 'enforce', 'leads_check')
        items = [{'kind': 'fact', 'confidence': 'high', 'summary': 'The build passed.', 'sources': []}]
        before = json.loads(json.dumps(items))
        result = await leads_check.check('leads', items, messages={}, participants=extraction.Participants({}, None),
            zone_name='UTC', text_max_chars=1000, types={}, owner='synthetic', reference={'kind': 'synthetic'},
            budget_usd=0.1, timeout_s=10, runtime_root=tmp_path / 'runtime',
            claude_bin=fake_claude(tmp_path / 'stand-in', STAND_IN))
        assert result.counts['check_failed'] is True
        assert result.detail.startswith('budget: leads_check reached today')
        assert items == before
        assert await pool.fetchval('SELECT count(*) FROM llm_records') == 0
    run_db(body, monkeypatch, timeout=30)
