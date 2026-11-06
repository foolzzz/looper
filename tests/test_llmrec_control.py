"""Recording failures remain visible without preventing a run from launching."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.llmrec import TLS_ENV, LlmrecSkip, RouteFile, SkipReason
from looper.agents.llmrec import control
from looper.agents.profile_registry import builtin_resolved_profile
from looper.db import activity


@pytest.fixture
def launch():
    return Launch(AgentSettings(profile=builtin_resolved_profile()))


def test_proxy_process_leaves_tls_overrides_to_routes(tmp_path, monkeypatch):
    owner = control.ProxyControl(tmp_path, '', enabled=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=None))
    for name in TLS_ENV:
        monkeypatch.setenv(name, 'route-only.pem')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example:8080')
    spawn = AsyncMock(side_effect=OSError('stop before spawning'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(OSError, match='stop before spawning'):
        asyncio.run(owner._start())
    environment = spawn.call_args.kwargs['env']
    assert not TLS_ENV.intersection(environment)
    assert environment['HTTPS_PROXY'] == 'http://proxy.example:8080'


def test_self_check_reports_gaps_through_proxy_status(tmp_path, monkeypatch):
    from looper.agents import api_v1, self_check
    from looper.agents.llmrec.proxy import Proxy
    from looper.agents.llmrec.writer import DbSink

    sink = DbSink(None)
    sink.recorded = 4
    sink.gaps = 3
    sink.gap_reasons = {'unparsable_stream': 1, 'rejected_by_database': 2}
    proxy = Proxy(tmp_path, sink=sink)
    owner = control.ProxyControl(tmp_path, '', enabled=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=proxy.stats))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234, 'pid': 1})
    monkeypatch.setattr(control, '_control', owner)
    status = asyncio.run(owner.status())
    api_v1.validate_arguments(api_v1.find('POST', '/api/v1/llmrec_settings/get').result, status)
    assert status['proxy']['recorded'] == 4 and status['proxy']['gaps'] == 3
    check = asyncio.run(self_check.llmrec_check())
    assert check.status == self_check.WARN
    assert 'gaps=3' in check.detail and 'rejected_by_database' in check.detail
    fresh = Proxy(tmp_path, sink=DbSink(None))
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=fresh.stats))
    assert asyncio.run(self_check.llmrec_check()).status == self_check.OK


@pytest.mark.parametrize('failure', ['start_false', 'start_error', 'missing_state', 'route_write'])
def test_proxy_bypass_warns_once_and_adds_task_activity(tmp_path, monkeypatch, caplog, failure, launch):
    owner = control.ProxyControl(tmp_path, '', enabled=True, include_oauth=True)
    monkeypatch.setattr(control, '_control', owner)
    monkeypatch.setattr(type(launch.runtime), 'llmrec_upstream', lambda self, launch: 'https://api.anthropic.com')
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=None if failure.startswith('start') else True))
    monkeypatch.setattr(owner, '_start', AsyncMock(return_value=False,
        side_effect=OSError('sensitive error') if failure == 'start_error' else None))
    monkeypatch.setattr(owner, '_state', lambda: None if failure == 'missing_state' else {'port': 1234})
    if failure == 'route_write':
        def denied(self, path):
            raise OSError('sensitive error')
        monkeypatch.setattr(RouteFile, 'write', denied)
    insert = AsyncMock()
    monkeypatch.setattr(activity, 'insert_activity', insert)
    with caplog.at_level(logging.WARNING, logger=control.__name__):
        attached = asyncio.run(control.attach(launch, agent_id='worker-test', task_id='task-test'))
    assert attached.llmrec == LlmrecSkip(SkipReason.PROXY_UNAVAILABLE)
    assert attached.environment() == launch.environment()
    warnings = [record for record in caplog.records if record.name == control.__name__]
    assert len(warnings) == 1
    assert 'worker-test' in warnings[0].message and 'proxy_unavailable' in warnings[0].message
    assert 'sensitive error' not in caplog.text
    insert.assert_awaited_once_with(task_id='task-test', worker='worker-test', kind='error',
        summary='Model requests of this run are not recorded: the recording proxy was unavailable.')


@pytest.mark.parametrize('task_id', [None, 'task-test'])
def test_bypass_activity_failure_does_not_block_launch(tmp_path, monkeypatch, caplog, task_id, launch):
    owner = control.ProxyControl(tmp_path, '', enabled=True, include_oauth=True)
    monkeypatch.setattr(control, '_control', owner)
    monkeypatch.setattr(type(launch.runtime), 'llmrec_upstream', lambda self, launch: 'https://api.anthropic.com')
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=None))
    monkeypatch.setattr(owner, '_start', AsyncMock(return_value=False))
    insert = AsyncMock(side_effect=RuntimeError('sensitive database error'))
    monkeypatch.setattr(activity, 'insert_activity', insert)
    attached = asyncio.run(control.attach(launch, agent_id='worker-test', task_id=task_id))
    assert attached.llmrec.reason is SkipReason.PROXY_UNAVAILABLE
    assert insert.await_count == int(task_id is not None)
    assert 'sensitive database error' not in caplog.text
