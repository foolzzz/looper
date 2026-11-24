"""Budget refusal is control state, including a refusal of an advisor retry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import advisor, cost_guard as cg
from looper.agents.model_call import CallFailed, CallFailure, Execution
from looper.agents.spawn import ChildRun, SpawnKind
from looper.agents.system_prompt import SystemPrompt


@pytest.mark.parametrize('role', ['advisor', 'advice_check'])
@pytest.mark.parametrize('retry', [False, True])
def test_advisor_budget_denial_does_not_retry_and_preserves_already_booked_spend(monkeypatch, tmp_path, role, retry):
    from looper.agents.agent_settings import AgentType
    admission = cg.Admission(decision=cg.Decision.DENY,
        scope=cg.Scope(cg.BudgetScope.ROLE_DAY, role, 1), currency=cg.Currency.PROVIDER, spent_usd=1)
    denied = cg.Denied(None, admission=admission)
    prior = CallFailed(execution=Execution(None, cost_usd=0.1, failure=CallFailure.RUNTIME),
                       failure=CallFailure.RUNTIME, reason='synthetic runtime failure')
    call = AsyncMock(side_effect=[prior, denied] if retry else denied)
    monkeypatch.setattr(advisor, 'call', call)
    async def body():
        child = ChildRun(SpawnKind.ADVISOR, SystemPrompt('prompt'), 'request', 10, role=AgentType(role))
        result, spent = await advisor._run(child, spawn_id=1,
            brief=advisor.Brief('synthetic-task', None, 1, advisor.Source.OPTIONAL), settings=SimpleNamespace(),
            cap_usd=1, runtime_root=tmp_path, claude_bin='stand-in', parse=str)
        assert result.failure is CallFailure.BUDGET
        assert result.execution.failure is CallFailure.BUDGET
        assert spent == (0.1 if retry else 0)
        assert call.await_count == (2 if retry else 1)
        assert role in result.reason and 'role_day' in result.reason
    asyncio.run(body())
