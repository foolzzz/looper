"""Messages wire contracts through the installed async SDK and loopback HTTP."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from functools import wraps
import json
from pathlib import Path

import pytest

from fake_model_api import (FakeModelAPI, ResponseScript, call_events, event,
    message_call_events, message_events, message_start, text_events)
from looper.agents.runtimes import ApiProtocol, RuntimeUnsupported
from looper.agents.runtimes.api.loop import (ErrorKind, MessageItem, ModelTarget, Outcome,
    ReasoningItem, ReplayState, RequestDone, TextDelta, ToolCall, ToolCallItem, ToolDone,
    ToolOutcome, ToolResultItem, ToolSpec, item_to_json, loop_for)
from looper.agents.runtimes.api.session import FORBIDDEN_ENV
from looper.agents.runtimes.cancel import TurnCancel

PROTOCOL = ApiProtocol.ANTHROPIC_MESSAGES
SCHEMA = {'type': 'object', 'properties': {'optional': {'type': 'string'}}}
BASELINE = '5f03d2929d14d21c6cc69ec053e0230370829f44'


def async_test(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 20))
    return wrapped


class Transcript:
    def __init__(self, *items, protocol=PROTOCOL):
        self.protocol, self.history, self.batches = protocol, list(items), []

    async def items(self):
        return list(self.history)

    async def append(self, items):
        self.batches.append(list(items))
        self.history.extend(items)


class Tools:
    def __init__(self):
        self.calls = []

    async def specs(self):
        return [ToolSpec('lookup', 'verbatim description', SCHEMA)]

    async def call(self, call):
        self.calls.append(call)
        return ToolOutcome('result', True, False)


async def turn(api, *, loop=None, history=None, tools=None, cancel=None, effort=None,
               model='test-model', protocol=PROTOCOL, system_prompt='system', **kw):
    owned = loop is None
    loop = loop or loop_for(protocol)
    events = []

    async def emit(e):
        events.append(e)

    try:
        result = await loop.run(target=ModelTarget(protocol, api.base_url, model, effort, 128000, 'test-key'),
            system_prompt=system_prompt, user_text='input', session_id='session',
            transcript=history or Transcript(protocol=protocol), tools=tools or Tools(),
            cancel=cancel or TurnCancel(), emit=emit, over_cap=lambda: False, **kw)
        return result, events
    finally:
        if owned:
            await loop.aclose()
            await loop.aclose()


@pytest.mark.parametrize('effort', [None, 'high'])
@async_test
async def test_messages_request_contract_and_cache_markers(effort):
    history = Transcript(MessageItem('user', 'prior'), MessageItem('assistant', 'checking'),
        ToolCallItem(ToolCall('old_1', 'lookup', '{"optional":"✓"}')),
        ToolResultItem('old_1', 'old result', True, False))
    async with FakeModelAPI([ResponseScript(events=message_events('answer'))]) as api:
        result, events = await turn(api, effort=effort, history=history, model='test-model[1m]')
        assert result.outcome == Outcome.ANSWERED and result.final_text == 'answer'
        assert ''.join(e.text for e in events if isinstance(e, TextDelta)) == 'answer'
        request = api.requests[0]
        assert request.path == '/v1/messages'
        assert request.headers['authorization'] == 'Bearer test-key'
        assert 'x-api-key' not in request.headers
        expected = dict(model='test-model', max_tokens=16000, stream=True,
            system=[dict(type='text', text='system', cache_control={'type': 'ephemeral'})],
            messages=[dict(role='user', content=[dict(type='text', text='prior')]),
                dict(role='assistant', content=[dict(type='text', text='checking'),
                    dict(type='tool_use', id='old_1', name='lookup', input={'optional': '✓'})]),
                dict(role='user', content=[dict(type='tool_result', tool_use_id='old_1', content='old result', is_error=True),
                    dict(type='text', text='input', cache_control={'type': 'ephemeral'})])],
            tools=[dict(name='lookup', description='verbatim description', input_schema=SCHEMA)])
        if effort is not None:
            expected.update(output_config={'effort': effort}, thinking={'type': 'adaptive'})
        assert request.body == expected


@async_test
async def test_messages_empty_system_does_not_manufacture_cache_block():
    async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
        result, _ = await turn(api, system_prompt='')
        assert result.outcome == Outcome.ANSWERED
        assert api.requests[0].body.get('system', []) == []
        assert 'thinking' not in api.requests[0].body and 'output_config' not in api.requests[0].body


@async_test
async def test_messages_tool_waits_for_message_stop():
    blocks = [dict(type='tool_use', id='first', name='lookup', input={'optional': 'one'}),
              dict(type='tool_use', id='second', name='lookup', input={'optional': 'two'})]
    script, tools, history = ResponseScript(events=message_events(blocks=blocks), gate=asyncio.Event()), Tools(), Transcript()
    async with FakeModelAPI([script, ResponseScript(events=message_events())]) as api:
        task = asyncio.create_task(turn(api, tools=tools, history=history))
        try:
            await asyncio.wait_for(script.gate_reached.wait(), 5)
            assert not tools.calls and not history.batches
            script.gate.set()
            result, events = await task
            assert result.outcome == Outcome.ANSWERED
            assert [c.call_id for c in tools.calls] == ['first', 'second']
            assert [e.call.call_id for e in events if isinstance(e, ToolDone)] == ['first', 'second']
            outputs = api.requests[1].body['messages'][-1]['content']
            assert outputs == [dict(type='tool_result', tool_use_id=c, content='result', is_error=True,
                **({'cache_control': {'type': 'ephemeral'}} if c == 'second' else {})) for c in ('first', 'second')]
            assert len(history.batches) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('model', ['test-model', 'other-model'])
@async_test
async def test_messages_thinking_replays_only_for_origin(model):
    blocks = [dict(type='thinking', thinking='safe summary', signature='opaque-signature'),
              dict(type='redacted_thinking', data='opaque-redacted'),
              dict(type='tool_use', id='call_1', name='lookup', input={})]
    history = Transcript()
    loop = loop_for(PROTOCOL)
    try:
        async with FakeModelAPI([ResponseScript(events=message_events(blocks=blocks)),
                ResponseScript(events=message_events()), ResponseScript(events=message_events())]) as api:
            result, visible = await turn(api, loop=loop, history=history)
            assert result.outcome == Outcome.ANSWERED
            first_replay = api.requests[1].body['messages'][1]['content']
            assert first_replay[:2] == blocks[:2]
            reasoning = [i for i in history.history if isinstance(i, ReasoningItem)]
            assert len(reasoning) == 2 and all(i.state.protocol == PROTOCOL for i in reasoning)
            assert 'opaque-signature' not in repr(visible) and 'opaque-redacted' not in repr(visible)
            await turn(api, loop=loop, history=history, model=model)
            encoded = json.dumps(api.requests[2].body)
            assert ('opaque-signature' in encoded) == (model == 'test-model')
            assert ('opaque-redacted' in encoded) == (model == 'test-model')
        # A foreign protocol's opaque content never reaches a Messages request.
        foreign = Transcript(ReasoningItem(('foreign summary',), ReplayState(
            ApiProtocol.OPENAI_RESPONSES, 'test-model', 'reason_1', 'foreign-opaque')))
        async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
            await turn(api, loop=loop, history=foreign)
            assert 'foreign-opaque' not in json.dumps(api.requests[0].body)
    finally:
        await loop.aclose()


@pytest.mark.parametrize('usage,expected', [
    ({'input_tokens': 20, 'output_tokens': 1, 'cache_read_input_tokens': 4, 'cache_creation_input_tokens': 3}, (20, 7, 4, 3, True, 27)),
    ({'input_tokens': 20, 'output_tokens': 1}, (20, 7, None, None, False, None)),
    ({'input_tokens': 20, 'cache_read_input_tokens': 0}, (20, 7, 0, None, False, None)),
    ({}, (None, 7, None, None, False, None)),
])
@async_test
async def test_messages_usage_separates_cache_buckets(usage, expected):
    events = message_events(usage=usage, output_tokens=7)
    events.insert(-2, event('message_delta', delta={'stop_reason': None, 'stop_sequence': None}, usage={'output_tokens': 4}))
    async with FakeModelAPI([ResponseScript(events=events)]) as api:
        result, _ = await turn(api)
        record = result.requests[0]
        u = record.usage
        assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.complete, record.prompt_tokens) == expected
        assert record.upstream_id == 'msg_1' and record.status == 200
        assert record.duration_ms >= record.first_token_ms >= 0


def invalid_scripts():
    ordinary = message_call_events()
    duplicate = message_events(blocks=[dict(type='tool_use', id='call_1', name='lookup', input={})] * 2)
    return [pytest.param([*ordinary[:-2], event('message_delta', delta={'stop_reason': 'max_tokens'}, usage={'output_tokens': 7}), event('message_stop')], ErrorKind.TRUNCATED, id='truncated'),
        pytest.param(ordinary[:-1], None, id='clean-eof'),
        pytest.param([*ordinary[:-1], event('error', error={'type': 'overloaded_error', 'message': 'temporary'})], ErrorKind.UNAVAILABLE, id='stream-error'),
        pytest.param([message_start(), event('content_block_start', index=-1, content_block=dict(type='tool_use', id='call_1', name='lookup', input={})), *ordinary[1:]], ErrorKind.PROTOCOL, id='negative-index'),
        pytest.param([message_start(), event('content_block_start', index=2, content_block=dict(type='tool_use', id='call_1', name='lookup', input={})), *ordinary[1:]], ErrorKind.PROTOCOL, id='index-gap'),
        pytest.param(duplicate, ErrorKind.PROTOCOL, id='duplicate-id'),
        pytest.param([*ordinary[:-1], event('unknown_structure'), event('message_stop')], ErrorKind.PROTOCOL, id='unknown-event'),
        pytest.param([message_start(), event('content_block_delta', index=0, delta={'type': 'text_delta', 'text': 'bad'}), *ordinary[1:]], ErrorKind.PROTOCOL, id='delta-before-start'),
        pytest.param([*ordinary[:-2], event('message_stop')], ErrorKind.PROTOCOL, id='missing-reason')]


@pytest.mark.parametrize('events,kind', invalid_scripts())
@async_test
async def test_messages_failed_or_truncated_stream_never_runs_tools(events, kind):
    tools, history = Tools(), Transcript()
    async with FakeModelAPI([ResponseScript(events=events)]) as api:
        result, visible = await turn(api, tools=tools, history=history, max_requests=1)
        assert result.outcome == (Outcome.MAX_REQUESTS if kind is None else Outcome.FAILED)
        assert result.error_kind == kind
        assert not tools.calls and not history.batches and not history.history
        assert not any(isinstance(e, RequestDone) for e in visible)
        assert len(result.requests) == 1 and not result.requests[0].usage.complete


@pytest.mark.parametrize('status,kind,count', [(401, ErrorKind.AUTH, 1), (403, ErrorKind.AUTH, 1),
    (404, ErrorKind.NOT_FOUND, 1), (429, ErrorKind.RATE_LIMITED, 3), (503, ErrorKind.UNAVAILABLE, 3),
    (400, ErrorKind.INVALID_REQUEST, 1)])
@async_test
async def test_messages_structured_status_errors_have_finite_retries(status, kind, count):
    script = ResponseScript(status=status, error={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'synthetic error'}},
        headers={'Retry-After': '0.01', 'x-request-id': 'req_error', 'x-should-retry': 'false' if count > 1 else 'true'})
    async with FakeModelAPI([script] * count) as api:
        result, _ = await turn(api)
        assert result.error_kind == kind and result.error_status == status
        assert len(api.requests) == count
        assert result.requests[0].retries == count - 1
        assert result.requests[0].upstream_id == 'req_error'
        assert [r.path for r in api.requests] == ['/v1/messages'] * count


@async_test
async def test_messages_retry_cancel_and_request_budget():
    unavailable = ResponseScript(status=503, headers={'Retry-After': '0.01'})
    async with FakeModelAPI([unavailable, ResponseScript(events=message_events())]) as api:
        result, _ = await turn(api)
        assert result.outcome == Outcome.ANSWERED
        assert result.requests[0].retries == 1 and len(api.requests) == 2
        assert [r.headers['x-stainless-retry-count'] for r in api.requests] == ['0', '1']
    cancel, tools, history = TurnCancel(), Tools(), Transcript()
    script = ResponseScript(events=message_call_events(), gate=asyncio.Event())
    async with FakeModelAPI([script]) as api:
        task = asyncio.create_task(turn(api, cancel=cancel, tools=tools, history=history))
        try:
            await asyncio.wait_for(script.gate_reached.wait(), 5)
            cancel.request()
            result, _ = await asyncio.wait_for(task, 2)
            assert result.outcome == Outcome.CANCELLED and not tools.calls and not history.history
            assert len(result.requests) == 1 and not result.requests[0].usage.complete
            await asyncio.wait_for(script.closed.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with FakeModelAPI([ResponseScript(events=message_call_events(call_id=f'call_{i}')) for i in range(30)]) as api:
        tools = Tools()
        result, _ = await turn(api, tools=tools)
        assert result.outcome == Outcome.MAX_REQUESTS and len(api.requests) == len(tools.calls) == 30
        assert sum(1 + r.retries for r in result.requests) == 30


@async_test
async def test_messages_broken_stream_resends_once_without_executing_staged_tools():
    tools, history = Tools(), Transcript()
    async with FakeModelAPI([ResponseScript(events=message_call_events()[:-1]),
            ResponseScript(events=message_events())]) as api:
        result, _ = await turn(api, tools=tools, history=history)
        assert result.outcome == Outcome.ANSWERED and len(result.requests) == 2
        assert not tools.calls and not result.requests[0].usage.complete
        assert history.history == [MessageItem('user', 'input'), MessageItem('assistant', 'Hello')]


@pytest.mark.parametrize('name', FORBIDDEN_ENV)
@async_test
async def test_messages_direct_codec_rejects_ambient_sdk_overrides(monkeypatch, name):
    loop = loop_for(PROTOCOL)
    try:
        async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
            result, _ = await turn(api, loop=loop)
            assert result.outcome == Outcome.ANSWERED
            monkeypatch.setenv(name, 'sensitive-value')
            with pytest.raises(RuntimeUnsupported) as error:
                await turn(api, loop=loop)
            assert name in str(error.value) and 'sensitive-value' not in str(error.value)
            assert len(api.requests) == 1
    finally:
        await loop.aclose()


def _event_projection(events):
    projected = []
    for value in events:
        item = asdict(value)
        if isinstance(value, RequestDone):
            item['record'].update(duration_ms='<duration>', first_token_ms='<first-token>')
        projected.append({'kind': type(value).__name__, **item})
    return json.loads(json.dumps(projected))


async def _responses_surfaces(root):
    """Capture independently comparable values; generated only on BASELINE."""
    import hashlib
    import inspect
    from looper.agents import runtimes
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
    from looper.agents.runtimes import AuthKind, SystemPromptMode
    from looper.agents.runtimes.api.adapter import ApiRuntime, Descriptor, RUNTIME_NOTE, prompt_text
    from looper.agents.runtimes.api import tools as platform_tools
    from tests.runtime_model_helpers import resolved_profile

    protocol = ApiProtocol.OPENAI_RESPONSES
    runtime = ApiRuntime()
    provider = LegacyProviderSpec('provider-test', 'Test account', AuthKind.API_KEY,
        frozenset({protocol}), base_url='https://example.invalid/v1', api_key='test-key',
        catalog=(CatalogModel('test-model[1m]', context_window=128000),))
    profile = resolved_profile('profile-test', 'Test profile', RuntimeSpec('rt-api', 'API', 'api',
        capabilities=runtime.capabilities), provider, 'test-model[1m]', 'high', custom_prompt='Custom instructions')
    launch = Launch(AgentSettings(profile=profile), role=AgentType.MANAGER)
    prompt_file = root / '.local' / 'prompts' / 'source.md'
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text('Custom instructions\n\nLooper instructions\n')
    command = runtime.resident_command('', cwd=root, prompt_file=prompt_file, launch=launch)
    descriptor = Descriptor.parse(command)
    values = {
        'runtime_note': RUNTIME_NOTE,
        'system_append': prompt_text(launch, 'Custom instructions\n\nLooper instructions\n'),
        'system_replace': prompt_text(replace(launch, settings=replace(launch.settings,
            system_prompt_mode=SystemPromptMode.REPLACE)), 'Looper instructions'),
        'command': [part.replace(str(root), '<test-root>') for part in command],
        'command_prompt': Path(descriptor.system_prompt_file).read_text(),
        'launch_record': runtime.launch_record(launch),
        'tool_result': asdict(platform_tools.tool_outcome({'status': 'error', 'reason': 'synthetic'})),
        'invalid_tool_result': asdict(platform_tools.invalid_arguments()),
    }
    # Freeze individual launch builders, rather than whole modules. Their argv
    # assembly is outside this codec change and remains at the recorded source.
    values['cli_launch_builders'] = {}
    for name, adapter in sorted(runtimes.ADAPTERS.items()):
        if name == 'api':
            continue
        for method in ('launch_args', 'resident_command', 'run_command', 'resume_args', 'environment'):
            fn = getattr(type(adapter), method)
            values['cli_launch_builders'][name + '.' + method] = hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
    values['cli_launch_args'] = {}
    import looper
    repository = str(Path(looper.__file__).resolve().parent.parent)
    for name in ('claude-code', 'codex', 'omp', 'grok', 'dsh', 'pi'):
        adapter = runtimes.ADAPTERS[name]
        selected = (ApiProtocol.ANTHROPIC_MESSAGES if name in ('claude-code', 'dsh') else
                    ApiProtocol.OPENAI_RESPONSES)
        cli_model = 'deepseek-flash[1m]' if name == 'dsh' else 'test-model[1m]'
        account = replace(provider, protocols=frozenset({selected}), catalog=(CatalogModel(cli_model, context_window=128000),),
            auth=AuthKind.OAUTH if name == 'grok' else AuthKind.API_KEY,
            oauth_runtime_type='grok' if name == 'grok' else None, api_key=None if name == 'grok' else 'test-key',
            base_url='' if name == 'grok' else provider.base_url)
        cli_profile = resolved_profile('cli-test', 'CLI profile', RuntimeSpec('rt-cli', name, name,
            capabilities=adapter.capabilities), account, cli_model, 'high')
        cli_launch = Launch(AgentSettings(profile=cli_profile), role=AgentType.MANAGER)
        values['cli_launch_args'][name] = [part.replace(repository, '<repository>')
            for part in adapter.launch_args(cli_launch)]
    values['platform_tool_boundaries'] = {name: hashlib.sha256(inspect.getsource(value).encode()).hexdigest()
        for name, value in [('ManagerTurnTools.specs', platform_tools.ManagerTurnTools.specs),
                            ('ManagerTurnTools.call', platform_tools.ManagerTurnTools.call)]}
    usage = {'input_tokens': 20, 'output_tokens': 5,
        'input_tokens_details': {'cached_tokens': 4, 'cache_write_tokens': 3}}
    history, tools = Transcript(MessageItem('assistant', 'prior'), protocol=protocol), Tools()
    history.history.extend([ReasoningItem(('summary',), ReplayState(protocol, 'test-model[1m]', 'reason_1', 'opaque')),
        ToolCallItem(ToolCall('old', 'lookup', '{"optional":"old"}')),
        ToolResultItem('old', 'old result', False, True)])
    async with FakeModelAPI([ResponseScript(events=call_events('{"optional":"✓"}')),
            ResponseScript(events=text_events('answer', usage=usage)),
            ResponseScript(events=text_events('resumed', usage=usage))]) as api:
        result, events = await turn(api, protocol=protocol, history=history, tools=tools,
            model='test-model[1m]', effort='high')
        assert result.outcome == Outcome.ANSWERED and len(api.requests) == 2
        values['requests'] = [{'path': r.path, 'body': r.body} for r in api.requests]
        values['events'] = _event_projection(events)
        values['history'] = [item_to_json(i) for i in history.history]
        values['usage'] = [asdict(r.usage) for r in result.requests]
        # A new loop consumes the recorded neutral history, as after restart.
        resumed, resumed_events = await turn(api, protocol=protocol, history=history,
            model='test-model[1m]', effort='high')
        values['resume_request'] = api.requests[2].body
        values['resume_answer'] = resumed.final_text
        values['resume_events'] = _event_projection(resumed_events)
        assert all(r.path == '/v1/responses' for r in api.requests)
    gate = ResponseScript(events=call_events(), gate=asyncio.Event())
    cancel, history, tools = TurnCancel(), Transcript(protocol=protocol), Tools()
    async with FakeModelAPI([gate]) as api:
        task = asyncio.create_task(turn(api, protocol=protocol, cancel=cancel, history=history, tools=tools))
        try:
            await asyncio.wait_for(gate.gate_reached.wait(), 5)
            cancel.request()
            result, events = await asyncio.wait_for(task, 2)
            values['cancel'] = {'outcome': result.outcome.value, 'history': history.history,
                'tool_calls': tools.calls, 'usage': asdict(result.requests[0].usage),
                'events': _event_projection(events), 'request': api.requests[0].body}
            await asyncio.wait_for(gate.closed.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return values


@async_test
async def test_responses_preserves_each_recorded_surface(tmp_path, monkeypatch):
    fixture = json.loads((Path(__file__).parent / 'fixtures' / 'api_messages_responses_off.json').read_text())
    assert fixture['source_commit'] == BASELINE
    # A Responses-only installation must never even create a Messages client.
    from looper.agents.runtimes.api import messages
    monkeypatch.setattr(messages.MessagesCodec, '__init__', lambda self: pytest.fail('unexpected Messages client'))
    actual = await _responses_surfaces(tmp_path)
    assert actual.keys() == fixture['surfaces'].keys()
    for name, expected in fixture['surfaces'].items():
        assert actual[name] == expected, name


@pytest.mark.parametrize('arguments', ['{bad', '[]', 'null', '{"x":1,"x":2}', '{"x":NaN}'])
@async_test
async def test_messages_invalid_object_arguments_use_existing_error_without_platform_call(arguments):
    from looper.agents.runtimes.api.tools import ManagerTurnTools
    class Ops:
        def __init__(self):
            self.calls = []

        async def current_listing(self, **kw):
            return [dict(name='lookup', description='verbatim description', inputSchema=SCHEMA)]

        async def call(self, *args, **kw):
            self.calls.append((args, kw))
            return {'status': 'ok'}

    ops, history = Ops(), Transcript()
    async with FakeModelAPI([ResponseScript(events=message_call_events(arguments))]) as api:
        result, visible = await turn(api, history=history, tools=ManagerTurnTools(ops, None), max_requests=1)
        assert result.outcome == Outcome.MAX_REQUESTS and not ops.calls
        outcomes = [e.outcome for e in visible if isinstance(e, ToolDone)]
        assert len(outcomes) == 1 and outcomes[0].is_error and not outcomes[0].ran
        assert json.loads(outcomes[0].text)['error']['code'] == 'invalid'
        assert history.history[-1].is_error and not history.history[-1].ran


@pytest.mark.parametrize('deadline', ['REQUEST_TIMEOUT', 'IDLE_TIMEOUT', 'SDK_RETRY'])
@async_test
async def test_messages_deadlines_close_streams_without_tool_or_partial_history(monkeypatch, deadline):
    from looper.agents.runtimes.api import messages
    async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
        warmup, _ = await turn(api)
        assert warmup.outcome == Outcome.ANSWERED
    monkeypatch.setattr(messages, 'REQUEST_TIMEOUT' if deadline == 'SDK_RETRY' else deadline, 0.2)
    scripts = ([ResponseScript(status=503, headers={'Retry-After': '1'})] if deadline == 'SDK_RETRY' else
               [ResponseScript(events=message_call_events(), gate=asyncio.Event()) for _ in range(2)])
    history, tools = Transcript(), Tools()
    async with FakeModelAPI(scripts) as api:
        result, events = await turn(api, history=history, tools=tools)
        assert result.outcome == Outcome.FAILED and result.error_kind == ErrorKind.UNAVAILABLE
        assert all(not r.usage.complete for r in result.requests)
        assert len(api.requests) == len(result.requests) == len(scripts)
        assert not history.history and not tools.calls
        assert not any(isinstance(e, RequestDone) for e in events)
        for script in scripts:
            await asyncio.wait_for(script.closed.wait(), 2)


@async_test
async def test_messages_client_is_owned_reused_and_closed():
    loop, other, history = loop_for(PROTOCOL), loop_for(PROTOCOL), Transcript()
    try:
        async with FakeModelAPI([ResponseScript(events=message_events()) for _ in range(2)]) as api:
            first, _ = await turn(api, loop=loop, history=history)
            assert first.outcome == Outcome.ANSWERED
            client = loop.codec._client
            await turn(api, loop=loop, history=history)
            assert loop.codec._client is client and other.codec._client is None
            assert len(api.requests[1].body['messages']) == 3
    finally:
        await loop.aclose()
        await loop.aclose()
        await other.aclose()
    assert client.is_closed()


@pytest.mark.parametrize('final_output', ['missing', None])
@async_test
async def test_messages_missing_final_output_is_not_reported_as_free(final_output):
    events = message_events()
    events[-2]['usage'] = {} if final_output == 'missing' else {'output_tokens': None}
    async with FakeModelAPI([ResponseScript(events=events)]) as api:
        result, _ = await turn(api)
        assert result.outcome == Outcome.ANSWERED
        assert result.requests[0].usage.output_tokens == 0
        assert not result.requests[0].usage.complete


@pytest.mark.parametrize('usage', [
    {'input_tokens': None, 'output_tokens': 0, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 2},
    {'input_tokens': 11, 'output_tokens': 0, 'cache_read_input_tokens': None, 'cache_creation_input_tokens': 2},
    {'input_tokens': 11, 'output_tokens': 0, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': None}])
@async_test
async def test_messages_null_usage_buckets_stay_unknown(usage):
    async with FakeModelAPI([ResponseScript(events=message_events(usage=usage))]) as api:
        result, _ = await turn(api)
        record = result.requests[0]
        assert result.outcome == Outcome.ANSWERED and not record.usage.complete
        assert record.prompt_tokens is None and record.usage.output_tokens == 5
        assert any(value is None for value in (record.usage.input_tokens,
            record.usage.cache_read_tokens, record.usage.cache_write_tokens))


@async_test
async def test_messages_aborted_transport_preserves_known_usage_and_resends_once():
    scripts = [ResponseScript(events=message_call_events(), disconnect_after=4),
               ResponseScript(events=message_call_events(), disconnect_after=4)]
    history, tools = Transcript(), Tools()
    async with FakeModelAPI(scripts) as api:
        result, _ = await turn(api, tools=tools, history=history)
        assert result.outcome == Outcome.FAILED and len(api.requests) == len(result.requests) == 2
        assert not tools.calls and not history.history
        assert all(r.usage.input_tokens == 11 and not r.usage.complete for r in result.requests)


@pytest.mark.parametrize('cap,prior_call', [(1, False), (2, False), (3, False), (3, True)])
@async_test
async def test_messages_budget_counts_http_retries_across_tool_rounds(cap, prior_call):
    unavailable = ResponseScript(status=503, headers={'Retry-After': '0.01'})
    scripts = [ResponseScript(events=message_call_events())] if prior_call else []
    scripts += [unavailable, unavailable, ResponseScript(events=message_events())]
    tools = Tools()
    async with FakeModelAPI(scripts) as api:
        result, _ = await turn(api, tools=tools, max_requests=cap)
        assert result.outcome == (Outcome.ANSWERED if cap == 3 and not prior_call else Outcome.MAX_REQUESTS)
        assert len(api.requests) == cap and len(tools.calls) == int(prior_call)
        assert sum(1 + r.retries for r in result.requests) == cap


@pytest.mark.parametrize('raw', ['{broken', '[]', '{"x":1,"x":2}', '{"x":NaN}'])
@async_test
async def test_messages_invalid_stored_arguments_fail_before_http_without_repair(raw):
    async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
        positive, _ = await turn(api)
        assert positive.outcome == Outcome.ANSWERED and len(api.requests) == 1
        history = Transcript(ToolCallItem(ToolCall('invalid-old', 'lookup', raw)),
            ToolResultItem('invalid-old', 'invalid arguments', True, False))
        result, _ = await turn(api, history=history)
        assert result.outcome == Outcome.FAILED and result.error_kind == ErrorKind.INVALID_REQUEST
        assert len(api.requests) == 1
        assert history.history[0].call.arguments == raw


@async_test
async def test_messages_stop_after_completed_tool_preserves_batch_with_unexecuted_results():
    cancel, history = TurnCancel(), Transcript()
    class StopTools(Tools):
        async def call(self, call):
            outcome = await super().call(call)
            cancel.request()
            return replace(outcome, ran=True)
    tools = StopTools()
    blocks = [dict(type='tool_use', id=identity, name='lookup', input={}) for identity in ('first', 'second')]
    async with FakeModelAPI([ResponseScript(events=message_events(blocks=blocks))]) as api:
        result, _ = await turn(api, tools=tools, history=history, cancel=cancel)
        assert result.outcome == Outcome.CANCELLED and len(tools.calls) == len(api.requests) == 1
        outputs = [i for i in history.history if isinstance(i, ToolResultItem)]
        assert [(i.call_id, i.ran) for i in outputs] == [('first', True), ('second', False)]
        assert json.loads(outputs[1].text)['error']['code'] == 'stopped'
        assert result.requests[0].usage.complete and len(history.batches) == 1


@async_test
async def test_messages_explicit_endpoint_prefix_and_ambient_key_are_respected(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ambient-key-must-not-be-sent')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'ambient-token-must-not-be-sent')
    async with FakeModelAPI([ResponseScript(events=message_events())]) as api:
        api.base_url += '/custom-prefix'
        result, _ = await turn(api)
        assert result.outcome == Outcome.ANSWERED and api.requests[0].path == '/v1/custom-prefix/messages'
        assert api.requests[0].headers['authorization'] == 'Bearer test-key'
        assert 'x-api-key' not in api.requests[0].headers
