"""Manager session contracts through the loopback Responses API.

Prompt composition is also covered by tests/test_model_launch_parity_db.py;
its CLI fixtures deliberately remain unchanged.
"""
import asyncio
from functools import wraps

import pytest

from fake_model_api import (
    FakeModelAPI, ResponseScript, call_item, completed, event, reasoning_item,
    text_events, text_item,
)
from looper.agents import claude_cli
from looper.agents.ops import ErrorCode, OpError
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ApiProtocol, InProcessTools
from looper.agents.runtimes.api import session as session_module
from looper.agents.runtimes.api.adapter import Descriptor, KEY_ENV
from looper.agents.runtimes.api.loop import (
    ErrorKind, LoopResult, MessageItem, Outcome, ReasoningItem,
    ToolCallItem, ToolOutcome, ToolResultItem, ToolSpec,
)
from looper.agents.runtimes.api.session import ApiSession, FORBIDDEN_ENV
from looper.agents.runtimes.cancel import TurnCancel

PROTOCOL = ApiProtocol.OPENAI_RESPONSES
TOKENS = {'input_tokens': 20, 'output_tokens': 5,
          'input_tokens_details': {'cached_tokens': 4, 'cache_write_tokens': 3}}


def async_test(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 12))
    return wrapped


@pytest.fixture(autouse=True)
def clear_sdk_environment(monkeypatch):
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)


class Tools:
    def __init__(self):
        self.listings, self.calls = 0, []

    async def specs(self):
        self.listings += 1
        return [ToolSpec('lookup', f'listing {self.listings}', {'type': 'object'})]

    async def call(self, call):
        self.calls.append(call)
        return ToolOutcome(f'result {call.call_id}', False, True)


async def start(tmp_path, base_url, *, session=None, instance=None, **kwargs):
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('system instructions', encoding='utf-8')
    command = Descriptor(PROTOCOL, 'test-model', base_url, None, None, str(prompt), session).to_argv()
    instance = instance or ApiSession(label='Manager')
    await instance.start(command, cwd=tmp_path, signature='signature', env={KEY_ENV: 'test-key'}, **kwargs)
    return instance, command


async def turn(session, text='hello', *, tools=None, collect=None, **kwargs):
    events = []

    async def emit(value):
        events.append(value)
        if collect:
            await collect(value)

    session.set_turn_tools(tools or Tools())
    return await session.run_turn(text, emit, **kwargs), events


@async_test
async def test_session_streams_calls_and_results_in_order_with_one_listing_snapshot(tmp_path):
    calls = [call_item(arguments='{"key":"value"}', name='lookup', call_id='a', id='fc_a'),
             call_item(arguments='[]', name='lookup', call_id='b', id='fc_b')]
    first = ResponseScript(events=[event('response.output_text.delta', output_index=1,
        item_id='msg_1', content_index=0, delta='checking'),
        completed([reasoning_item(), text_item('checking'), *calls], usage=TOKENS)])
    tools = Tools()
    async with FakeModelAPI([first, ResponseScript(events=text_events('answer', usage=TOKENS))]) as api:
        session, command = await start(tmp_path, api.base_url)
        try:
            result, events = await turn(session, tools=tools)
            assert isinstance(session, InProcessTools)
            assert session.label == 'Manager' and session.started and session.command == command
            assert session.pid is None and not session.shares_bridge and not session.can_interrupt
            assert not session.stale('signature') and session.stale('changed')
            assert events[0] == {'type': 'system', 'subtype': 'init',
                'session_id': result.result_event['session_id'], 'model': 'test-model', 'tools': ['lookup']}
            assert events[1]['event']['delta'] == {'type': 'text_delta', 'text': 'checking'}
            assert result.result_event['subtype'] == 'success', result.result_event
            assistant = next(e for e in events if e['type'] == 'assistant')
            assert assistant['message']['content'] == [
                {'type': 'thinking', 'thinking': 'Thinking'}, {'type': 'text', 'text': 'checking'},
                {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {'key': 'value'}},
                {'type': 'tool_use', 'id': 'b', 'name': 'lookup', 'input': {}}]
            results = [e['message']['content'][0] for e in events if e['type'] == 'user']
            assert results == [dict(type='tool_result', tool_use_id=i,
                content=f'result {i}', is_error=False) for i in ['a', 'b']]
            assert [c.call_id for c in tools.calls] == ['a', 'b']
            assert tools.listings == 1
            assert api.requests[0].body['tools'] == api.requests[1].body['tools']
            assert result.result_event['subtype'] == 'success' and result.result_event['result'] == 'answer'
            assert result.result_event['modelUsage'] == {'test-model': dict(inputTokens=26,
                outputTokens=10, cacheReadInputTokens=8, cacheCreationInputTokens=6)}
            assert result.result_event['api'] == dict(protocol=PROTOCOL.value, requests=2,
                retries=0, upstream_ids=['resp_1', 'resp_1'])
            usage_events = [e for e in events if e['type'] == 'usage']
            assert len(usage_events) == 2 and all('parent_tool_use_id' not in e for e in usage_events)
            assert result.session_started and result.prompt_sent and result.usage_complete
            assert [e['type'] for e in events] == [
                'system', 'stream_event', 'assistant', 'usage', 'user', 'user',
                'stream_event', 'assistant', 'usage']
            history = await session._transcripts[result.result_event['session_id']].items()
            assert all(isinstance(item, (MessageItem, ReasoningItem, ToolCallItem, ToolResultItem))
                       for item in history)
            assert sum(isinstance(item, ToolCallItem) for item in history) == 2
            assert sum(isinstance(item, ToolResultItem) for item in history) == 2
        finally:
            await session.stop()


@async_test
async def test_two_turns_reuse_neutral_history_and_add_provider_priced_usage(tmp_path, monkeypatch):
    async with FakeModelAPI([ResponseScript(events=text_events('one', usage=TOKENS)),
                             ResponseScript(events=text_events('two', usage=TOKENS))]) as api:
        def price(model, usage):
            return ((usage.input_tokens or 0) + (usage.output_tokens or 0)) / 1000
        session, _ = await start(tmp_path, api.base_url, totals=Usage(cost_usd=1), price=price)
        loop = session._loop
        closed = []
        original_close = loop.aclose

        async def close():
            closed.append(True)
            await original_close()

        monkeypatch.setattr(loop, 'aclose', close)
        first, _ = await turn(session, 'first')
        second, _ = await turn(session, 'second')
        sid = first.result_event['session_id']
        assert sid.startswith('api-') and second.result_event['session_id'] == sid
        assert session._loop is loop
        assert first.usage.cost_usd == second.usage.cost_usd == pytest.approx(.018)
        assert first.usage.input_tokens == 13 and first.usage.cache_read_tokens == 4
        assert session.totals == Usage(cost_usd=1.036, input_tokens=26, output_tokens=10,
                                      cache_read_tokens=8, cache_creation_tokens=6)
        assert api.requests[1].body['input'] == [
            {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'one'},
            {'role': 'user', 'content': 'second'}]
        history = session._transcripts[sid]
        assert history.protocol == PROTOCOL
        assert await history.items() == [MessageItem('user', 'first'), MessageItem('assistant', 'one'),
                                         MessageItem('user', 'second'), MessageItem('assistant', 'two')]
        await session.stop()
        assert closed == [True] and not session.started
        assert await history.items() and sid in session._transcripts
        assert await session.interrupt() is None


@pytest.mark.parametrize('name', FORBIDDEN_ENV)
@async_test
async def test_forbidden_sdk_environment_refuses_start_before_provider_request(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, '')
    async with FakeModelAPI([]) as api:
        with pytest.raises(OSError, match=name):
            await start(tmp_path, api.base_url)
        assert not api.requests


@async_test
async def test_start_rejects_invalid_descriptor_and_missing_provider_key(tmp_path):
    session = ApiSession(label='Manager')
    with pytest.raises(OSError, match='not an api runtime command'):
        await session.start(['other'], cwd=tmp_path, signature='s')
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('system')
    command = Descriptor(PROTOCOL, 'test-model', 'http://127.0.0.1:1/v1', None, None, str(prompt)).to_argv()
    with pytest.raises(OSError, match="the provider's API key did not reach the api runtime"):
        await session.start(command, cwd=tmp_path, signature='s')
    assert not session.started


@async_test
async def test_turn_without_tools_fails_and_turn_tools_are_cleared_after_use(tmp_path):
    async with FakeModelAPI([ResponseScript(events=text_events())]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            await turn(session)
            events = []

            async def collect(value):
                events.append(value)

            result = await session.run_turn('again', collect)
            assert result.result_event['errors'] == ['the Manager gave the api session no tools']
            assert not events and len(api.requests) == 1
        finally:
            await session.stop()


@async_test
async def test_unavailable_tool_listing_fails_before_init_or_request(tmp_path):
    class Unavailable(Tools):
        async def specs(self):
            raise OpError(ErrorCode.UNAVAILABLE, 'database unavailable')

    async with FakeModelAPI([]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            result, events = await turn(session, tools=Unavailable())
            assert result.result_event['errors'] == ["Looper's platform tools are unavailable: database unavailable"]
            assert not events and not api.requests
        finally:
            await session.stop()


@pytest.mark.parametrize('mismatch', [False, True])
@async_test
async def test_unknown_or_incompatible_saved_session_emits_nothing(tmp_path, mismatch):
    async with FakeModelAPI([ResponseScript(events=text_events())] if mismatch else []) as api:
        session, _ = await start(tmp_path, api.base_url)
        sid = 'api-unknown'
        if mismatch:
            previous, _ = await turn(session)
            sid = previous.result_event['session_id']
            session._transcripts[sid].protocol = 'different-protocol'
            await session.stop()
        session, _ = await start(tmp_path, api.base_url, session=sid, instance=session)
        try:
            before = len(api.requests)
            result, events = await turn(session)
            assert result.result_event == dict(type='result', subtype='error_during_execution',
                is_error=True, errors=[f'session {sid} could not be resumed'], num_turns=0)
            assert not result.session_started and not result.prompt_sent and result.usage == Usage()
            assert not events and len(api.requests) == before
        finally:
            await session.stop()


@async_test
async def test_session_history_evicts_the_least_recently_used_after_four_sessions(tmp_path):
    async with FakeModelAPI([ResponseScript(events=text_events()) for _ in range(6)]) as api:
        session, ids = None, []
        for _ in range(4):
            session, _ = await start(tmp_path, api.base_url, instance=session)
            result, _ = await turn(session)
            ids.append(result.result_event['session_id'])
            await session.stop()
        session, _ = await start(tmp_path, api.base_url, instance=session, session=ids[0])
        await turn(session)
        await session.stop()
        session, _ = await start(tmp_path, api.base_url, instance=session)
        result, _ = await turn(session)
        try:
            assert len(session._transcripts) == 4
            assert ids[0] in session._transcripts and ids[1] not in session._transcripts
            assert result.result_event['session_id'] in session._transcripts
        finally:
            await session.stop()


@pytest.mark.parametrize('mode', ['failed', 'cancelled', 'resent'])
@async_test
async def test_unfinished_attempts_make_final_usage_incomplete(tmp_path, mode):
    cancel = TurnCancel()
    script = ResponseScript(events=text_events('partial', usage=TOKENS),
                            gate=asyncio.Event()) if mode == 'cancelled' else ResponseScript(
        events=[event('response.output_text.delta', output_index=0, item_id='msg_1',
                      content_index=0, delta='partial')], disconnect_after=1)
    scripts = [script]
    if mode == 'resent':
        scripts.append(ResponseScript(events=text_events('partial answer', usage=TOKENS)))
    elif mode == 'failed':
        scripts.append(ResponseScript(events=['not-json']))
    async with FakeModelAPI(scripts) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            if mode == 'cancelled':
                task = asyncio.create_task(turn(session, cancel=cancel))
                await script.gate_reached.wait()
                cancel.request()
                result, events = await task
            else:
                result, events = await turn(session)
            assert not result.usage_complete and not result.result_event['usage_complete']
            usage_events = [e for e in events if e['type'] == 'usage']
            assert len(usage_events) == (1 if mode == 'resent' else 0)
            assert result.result_event['api']['requests'] == len(scripts)
            if mode == 'cancelled':
                assert result.result_event['subtype'] == 'error_user_stop'
            elif mode == 'resent':
                assert result.result_event['subtype'] == 'success'
        finally:
            await session.stop()


@async_test
async def test_soft_cap_uses_capstop_usage_and_keeps_completed_tool_effects(tmp_path):
    cap = claude_cli.CapStop(claude_cli.soft_cap_result(2, 1), Usage(cost_usd=2))
    crossed = False

    async def collect(value):
        nonlocal crossed
        if value['type'] == 'usage':
            crossed = True

    async with FakeModelAPI([ResponseScript(events=[completed([call_item()], usage=TOKENS)])]) as api:
        session, _ = await start(tmp_path, api.base_url)
        tools = Tools()
        try:
            result, _ = await turn(session, tools=tools, collect=collect,
                                   stop=lambda: cap if crossed else None)
            assert result.result_event[claude_cli.SOFT_CAP]
            assert result.usage == cap.usage and session.totals == cap.usage
            assert len(tools.calls) == len(api.requests) == 1
        finally:
            await session.stop()


@async_test
async def test_request_limit_stops_at_thirty_and_bounds_upstream_ids(tmp_path):
    async with FakeModelAPI([ResponseScript(events=[completed([call_item()], usage=TOKENS)])
                             for _ in range(30)]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            result, _ = await turn(session)
            assert result.result_event['subtype'] == 'error_max_turns'
            assert result.result_event['errors'] == [
                'the turn made 30 model requests, the most a turn may make; it stopped there, '
                'and what its tool calls did was not undone']
            assert result.result_event['num_turns'] == len(api.requests) == 30
            assert len(result.result_event['api']['upstream_ids']) == 30
        finally:
            await session.stop()


@pytest.mark.parametrize('kind,status,expected', [
    (ErrorKind.AUTH, 401, 'the provider refused the API key (HTTP 401): update it in Settings → LLM Providers'),
    (ErrorKind.NOT_FOUND, 404, 'the provider has no model test-model'),
    (ErrorKind.RATE_LIMITED, 429, 'the model API is not available now (HTTP 429, after retries); try again later'),
    (ErrorKind.UNAVAILABLE, None, 'the model API is not available now (HTTP no response, after retries); try again later'),
    (ErrorKind.CONTEXT_OVERFLOW, 400, "the conversation is longer than the model's context window; send /new to start a new session"),
    (ErrorKind.TRUNCATED, 200, 'the reply was longer than the output limit and did not finish'),
    (ErrorKind.INVALID_REQUEST, 400, 'the provider refused the request: rejected'),
    (ErrorKind.PROTOCOL, None, "the provider's stream could not be read: rejected"),
])
@async_test
async def test_error_kind_maps_to_safe_user_message(tmp_path, monkeypatch, kind, status, expected):
    class FailedLoop:
        async def run(self, **kwargs):
            return LoopResult(Outcome.FAILED, '', (), kind, status, 'rejected', True)

        async def aclose(self):
            pass

    monkeypatch.setattr(session_module, 'loop_for', lambda protocol: FailedLoop())
    session, _ = await start(tmp_path, 'http://127.0.0.1:1/v1')
    try:
        result, _ = await turn(session)
        assert result.result_event['errors'] == [expected]
        assert len(expected) <= 300
    finally:
        await session.stop()


@async_test
async def test_provider_error_echoing_key_is_redacted_in_user_result(tmp_path):
    async with FakeModelAPI([ResponseScript(status=401,
        error={'error': {'message': 'rejected test-key'}})]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            result, _ = await turn(session)
            assert 'test-key' not in str(result.result_event)
            assert 'the provider refused the API key (HTTP 401)' in result.result_event['errors'][0]
        finally:
            await session.stop()


@async_test
async def test_completed_request_without_usage_marks_its_usage_event_incomplete(tmp_path):
    async with FakeModelAPI([ResponseScript(events=text_events('answer'))]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            result, events = await turn(session)
            usage_events = [e for e in events if e['type'] == 'usage']
            assert len(usage_events) == 1 and usage_events[0]['usage_complete'] is False
            assert result.result_event['subtype'] == 'success' and not result.usage_complete
        finally:
            await session.stop()


@async_test
async def test_provider_detail_is_redacted_and_bounded_before_user_display(tmp_path):
    async with FakeModelAPI([ResponseScript(status=400,
        error={'error': {'message': 'test-key ' + 'x' * 400}})]) as api:
        session, _ = await start(tmp_path, api.base_url)
        try:
            result, _ = await turn(session)
            detail = result.result_event['errors'][0]
            assert detail.startswith('the provider refused the request: ')
            assert 'test-key' not in detail and len(detail) == 300
        finally:
            await session.stop()
