"""DSH measurement contracts; every model response is synthetic and loopback-only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from evals import dsh_density as density


@pytest.mark.parametrize('arguments', [
    ['--real'], ['--total-cap-usd', '.1'],
    ['--real', '--total-cap-usd', 'nan'], ['--real', '--total-cap-usd', '0'],
    ['--memory-budget-mib', 'inf'], ['--sample-interval', '-1'],
    ['--idle-settle', 'nan'], ['--timeout', '0'],
    ['--idle-sessions', '129'], ['--workers', '0'], ['--steps', '1'],
])
def test_invalid_measurement_options_fail_before_starting(arguments):
    with pytest.raises(SystemExit) as error:
        density.parse_args(arguments)
    assert error.value.code == 2


def test_real_calls_require_both_explicit_options():
    default = density.parse_args([])
    assert not default.real and default.total_cap_usd is None
    enabled = density.parse_args(['--real', '--total-cap-usd', '.25'])
    assert enabled.real and enabled.total_cap_usd == .25


def test_rss_includes_complete_owned_subtrees_with_helpers_separate():
    table = {row.pid: row for row in (
        density.ProcessRow(1, 0, 100, 'dsh'),
        density.ProcessRow(2, 1, 20, 'platform-tool'),
        density.ProcessRow(3, 2, 30, 'tool-child'),
        density.ProcessRow(4, 0, 200, 'second-dsh'),
        density.ProcessRow(5, 0, 999, 'unrelated'),
    )}
    sample = density.tree_sample(table, {1, 4, 404})
    assert sample['dsh_rss_bytes'] == 300
    assert sample['helper_rss_bytes'] == 50
    assert sample['tree_rss_bytes'] == 350
    assert {row['pid'] for row in sample['processes']} == {1, 2, 3, 4}
    assert {row['name'] for row in sample['processes'] if row['category'] != 'dsh'} == {
        'platform-tool', 'tool-child'}


@pytest.mark.parametrize('mode,budget,peak,workers,base,expected', [
    ('cli', 1000, 400, 4, 0, 10),
    ('runner', 99, 140, 4, 100, 0),
    ('runner', 429, 140, 4, 100, 32),
    ('runner', 530, 140, 4, 100, 33),
    ('runner', 10_000, 140, 4, 100, 128),
    ('runner', 10_000, 100, 4, 100, None),
    ('runner', 10_000, 90, 4, 100, None),
])
def test_worker_projection_respects_budget_and_runner_member_limits(mode, budget, peak, workers, base, expected):
    result = density.worker_capacity(mode, budget, peak, workers, base)
    assert result['estimated_workers'] == expected
    assert result['measured_workers'] == workers
    assert 'projection' in result['assumption']


def test_private_environment_excludes_ambient_credentials_and_proxies(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'HTTPS_PROXY', 'NODE_OPTIONS'):
        monkeypatch.setenv(name, 'ambient-secret-sentinel')
    environment = density.isolated_env(tmp_path, 12345, '/private-node/bin/node')
    assert 'ambient-secret-sentinel' not in json.dumps(environment)
    assert environment[density.dsh.KEY_ENV] == density.KEY
    assert environment['LOCAL_MESSAGES_PORT'] == '12345'
    for key, value in density.dsh.DshHome(tmp_path / 'private', tmp_path).paths.items():
        assert environment[key] == value
        assert Path(value).is_dir()
    guard = Path(environment['NODE_OPTIONS'].removeprefix('--import=file://'))
    assert guard.read_text() == density.NETWORK_GUARD


def test_cli_and_runner_share_inputs_without_catalog_mcp_or_web(tmp_path):
    executable = tmp_path / 'fake-dsh'
    executable.write_text('#!/bin/sh\nexit 1\n')
    executable.chmod(0o700)
    endpoint = 'http://127.0.0.1:12345'
    args = density.parse_args(['--model', 'deepseek-v4-pro', '--effort', 'low'])
    launch = density.launch_for(str(executable), endpoint, args.model, args.effort)
    command = density.cli_command(tmp_path / 'cli', str(executable), launch)
    patch = Path(command[command.index('--patch') + 1])
    overlay = json.loads(patch.read_text())
    disabled = {row['id'] for row in overlay if row.get('disabled') is True}
    assert {'web', 'web-fetch-http', 'tool-web', 'skill', 'skill-filesystem', 'tool-skill'} <= disabled
    assert not launch.library.items and launch.library.mcp_config is None
    configs = {row['id']: row['config'] for row in overlay if 'config' in row}
    assert configs['agent-default-model']['model'] == args.model
    assert configs['llm-deepseek']['reasoningEffort'] == args.effort
    assert configs['llm-deepseek']['baseURL'] == endpoint
    bootstrap = patch.parent / 'looper-bootstrap.mjs'
    options = json.loads(bootstrap.read_text().split('await createBootstrap(', 1)[1].rsplit(');', 1)[0])
    prompt = density.dsh._prompt_text(density.SYSTEM, launch)
    assert options['text'] == prompt and options['mcp'] == []
    prompt_file = tmp_path / 'runner-system.md'
    prompt_file.write_text(prompt)
    descriptor = density.descriptor(prompt_file, args, endpoint)
    assert descriptor.model == args.model and descriptor.effort == args.effort
    assert descriptor.base_url == endpoint and descriptor.system_prompt_mode == 'append'
    assert Path(descriptor.system_prompt_file).read_text() == options['text']


def test_idle_session_33_opens_second_real_pool_process_without_prompt(tmp_path, monkeypatch):
    """Run production lease/session logic over the existing stdlib protocol fixture."""
    peer = density.ROOT / 'tests/test_runner_supervisor.py'
    monkeypatch.setattr(density.dsh_command, 'command', lambda *unused: [
        sys.executable, str(peer), json.dumps({'kind': 'dsh', 'cap': 32})])
    async def exercise():
        (tmp_path / 'runner-system.md').write_text(density.SYSTEM)
        args = density.parse_args([])
        sessions = []
        async with density.Sampler(.05) as sampler:
            pool = density.RunnerPool(tmp_path / 'runtime', sys.executable, sys.executable, {}, sampler)
            try:
                for index in range(33):
                    sessions.append(await density.runner_step(pool, tmp_path, args,
                        'http://127.0.0.1:1', {}, index, idle=True))
                assert len({session.runner_id for session in sessions[:32]}) == 1
                assert sessions[32].runner_id != sessions[0].runner_id
                assert len(pool.spawns) == 2
                sample = await sampler.sample()
                assert sample['dsh_rss_bytes'] > 0
                assert len([row for row in sample['processes'] if row['category'] == 'dsh']) == 2
                methods = [json.loads(line)['method'] for path in
                    (tmp_path / 'runtime/runners').glob('*/requests.jsonl') for line in path.read_text().splitlines()]
                assert methods.count('session/new') == 33
                assert 'session/prompt' not in methods
            finally:
                await asyncio.gather(*(session.stop() for session in sessions))
                await pool.stop()
            assert all(session._lease is None for session in sessions)
            assert not sampler.roots & density.process_table().keys()
    asyncio.run(asyncio.wait_for(exercise(), 40))


@pytest.mark.parametrize('during_spawn', [False, True], ids=['step-timeout', 'cancelled-spawn'])
def test_cli_timeout_and_spawn_cancellation_reap_process_tree(tmp_path, monkeypatch, during_spawn):
    pid_file = tmp_path / 'helper-pid'
    code = ('import subprocess,sys,time; from pathlib import Path; '
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
            f'Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(60)')
    (tmp_path.parent / 'workspace').mkdir(exist_ok=True)
    monkeypatch.setattr(density, 'cli_command', lambda *unused, **kwargs: [sys.executable, '-c', code])
    original = density.proc_tree.spawn
    processes = []
    async def exercise():
        release = asyncio.Event()
        async def spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            if during_spawn:
                await release.wait()
            return process
        monkeypatch.setattr(density.proc_tree, 'spawn', spawn)
        async with density.Sampler(.05) as sampler:
            task = asyncio.create_task(density.cli_step(tmp_path, density.parse_args([]),
                sys.executable, None, {'PATH': os.defpath}, sampler, 0))
            try:
                async with asyncio.timeout(5):
                    while not pid_file.exists():
                        await asyncio.sleep(.01)
                helper_pid = int(pid_file.read_text())
                if during_spawn:
                    task.cancel()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(task, .05)
                assert all(process.returncode is not None for process in processes)
                assert helper_pid not in density.process_table()
                assert not sampler.roots
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(*(density.proc_tree.end(process, label='density test cleanup')
                                       for process in processes))
    asyncio.run(asyncio.wait_for(exercise(), 15))


@pytest.mark.installed
def test_offline_node_guard_blocks_fetch_tcp_and_dns_before_network(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Installed Node is required for the DSH network guard')
    environment = density.isolated_env(tmp_path, 12345, node)
    # Replace underlying transports before loading the real guard. Even a broken
    # guard can only reach these local stubs, never an external socket or resolver.
    transport = tmp_path / 'fake-transport.mjs'
    transport.write_text('''import net from 'node:net'; import dns from 'node:dns';
net.Socket.prototype.connect = () => { throw Error('underlying socket reached'); };
dns.lookup = () => { throw Error('underlying resolver reached'); };
globalThis.fetch = async () => new Response('synthetic transport');
''')
    script = '''import net from 'node:net'; import dns from 'node:dns';
const allowed = await (await fetch('http://127.0.0.1:12345/v1/messages')).text();
const refusals = [];
for (const call of [() => fetch('https://outside.invalid/v1/messages'),
  () => new net.Socket().connect({host: '192.0.2.1', port: 443}),
  () => dns.lookup('outside.invalid', () => {})]) {
  try { await call(); } catch (error) { refusals.push(error.message); }
}
console.log(JSON.stringify({allowed, refusals}));
'''
    completed = subprocess.run([node, '--import', transport.as_uri(), '--import',
        (tmp_path / 'network-guard.mjs').as_uri(), '--input-type=module', '-e', script],
        cwd=tmp_path, env={**environment, 'NODE_OPTIONS': ''}, capture_output=True,
        text=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result['allowed'] == 'synthetic transport'
    assert len(result['refusals']) == 3
    assert all('Offline DSH test refused' in message for message in result['refusals'])


def test_real_option_gateway_caps_only_loopback_synthetic_requests(monkeypatch):
    # Exercise the optional forwarding path with a fake upstream, never DeepSeek.
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-gateway-key')
    args = density.parse_args(['--real', '--total-cap-usd', '.0028'])
    with density.MessagesServer([['density-ok'], ['density-ok']]) as upstream:
        monkeypatch.setattr(density.dsh, 'DEFAULT_BASE_URL', upstream.url)
        with density.real_server(args) as (port, spending):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request():
                body = {'model': args.model, 'max_tokens': 999999, 'stream': True,
                        'messages': [{'role': 'user', 'content': 'synthetic'}]}
                return urllib.request.Request(f'http://127.0.0.1:{port}/v1/messages',
                    data=json.dumps(body).encode(), headers={'content-type': 'application/json'})
            for _ in range(2):
                with opener.open(request(), timeout=5) as response:
                    assert b'event: message_stop' in response.read()
            with pytest.raises(urllib.error.HTTPError) as rejected:
                opener.open(request(), timeout=5)
            assert rejected.value.code == 402
            assert spending['requests'] == 2 and spending['reserved_usd'] <= args.total_cap_usd
        assert len(upstream.requests) == 2
        assert all(row['body']['max_tokens'] == 1024 for row in upstream.requests)
        assert all(row['api_key'] == 'synthetic-gateway-key' for row in upstream.requests)


@pytest.mark.installed
def test_installed_dsh_density_uses_only_fake_messages(tmp_path, capsys, monkeypatch):
    executable = os.environ.get('LOOPER_TEST_DSH_DENSITY_EXECUTABLE')
    if not executable:
        pytest.skip('Set LOOPER_TEST_DSH_DENSITY_EXECUTABLE for installed DSH/fake Messages measurement')
    assert shutil.which(executable), 'Requested DSH executable is unavailable'
    servers = []
    original = density.MessagesServer
    class CapturingServer(original):
        def __init__(self, replies):
            super().__init__(replies)
            servers.append(self)
    monkeypatch.setattr(density, 'MessagesServer', CapturingServer)
    output = tmp_path / 'density.json'
    assert density.main(['--dsh', executable, '--steps', '1', '--workers', '1',
                         '--idle-sessions', '33', '--out', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['fake_requests'] == 4 and report['idle_model_requests'] == 0
    assert {row['mode'] for row in report['modes']} == {'cli', 'runner'}
    for mode in report['modes']:
        assert len(mode['steps']) == 2
        assert mode['active_memory']['peak_tree_rss_bytes'] > 0
        assert len(mode['idle_checkpoints']) == 33
    runner = next(row for row in report['modes'] if row['mode'] == 'runner')
    assert runner['session_33_second_member'] is True
    assert runner['steps'][0]['spawn_to_first_event_ms'] > 0
    assert runner['steps'][1]['spawn_to_first_event_ms'] is None
    assert 'runner' in capsys.readouterr().out
    bodies = [request['body'] for request in servers[0].requests]
    assert all({tool['name'] for tool in body['tools']} == {'read', 'glob', 'grep'} for body in bodies)
    for field in ('model', 'thinking', 'output_config', 'system', 'messages', 'tools'):
        assert all(body.get(field) == bodies[0].get(field) for body in bodies[1:]), f'CLI/runner differ on {field}'
