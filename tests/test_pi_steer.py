"""Pi RPC startup and lifecycle through real pipes, in the Worker's call order."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents.runtimes import Capability, StreamChannel
from looper.agents.runtimes import pi_steer
from looper.agents.runtimes.pi_steer import PiChannel
from tests.test_pi_runtime import PI, _launch, _provider


_RPC = r"""
import json, os, select, sys, time

scenario = sys.argv[1]
record_path = sys.argv[2]
sys.stdin = open(0, 'rb', buffering=0, closefd=False)

def record(value):
    with open(record_path, 'a') as out:
        out.write(json.dumps(value) + '\n')

def read():
    request = json.loads(sys.stdin.readline())
    record(request)
    return request

def emit(value):
    print(json.dumps(value), flush=True)

def ack(request, **fields):
    response = dict(type='response', id=request['id'], command=request['type'], success=True)
    response.update(fields)
    emit(response)

def assistant(text, stop='stop'):
    emit(dict(type='message_end', message=dict(role='assistant', model='test-model',
        stopReason=stop, content=[dict(type='text', text=text)], usage=dict(input=7, output=3))))

def finish():
    assistant('final\u2028answer')
    emit(dict(type='agent_settled', aborted=False))

if scenario == 'early_exit':
    sys.exit(7)
requests = [read(), read()]
assert {r['type'] for r in requests} == {'get_state', 'set_steering_mode'}
assert next(r for r in requests if r['type'] == 'set_steering_mode')['mode'] == 'one-at-a-time'
if scenario == 'setup_timeout':
    sys.stdin.read()
    sys.exit(0)
if scenario == 'setup_noise_timeout':
    while True:
        if select.select([sys.stdin], [], [], 0)[0]:
            assert sys.stdin.read() == b''
            sys.exit(0)
        emit(dict(type='notice'))
        time.sleep(0.005)
if scenario == 'reverse':
    requests.reverse()
for index, request in enumerate(requests):
    if scenario == 'fail_' + request['type']:
        ack(request, success=False, error=request['type'] + ' rejected')
    elif request['type'] == 'get_state':
        session_id = {'missing_session': None, 'empty_session': '', 'invalid_session': 42}.get(
            scenario, 'rpc-session')
        ack(request, data={} if session_id is None else dict(sessionId=session_id))
    else:
        ack(request)
    record(dict(acked=request['type']))
    if scenario == 'fail_' + request['type'] or (request['type'] == 'get_state' and scenario.endswith('_session')):
        assert sys.stdin.read() == b''
        sys.exit(0)
    if index == 0:
        # A model prompt must never arrive after only one setup acknowledgement.
        assert not select.select([sys.stdin], [], [], 0.03)[0]

request = read()
assert request['type'] == 'prompt'
if scenario == 'prompt_eof':
    sys.exit(3)
if scenario == 'prompt_timeout':
    sys.stdin.read()
    sys.exit(0)
if scenario == 'fast_timeout':
    finish()
    sys.stdin.read()
    sys.exit(0)
if scenario in ('fast', 'fast_rejected'):
    finish()
if scenario == 'flood':
    for _ in range(128):
        emit(dict(type='notice', payload='x' * 16384))
if scenario in ('handled', 'queued', 'missing_disposition', 'fail_prompt', 'fast_rejected'):
    ack(request, success=scenario not in ('fail_prompt', 'fast_rejected'), error='prompt rejected',
        data={} if scenario == 'missing_disposition' else dict(disposition=scenario))
    sys.stdin.read()
    sys.exit(0)
# Unmatched and duplicate IDs must not acknowledge the outstanding prompt.
ack(dict(id='unknown', type='prompt'), success=False)
ack(requests[0])
ack(request, data=dict(disposition='started'))
if scenario == 'extension_error':
    emit(dict(type='extension_error', error='extension failed'))
elif scenario == 'invalid_json':
    print('not JSON SECRET-test', flush=True)
elif scenario == 'invalid_utf8':
    sys.stdout.buffer.write(b'\xff\n')
    sys.stdout.buffer.flush()
elif scenario == 'non_object':
    emit([])
elif scenario == 'parse_error':
    emit(dict(type='response', command='parse', success=False, error='bad command'))
elif scenario == 'run_eof':
    assistant('unfinished')
    emit(dict(type='agent_end'))
    sys.exit(2)
elif scenario == 'retry':
    assistant('retrying', 'error')
    emit(dict(type='agent_end'))
    emit(dict(type='auto_retry_start'))
    # Wait for the test to prove no result has been emitted at agent_end.
    record(dict(waiting_for_retry=True))
    while not os.path.exists(record_path + '.continue'):
        time.sleep(0.005)
    finish()
elif scenario != 'fast':
    finish()
assert sys.stdin.read() == b''
record(dict(stdin_closed=True))
"""


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / 'requests.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _run(tmp_path: Path, scenario: str) -> list[dict[str, Any]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-u', '-c', _RPC, scenario, str(tmp_path / 'requests.jsonl'),
        cwd=tmp_path, env={**os.environ, 'HOME': str(tmp_path)},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    channel = PiChannel(process, model='test-model', cwd=str(tmp_path))
    events: list[dict[str, Any]] = []

    async def consume() -> None:
        # Exactly the Worker order: start must return before any stdout consumption.
        await asyncio.wait_for(channel.start('model prompt'), 1)
        async for event in channel.events():
            events.append(event)
            if scenario == 'retry' and event.get('event', {}).get('type') == 'auto_retry_start':
                assert not any(e['type'] == 'result' for e in events)
                (tmp_path / 'requests.jsonl.continue').touch()
            if event['type'] == 'result':
                channel.end_input()
                channel.end_input()

    try:
        await asyncio.wait_for(consume(), 4)
        await asyncio.wait_for(process.wait(), 1)
        assert process.stderr is not None
        assert await process.stderr.read() == b''
        return events
    finally:
        channel.end_input()
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.parametrize('scenario', ['normal', 'reverse', 'fast', 'retry', 'flood'])
def test_start_then_events_correlates_setup_and_waits_for_settled(tmp_path: Path, scenario: str) -> None:
    events = asyncio.run(_run(tmp_path, scenario))
    assert events[0] == dict(type='system', subtype='init', session_id='rpc-session',
                             runtime='pi', model='test-model', cwd=str(tmp_path))
    result, = [event for event in events if event['type'] == 'result']
    assert result['subtype'] == 'success'
    assert result['result'] == 'final\u2028answer'
    assert result['modelUsage']['test-model']['inputTokens'] == (14 if scenario == 'retry' else 7)
    records = _records(tmp_path)
    requests = [r for r in records if 'id' in r]
    assert len(requests) == 3
    assert len({r['id'] for r in requests}) == 3
    assert all(isinstance(r['id'], str) for r in requests)
    assert requests[-1]['message'] == 'model prompt'
    assert sum('acked' in r for r in records[:records.index(requests[-1])]) == 2
    assert records[-1] == {'stdin_closed': True}


@pytest.mark.parametrize(('scenario', 'error', 'prompt_sent'), [
    ('missing_session', 'sessionId', False),
    ('empty_session', 'sessionId', False),
    ('invalid_session', 'sessionId', False),
    ('fail_get_state', 'get_state', False),
    ('fail_set_steering_mode', 'set_steering_mode', False),
    ('handled', 'handled', True),
    ('queued', 'queued', True),
    ('missing_disposition', 'disposition', True),
    ('fail_prompt', 'prompt', True),
    ('fast_rejected', 'prompt', True),
    ('early_exit', 'ended', False),
    ('prompt_eof', 'ended', True),
    ('run_eof', 'settled', True),
    ('extension_error', 'extension_error', True),
    ('invalid_json', 'JSON', True),
    ('invalid_utf8', 'JSON', True),
    ('non_object', 'JSON', True),
    ('parse_error', 'parse', True),
])
def test_startup_and_protocol_failures_are_explicit(tmp_path: Path, scenario: str, error: str,
                                                   prompt_sent: bool) -> None:
    events = asyncio.run(_run(tmp_path, scenario))
    result, = [event for event in events if event['type'] == 'result']
    assert result['is_error'] is True
    assert error in ' '.join(result['errors'])
    assert 'SECRET-test' not in str(events)
    assert any(r.get('type') == 'prompt' for r in _records(tmp_path)) is prompt_sent


@pytest.mark.parametrize('scenario', ['setup_timeout', 'setup_noise_timeout', 'prompt_timeout', 'fast_timeout'])
def test_ack_deadlines_include_silent_and_busy_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     scenario: str) -> None:
    monkeypatch.setattr(pi_steer, '_SETUP_TIMEOUT_SEC', 0.15)
    monkeypatch.setattr(pi_steer, '_PROMPT_TIMEOUT_SEC', 0.15)
    events = asyncio.run(_run(tmp_path, scenario))
    result, = [event for event in events if event['type'] == 'result']
    assert result['is_error'] is True
    assert 'timed out' in ' '.join(result['errors'])
    assert any(r.get('type') == 'prompt' for r in _records(tmp_path)) is (
        scenario in ('prompt_timeout', 'fast_timeout'))


def test_runtime_selects_rpc_channel_and_preserves_private_launch(tmp_path: Path) -> None:
    plain = _launch(_provider())
    profile = replace(plain.profile, runtime=replace(plain.profile.runtime, capabilities=frozenset({Capability.STEER})))
    launch = replace(plain, settings=replace(plain.settings, profile=profile))
    prompt_file = tmp_path / 'prompts' / 'system.md'
    prompt_file.parent.mkdir()
    prompt_file.write_text('system instructions')
    args = PI.steer_command('pi', cwd=tmp_path, prompt_file=prompt_file, launch=launch)
    assert args[args.index('--mode') + 1] == 'rpc'
    assert '-p' not in args and '--no-ui' not in args
    assert '--no-session' in args and '--offline' in args
    assert '-e' in args and '--append-system-prompt' in args
    assert any(arg.startswith('HOME=') for arg in args)
    assert isinstance(PI.run_channel(None, launch=launch, cwd=tmp_path), PiChannel)
    assert isinstance(PI.run_channel(None, launch=plain, cwd=tmp_path), StreamChannel)
