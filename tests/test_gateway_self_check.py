"""Gateway health is required only when a saved profile needs translation."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import self_check
from looper.agents.llmrec import control


@pytest.mark.parametrize('profiles,state,missing,expected,detail', [
    ([], 'stopped', 0, self_check.SKIP, 'no Agent Profile'),
    ([{'type': 'codex', 'protocol': 'openai-chat'}], 'stopped', 0, self_check.SKIP, 'no Agent Profile'),
    ([{'type': 'claude-code', 'protocol': 'anthropic-messages'}], 'stopped', 0, self_check.SKIP, 'no Agent Profile'),
    ([{'type': 'claude-code', 'protocol': 'openai-chat'}], 'unavailable', 0, self_check.FAIL, 'cannot start'),
    ([{'type': 'claude-code', 'protocol': 'openai-chat'}], 'outdated', 0, self_check.WARN, 'older gateway'),
    ([{'type': 'claude-code', 'protocol': 'openai-chat'}], 'running', 2, self_check.WARN, 'underestimated'),
    ([{'type': 'claude-code', 'protocol': 'openai-chat'}], 'running', 0, self_check.OK, 'gateway running'),
])
def test_gateway_check_reports_translation_availability(monkeypatch, profiles, state, missing, expected, detail):
    from types import SimpleNamespace

    monkeypatch.setattr(self_check, '_translated_profile_rows', AsyncMock(return_value=profiles))
    owner = SimpleNamespace(status=AsyncMock(return_value={
        'enabled': False, 'proxy': {'state': state}, 'translate': {'usage_missing': missing}}))
    monkeypatch.setattr(control, '_control', owner)
    result = asyncio.run(self_check.gateway_check())
    assert result.status == expected
    assert detail in result.detail
    assert self_check.LOGIN_CHECKS['gateway'] is self_check.gateway_check


def test_gateway_check_without_controller_fails_for_translation(monkeypatch):
    monkeypatch.setattr(self_check, '_translated_profile_rows', AsyncMock(return_value=[
        {'type': 'claude-code', 'protocol': 'openai-chat'}]))
    monkeypatch.setattr(control, '_control', None)
    assert asyncio.run(self_check.gateway_check()).status == self_check.FAIL
