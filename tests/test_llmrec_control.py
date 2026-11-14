"""Recording failures remain visible without preventing a run from launching."""

import asyncio
import logging
import json
import signal
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.keeper import write_json
from looper.agents.llmrec import STATE_FILE, TLS_ENV, LlmrecSkip, RouteFile, SkipReason
from looper.agents.llmrec import control
from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.db import activity
from tests.runtime_model_helpers import resolved_profile
from tests.test_claude_code_gateway import translated_launch


@pytest.fixture
def launch():
    provider = LegacyProviderSpec('test-provider', 'Test provider', AuthKind.API_KEY,
                                  frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                                  base_url='https://model.example.invalid', api_key='test-key')
    profile = resolved_profile('test-agent', 'Test agent', RuntimeSpec('test-runtime', 'Claude Code', 'claude-code'),
                               provider, 'haiku')
    return Launch(AgentSettings(profile=profile))


@pytest.mark.parametrize('enabled', [None, 'false', 'true'])
def test_self_check_without_controller_defaults_recording_on(monkeypatch, enabled):
    from looper.agents import self_check
    from looper.db import system_settings

    monkeypatch.setattr(control, '_control', None)
    monkeypatch.setattr(system_settings, 'get_settings', AsyncMock(
        return_value={} if enabled is None else {'llmrec.enabled': enabled}))
    monkeypatch.setattr(self_check, '_llmrec_storage', AsyncMock(return_value=(0, 2048)))
    check = asyncio.run(self_check.llmrec_check())
    assert check.status == (self_check.SKIP if enabled == 'false' else self_check.FAIL)


@pytest.mark.parametrize('foreign', ['instance', 'pid'])
def test_failed_health_requires_matching_instance_and_pid(tmp_path, monkeypatch, foreign):
    owner = control.ProxyControl(tmp_path, '', enabled=True)
    state = {'pid': 1234, 'instance': 'owned', 'start_time': 'owned-start', 'port': 12345}
    (tmp_path / STATE_FILE).parent.mkdir(parents=True)
    write_json(tmp_path / STATE_FILE, state)
    monkeypatch.setattr(owner, '_running', AsyncMock(return_value=True))
    data = {**state, foreign: 'foreign' if foreign == 'instance' else 4321, 'write_errors': 7}
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json=data))))

    async def run():
        assert await owner._health() is None
        assert owner._failed_health is None
        assert (await owner.status())['proxy']['write_errors'] == 0
        stop = AsyncMock()
        monkeypatch.setattr(owner, '_stop', stop)
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('no spawn')))
        with pytest.raises(OSError, match='no spawn'):
            await owner._start()
        stop.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize('identity', ['matching', 'changed_instance', 'changed_start_time'])
def test_stop_escalates_only_for_owned_process(tmp_path, identity):
    async def run():
        owner = control.ProxyControl(tmp_path, '', enabled=True)
        child = await asyncio.create_subprocess_exec(sys.executable, '-c',
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); signal.pause()",
            stdout=asyncio.subprocess.PIPE)
        try:
            assert await asyncio.wait_for(child.stdout.readline(), 3) == b'ready\n'
            state = {'pid': child.pid, 'instance': 'owned', 'port': 12345,
                     'start_time': await asyncio.to_thread(control.proc_tree._start_time, child.pid)}
            current = {**state}
            if identity == 'changed_instance':
                current['instance'] = 'replacement'
            elif identity == 'changed_start_time':
                state['start_time'] = 'previous-process'
            (tmp_path / STATE_FILE).parent.mkdir(parents=True)
            write_json(tmp_path / STATE_FILE, current)
            await owner._stop(state, grace_s=.05)
            if identity == 'matching':
                assert await asyncio.wait_for(child.wait(), 2) == -signal.SIGKILL
                assert owner._state() is None
            else:
                assert child.returncode is None
                assert owner._state() == current
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()

    asyncio.run(asyncio.wait_for(run(), 10))


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
    monkeypatch.setattr(self_check, '_llmrec_storage', AsyncMock(return_value=(0, 2048)))
    status = asyncio.run(owner.status())
    api_v1.validate_arguments(api_v1.find('POST', '/api/v1/llmrec_settings/get').result['properties']['proxy'],
                              status['proxy'])
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


@pytest.mark.parametrize('change', ['upstream', 'runtime', 'provider', 'model', 'missing_file'])
def test_resident_route_reuses_binding_and_retires_changed_route(tmp_path, monkeypatch, change, launch):
    from dataclasses import replace
    from looper.agents.llmrec import LlmrecRoute, ROUTES_DIR

    original = launch
    owner = control.ProxyControl(tmp_path, '', enabled=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=True))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})

    async def body():
        one = await owner.attach(original, agent_id='manager-test', task_id=None, reuse=True)
        assert isinstance(one.llmrec, LlmrecRoute)
        path = tmp_path / ROUTES_DIR / f'{one.llmrec.id}.json'
        owner.bind(one.llmrec.id, 11)
        owner.release(11, end_route=False)
        two = await owner.attach(original, agent_id='manager-test', task_id=None, reuse=True)
        assert two.llmrec == one.llmrec
        assert two.binding_sha256 == original.binding_sha256
        assert RouteFile.read(path).last_record_id == 11
        assert RouteFile.read(path).ended_at is None

        profile = original.profile
        resolved = profile.resolved_model
        if change == 'upstream':
            resolved = replace(resolved, endpoint=replace(resolved.endpoint, base_url='http://127.0.0.1:2/v1'))
        elif change == 'runtime':
            profile = replace(profile, runtime=replace(profile.runtime, type='api'))
            resolved = replace(resolved, model=replace(resolved.model, protocol=ApiProtocol.OPENAI_RESPONSES),
                               endpoint=replace(resolved.endpoint, protocol=ApiProtocol.OPENAI_RESPONSES))
        elif change == 'provider':
            resolved = replace(resolved, provider=replace(resolved.provider, id='prov-other'),
                               endpoint=replace(resolved.endpoint, provider_id='prov-other'),
                               model=replace(resolved.model, provider_id='prov-other'))
        elif change == 'model':
            resolved = replace(resolved, model=replace(resolved.model, name='gpt-other'))
        else:
            path.unlink()
        changed = replace(original, settings=replace(original.settings,
                          profile=replace(profile, resolved_model=resolved)))
        three = await owner.attach(changed, agent_id='manager-test', task_id=None, reuse=True)
        assert isinstance(three.llmrec, LlmrecRoute)
        assert three.llmrec.id != one.llmrec.id
        if change != 'missing_file':
            assert RouteFile.read(path).ended_at is not None
            assert RouteFile.read(path).last_record_id == 11
            # The old resident can write a last handoff after its replacement was prepared.
            owner.bind(one.llmrec.id, 12)
            owner.release(12, end_route=False)
            assert RouteFile.read(path).ended_at is not None
            assert RouteFile.read(path).last_record_id == 12
        again = await owner.attach(changed, agent_id='manager-test', task_id=None, reuse=True)
        assert again.llmrec == three.llmrec

    asyncio.run(body())


def test_resident_attach_checks_proxy_before_reusing_route(tmp_path, monkeypatch, launch):
    from looper.agents.llmrec import LlmrecRoute

    owner = control.ProxyControl(tmp_path, '', enabled=True)
    health = AsyncMock(return_value=True)
    monkeypatch.setattr(owner, '_health', health)
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})
    monkeypatch.setattr(owner, '_start', AsyncMock(return_value=False))

    async def body():
        one = await owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True)
        health.return_value = None
        bypass = await owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True)
        assert bypass.llmrec == LlmrecSkip(SkipReason.PROXY_UNAVAILABLE)
        assert health.await_args.kwargs == {'cached': False}
        health.return_value = True
        recovered = await owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True)
        assert isinstance(recovered.llmrec, LlmrecRoute)
        assert recovered.llmrec == one.llmrec

    asyncio.run(body())


def test_consult_routes_are_separate_and_end_with_their_records(tmp_path, monkeypatch, launch):
    from looper.agents.llmrec import ROUTES_DIR

    owner = control.ProxyControl(tmp_path, '', enabled=True, include_oauth=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=True))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})

    async def body():
        one, two = await asyncio.gather(*(
            owner.attach(launch, agent_id=agent, task_id='task-test') for agent in ('consult-one', 'consult-two')))
        assert one.llmrec.id != two.llmrec.id
        owner.bind(one.llmrec.id, 11)
        owner.bind(two.llmrec.id, 12)
        owner.release(11, end_route=True)
        first = RouteFile.read(tmp_path / ROUTES_DIR / f'{one.llmrec.id}.json')
        second = RouteFile.read(tmp_path / ROUTES_DIR / f'{two.llmrec.id}.json')
        assert first.ended_at and first.record_id is None and first.last_record_id == 11
        assert second.ended_at is None and second.record_id == 12

    asyncio.run(body())


@pytest.mark.parametrize('source', ['settings', 'provider'])
@pytest.mark.parametrize('cwd', [None, 'worker-alias'])
def test_reused_route_refreshes_proxy_and_tls_without_losing_binding(tmp_path, monkeypatch, source, cwd, launch):
    from dataclasses import replace
    from looper.agents.llmrec import ROUTES_DIR

    owner = control.ProxyControl(tmp_path, '', enabled=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=True))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})
    worker = tmp_path / 'worker'
    worker.mkdir()
    alias = tmp_path / 'worker-alias'
    alias.symlink_to(worker, target_is_directory=True)
    cwd = str(tmp_path / cwd) if cwd else None
    monkeypatch.chdir(tmp_path)
    changes = (('HTTPS_PROXY', 'http://proxy.example.invalid:8080'),
               ('NO_PROXY', 'model.example.invalid'), ('SSL_CERT_FILE', 'updated-ca.pem'))
    settings = launch.settings
    if source == 'settings':
        settings = replace(settings, env=changes)
    else:
        profile = settings.profile
        resolved = profile.resolved_model
        settings = replace(settings, profile=replace(profile, resolved_model=replace(
            resolved, provider=replace(resolved.provider, env=changes))))
    updated = replace(launch, settings=settings)

    async def body():
        one = await owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True, cwd=cwd)
        owner.bind(one.llmrec.id, 11)
        two = await owner.attach(updated, agent_id='manager-test', task_id=None, reuse=True, cwd=cwd)
        assert two.llmrec == one.llmrec
        route = RouteFile.read(tmp_path / ROUTES_DIR / f'{two.llmrec.id}.json')
        assert route.proxy_env['HTTPS_PROXY'] == dict(changes)['HTTPS_PROXY']
        assert route.proxy_env['NO_PROXY'] == dict(changes)['NO_PROXY']
        assert route.tls_env == {'SSL_CERT_FILE': str((worker if cwd else tmp_path) / 'updated-ca.pem')}
        assert route.record_id == 11 and route.last_record_id is None and route.ended_at is None
        fresh = await owner.attach(updated, agent_id='worker-test', task_id=None, cwd=cwd)
        assert RouteFile.read(tmp_path / ROUTES_DIR / f'{fresh.llmrec.id}.json').tls_env == route.tls_env

    asyncio.run(body())


@pytest.mark.parametrize('enabled', [False, True])
def test_translation_route_exists_even_when_recording_is_off(tmp_path, monkeypatch, enabled, translated_launch):
    from looper.agents.llmrec import ROUTES_DIR, ROUTE_FORMAT, RouteKind

    launch = translated_launch
    owner = control.ProxyControl(tmp_path, '', enabled=enabled)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value={
        'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 12345})
    attached = asyncio.run(owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True))
    route = RouteFile.read(tmp_path / ROUTES_DIR / f'{attached.llmrec.id}.json')
    assert route.local_token and route.local_token == attached.llmrec.local_token
    assert 'synthetic-key' not in (tmp_path / ROUTES_DIR / f'{route.id}.json').read_text()
    assert route.kind == RouteKind.TRANSLATE
    assert route.record is enabled
    assert route.models == {'chat-main': 'chat-main', 'chat-small': 'chat-small'}
    assert route.dialect == 'generic'
    assert attached.environment()['ANTHROPIC_BASE_URL'] == attached.llmrec.url
    assert attached.binding_sha256 == launch.binding_sha256
    assert asyncio.run(owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True)).llmrec == attached.llmrec


@pytest.mark.parametrize('failure', ['start_false', 'start_error', 'missing_state', 'route_write'])
def test_translation_failure_is_fail_closed_and_never_recording_bypass(tmp_path, monkeypatch, failure, translated_launch):
    from looper.agents.llmrec import ROUTE_FORMAT
    from looper.agents.runtimes import RuntimeUnsupported

    launch = translated_launch
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(control, '_control', owner)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=None if failure.startswith('start') else {
        'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}))
    monkeypatch.setattr(owner, '_start', AsyncMock(return_value=False,
        side_effect=OSError('sensitive error') if failure == 'start_error' else None))
    monkeypatch.setattr(owner, '_state', lambda: None if failure == 'missing_state' else {'port': 12345})
    if failure == 'route_write':
        monkeypatch.setattr(RouteFile, 'write', lambda *args: (_ for _ in ()).throw(OSError('sensitive error')))
    insert = AsyncMock()
    monkeypatch.setattr(activity, 'insert_activity', insert)
    attached = asyncio.run(control.attach(launch, agent_id='worker-test', task_id='task-test'))
    assert attached.llmrec.reason is SkipReason.GATEWAY_UNAVAILABLE
    with pytest.raises(RuntimeUnsupported, match='gateway could not start'):
        attached.environment()
    insert.assert_awaited_once_with(task_id='task-test', worker='worker-test', kind='error',
        summary='This run cannot start: the local gateway is unavailable.')


def test_translated_launch_waits_for_cold_proxy_startup(tmp_path, monkeypatch, translated_launch):  # noqa: F811
    from looper.agents.llmrec import LlmrecRoute

    boot = control.boot_argv

    def delayed_boot(*args):
        argv = boot(*args)
        index = argv.index('-c') + 1
        argv[index] = 'import time; time.sleep(3.2); ' + argv[index]
        return argv

    monkeypatch.setattr(control, 'boot_argv', delayed_boot)

    async def body():
        owner = control.ProxyControl(tmp_path, 'postgresql://127.0.0.1:1/disposable', enabled=False)
        try:
            launch = await owner.attach(translated_launch, agent_id='worker-test', task_id=None)
            assert isinstance(launch.llmrec, LlmrecRoute)
            assert launch.environment()['ANTHROPIC_AUTH_TOKEN'] == launch.llmrec.local_token
        finally:
            for child in list(owner._children.values()):
                if child.returncode is None:
                    child.terminate()
                await asyncio.wait_for(child.wait(), 5)

    asyncio.run(asyncio.wait_for(body(), 20))


@pytest.mark.parametrize('features,format', [({}, 5), ({'translate': 0}, 5), ({'translate': 1}, 4)])
def test_translation_refuses_outdated_proxy(tmp_path, monkeypatch, features, format, translated_launch):

    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value={'route_format': format, 'features': features}))
    attached = asyncio.run(owner.attach(translated_launch, agent_id='worker-test', task_id=None))
    assert attached.llmrec.reason is SkipReason.PROXY_OUTDATED


@pytest.mark.parametrize('change', ['dialect', 'background_model', 'provider', 'upstream', 'model'])
def test_translated_resident_route_changes_with_effective_model(tmp_path, monkeypatch, change, translated_launch):
    from dataclasses import replace
    from looper.agents.llmrec import ROUTE_FORMAT, ROUTES_DIR

    launch = translated_launch
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value={
        'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 12345})
    first = asyncio.run(owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True))
    resolved = launch.profile.resolved_model
    if change == 'dialect':
        resolved = replace(resolved, model=replace(resolved.model, chat_dialect='deepseek'))
    elif change == 'background_model':
        resolved = replace(resolved, model=replace(resolved.model, background_model='other-small'))
    elif change == 'provider':
        provider = replace(resolved.provider, id='prov-other')
        resolved = replace(resolved, provider=provider, model=replace(resolved.model, provider_id=provider.id),
                           endpoint=replace(resolved.endpoint, provider_id=provider.id))
    elif change == 'upstream':
        resolved = replace(resolved, endpoint=replace(resolved.endpoint, base_url='https://other.example.invalid'))
    else:
        resolved = replace(resolved, model=replace(resolved.model, name='other-model'))
    changed = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, resolved_model=resolved)))
    second = asyncio.run(owner.attach(changed, agent_id='manager-test', task_id=None, reuse=True))
    assert second.llmrec.id != first.llmrec.id
    assert second.llmrec.local_token != first.llmrec.local_token
    assert second.binding_sha256 != first.binding_sha256
    assert RouteFile.read(tmp_path / ROUTES_DIR / f'{first.llmrec.id}.json').ended_at


def test_translation_starts_proxy_on_demand_with_recording_off(tmp_path, monkeypatch, translated_launch):
    from looper.agents.llmrec import ROUTE_FORMAT

    owner = control.ProxyControl(tmp_path, '', enabled=False)
    health = {'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}
    monkeypatch.setattr(owner, '_health', AsyncMock(side_effect=[None, health]))
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(owner, '_start', start)
    monkeypatch.setattr(owner, '_state', lambda: {'port': 12345})
    attached = asyncio.run(owner.attach(translated_launch, agent_id='worker-test', task_id=None))
    start.assert_awaited_once()
    assert attached.llmrec.record is False
    assert attached.record()['llmrec']['state'] == 'translating'


@pytest.mark.parametrize('upstream', ['https://user:password@chat.example.invalid/v1',
    'https://chat.example.invalid/v1?api_key=secret', 'https://chat.example.invalid/v1?token=secret'])
def test_translation_rejects_url_credentials_before_proxy_start(tmp_path, monkeypatch, translated_launch, upstream):
    from dataclasses import replace

    launch = translated_launch
    monkeypatch.setattr(type(launch.runtime), 'llmrec_upstream', lambda self, launch: upstream)
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    start = AsyncMock(side_effect=AssertionError('credentials must be rejected before startup'))
    monkeypatch.setattr(owner, '_start', start)
    attached = asyncio.run(owner.attach(launch, agent_id='worker-test', task_id=None))
    assert attached.llmrec.reason is SkipReason.CREDENTIALS_IN_URL
    start.assert_not_awaited()
    assert 'secret' not in attached.record()['llmrec']['detail']


def test_translated_resident_record_toggle_updates_existing_route(tmp_path, monkeypatch, translated_launch):
    from looper.agents.llmrec import ROUTE_FORMAT, ROUTES_DIR

    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value={
        'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 12345})
    first = asyncio.run(owner.attach(translated_launch, agent_id='manager-test', task_id=None, reuse=True))
    owner.enabled = True
    second = asyncio.run(owner.attach(translated_launch, agent_id='manager-test', task_id=None, reuse=True))
    assert second.llmrec.id == first.llmrec.id
    assert second.llmrec.record is True
    assert RouteFile.read(tmp_path / ROUTES_DIR / f'{first.llmrec.id}.json').record is True


def test_translation_proxy_dies_after_start_is_unavailable(tmp_path, monkeypatch, translated_launch):
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=None))
    monkeypatch.setattr(owner, '_start', AsyncMock(return_value=True))
    attached = asyncio.run(owner.attach(translated_launch, agent_id='worker-test', task_id=None))
    assert attached.llmrec.reason is SkipReason.GATEWAY_UNAVAILABLE


def test_translation_routes_context_suffixed_main_and_background_models(tmp_path, monkeypatch, translated_launch):
    from dataclasses import replace
    from looper.agents.llmrec import ROUTE_FORMAT, ROUTES_DIR
    from looper.agents.llmrec.dialects import DIALECTS, ChatDialect
    from looper.agents.llmrec.translate import to_chat

    resolved = translated_launch.profile.resolved_model
    profile = replace(translated_launch.profile, resolved_model=replace(resolved,
        model=replace(resolved.model, name='chat-main[1m]', background_model='chat-small[1m]')))
    launch = replace(translated_launch, settings=replace(translated_launch.settings, profile=profile))
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value={
        'route_format': ROUTE_FORMAT, 'features': {'translate': 1}}))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 12345})
    attached = asyncio.run(owner.attach(launch, agent_id='worker-test', task_id=None))
    route = RouteFile.read(tmp_path / ROUTES_DIR / f'{attached.llmrec.id}.json')
    for name in ('chat-main', 'chat-small'):
        request = to_chat({'model': name, 'messages': [{'role': 'user', 'content': 'hello'}]},
                          DIALECTS[ChatDialect.GENERIC], route.models, session_id=None)
        assert json.loads(request.body)['model'] == name


@pytest.mark.parametrize('available', [True, False])
def test_first_translated_request_waits_for_registry_connection(tmp_path, monkeypatch, available):
    from types import SimpleNamespace
    from looper.agents.llmrec import __main__ as process, ROUTES_DIR
    from tests.fake_model_api import FakeModelAPI, ResponseScript

    async def body():
        connecting, connect, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pool = SimpleNamespace(close=AsyncMock(), fetchrow=AsyncMock())

        async def init_pool(*args, **kwargs):
            connecting.set()
            await connect.wait()
            return pool

        monkeypatch.setattr(process, 'init_pool', init_pool)
        stop_handlers = {}
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler',
                            lambda sig, callback: stop_handlers.setdefault(sig, callback))
        base_proxy = process.proxy.Proxy
        proxies = []

        class ObservedProxy(base_proxy):
            async def start(self):
                port = await super().start()
                proxies.append(self)
                return port

            async def _translated(self, *args, **kwargs):
                requested.set()
                return await super()._translated(*args, **kwargs)

        monkeypatch.setattr(process.proxy, 'Proxy', ObservedProxy)
        script = ResponseScript(events=[{'id': 'chat-test', 'choices': [
            {'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}, '[DONE]'])
        async with FakeModelAPI([script]) as upstream:
            pool.fetchrow.return_value = dict(base_url=upstream.base_url, api_key='test-key',
                                             enabled=True, auth='api_key')
            route = RouteFile(id='a' * 22, kind='translate', record=False, local_token='local-token',
                provider_id='test-provider', upstream=upstream.base_url, api='openai-chat', dialect='generic',
                models={'client': 'upstream'}, created_at='2026-01-01T00:00:00+00:00', agent_id='test')
            route.write(tmp_path / ROUTES_DIR / f'{route.id}.json')
            task = asyncio.create_task(process._serve(SimpleNamespace(
                runtime_root=tmp_path, port=0, check_interval=60), 'disposable'))
            try:
                await connecting.wait()
                async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                    base = f'http://127.0.0.1:{proxies[0].port}'
                    # Recording health stays available while the registry connects.
                    assert (await client.get(base + '/llmrec/health')).status_code == 200
                    request = asyncio.create_task(client.post(base + f'/r/{route.id}/v1/messages',
                        headers={'authorization': 'Bearer local-token'},
                        json={'model': 'client', 'messages': [], 'stream': True}))
                    await requested.wait()
                    done, _ = await asyncio.wait({request}, timeout=.05)
                    assert not done, 'translation refused before the registry could finish connecting'
                    if available:
                        connect.set()
                        assert (await request).status_code == 200
                        assert upstream.requests[0].headers['authorization'] == 'Bearer test-key'
                    else:
                        assert (await request).status_code == 503
                        assert not upstream.requests
            finally:
                connect.set()
                stop_handlers[signal.SIGTERM]()
                await asyncio.wait_for(task, 3)

    asyncio.run(asyncio.wait_for(body(), 10))


def test_proxy_recovers_writer_without_replacing_endpoint(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from looper.agents.llmrec import __main__ as process, ROUTES_DIR
    from tests.fake_model_api import FakeModelAPI, ResponseScript

    async def body():
        started, failed, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        failed_on_shutdown = asyncio.Event()
        pool = SimpleNamespace(close=AsyncMock())
        monkeypatch.setattr(process, 'init_pool', AsyncMock(return_value=pool))
        stop_handlers = {}
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler',
                            lambda sig, callback: stop_handlers.setdefault(sig, callback))
        base_proxy = process.proxy.Proxy
        proxies, written = [], []

        class ObservedProxy(base_proxy):
            async def start(self):
                port = await super().start()
                proxies.append(self)
                started.set()
                return port

        async def write_batch(captures, *, pool):
            if not failed.is_set():
                failed.set()
                raise RuntimeError('sensitive database error')
            if captures[0].seq == 3 and not failed_on_shutdown.is_set():
                failed_on_shutdown.set()
                raise RuntimeError('sensitive database error')
            written.extend(capture.seq for capture in captures)
            recovered.set()

        monkeypatch.setattr(process.proxy, 'Proxy', ObservedProxy)
        monkeypatch.setattr(process.llm_requests, 'write_batch', write_batch)
        script = [{'type': 'message_start', 'message': {'content': []}}, {'type': 'message_stop'}]
        async with FakeModelAPI([ResponseScript(events=script) for _ in range(3)]) as upstream:
            route = RouteFile(id='a' * 22, upstream=upstream.base_url, agent_id='worker-test',
                              created_at='2026-01-01T00:00:00+00:00')
            route.write(tmp_path / ROUTES_DIR / f'{route.id}.json')
            task = asyncio.create_task(process._serve(SimpleNamespace(
                runtime_root=tmp_path, port=0, check_interval=60), 'disposable'))
            try:
                await started.wait()
                server = proxies[0]
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    base = f'http://127.0.0.1:{server.port}'
                    before = await client.get(base + '/llmrec/health')
                    assert before.status_code == 200
                    url = base + f'/r/{route.id}/messages'
                    headers = {'authorization': 'Bearer test-key'}
                    assert (await client.post(url, headers=headers, json={'model': 'test'})).status_code == 200
                    await failed.wait()
                    health = await client.get(base + '/llmrec/health')
                    assert health.status_code == 200
                    assert (await client.post(url, headers=headers, json={'model': 'test'})).status_code == 200
                    await recovered.wait()
                    while server.sink.pending:
                        await asyncio.sleep(.01)
                    after = (await client.get(base + '/llmrec/health')).json()
                    assert (after['pid'], after['instance']) == (before.json()['pid'], before.json()['instance'])
                    assert written == [1, 2] and after['recorded'] == 2
                    assert (await client.post(url, headers=headers, json={'model': 'test'})).status_code == 200
                    await failed_on_shutdown.wait()
                    # Stop during another retry backoff: shutdown must still drain.
                    assert server.sink.pending == 1
                    stop_handlers[signal.SIGTERM]()
                await task
                assert written == [1, 2, 3]
                assert server.sink.pending == 0 and server.sink.recorded == 3
                assert server.sink.write_errors == 2
            finally:
                stop_handlers[signal.SIGTERM]()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)

    asyncio.run(asyncio.wait_for(body(), 8))


@pytest.mark.parametrize('transition', ['promote', 'cancel', 'end'])
def test_resident_route_keeps_live_record_until_release(tmp_path, monkeypatch, launch, transition):
    from looper.agents.llmrec import ROUTES_DIR

    owner = control.ProxyControl(tmp_path, '', enabled=True)
    monkeypatch.setattr(owner, '_health', AsyncMock(return_value=True))
    monkeypatch.setattr(owner, '_state', lambda: {'port': 1234})
    attached = asyncio.run(owner.attach(launch, agent_id='manager-test', task_id=None, reuse=True))
    path = tmp_path / ROUTES_DIR / f'{attached.llmrec.id}.json'
    owner.bind(attached.llmrec.id, 11)
    owner.bind(attached.llmrec.id, 12)
    assert RouteFile.read(path).record_id == 11
    owner.bind(attached.llmrec.id, 11)  # Rebinding the live turn must keep the pending turn.
    assert RouteFile.read(path).record_id == 11
    if transition == 'cancel':
        owner.release(12, end_route=False)
        assert RouteFile.read(path).record_id == 11
    owner.release(11, end_route=transition == 'end')
    route = RouteFile.read(path)
    assert route.record_id == (12 if transition == 'promote' else None)
    assert route.last_record_id == 11
    assert bool(route.ended_at) == (transition == 'end')
    if transition == 'promote':
        owner.release(11, end_route=False)  # A stale release cannot clear the next turn.
        assert RouteFile.read(path).record_id == 12
        owner.release(12, end_route=False)
        route = RouteFile.read(path)
        assert route.record_id is None and route.last_record_id == 12
