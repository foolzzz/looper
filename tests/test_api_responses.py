"""Responses wire contracts through the real async SDK on loopback HTTP."""
import asyncio
from functools import wraps

import pytest

from fake_model_api import (
    FakeModelAPI, ResponseScript, call_events, call_item, completed, event, failed,
    reasoning_item, text_events, text_item,
)
from looper.agents.runtimes import ApiProtocol
from looper.agents.runtimes.api.loop import (
    ErrorKind, MessageItem, ModelTarget, Outcome, ReasoningItem, ReplayState,
    RequestDone, TextDelta, ToolCallItem, ToolOutcome, ToolResultItem, ToolSpec, loop_for,
)
from looper.agents.runtimes.cancel import TurnCancel

PROTOCOL = ApiProtocol.OPENAI_RESPONSES
SCHEMA = {'type': 'object', 'properties': {'optional': {'type': 'string'}}}


def async_test(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 12))
    return wrapped


class Transcript:
    protocol = PROTOCOL

    def __init__(self, *items):
        self.history = list(items)

    async def items(self):
        return list(self.history)

    async def append(self, items):
        self.history.extend(items)


class Tools:
    def __init__(self):
        self.calls = []

    async def specs(self):
        return [ToolSpec('lookup', 'verbatim description', SCHEMA)]

    async def call(self, call):
        self.calls.append(call)
        return ToolOutcome('result', True, False)


async def turn(api, *, loop=None, history=None, tools=None, cancel=None, effort=None, model='test-model', **kw):
    owned = loop is None
    loop = loop or loop_for(PROTOCOL)
    events = []

    async def emit(e):
        events.append(e)

    try:
        result = await loop.run(target=ModelTarget(PROTOCOL, api.base_url, model, effort, None, 'test-key'),
            system_prompt='system', user_text='input', session_id='session',
            transcript=history or Transcript(), tools=tools or Tools(), cancel=cancel or TurnCancel(),
            emit=emit, over_cap=lambda: False, **kw)
        return result, events
    finally:
        if owned:
            await loop.aclose()
            await loop.aclose()


@pytest.mark.parametrize('effort', ['low', None])
@async_test
async def test_loop_sends_full_history_and_verbatim_request_contract(effort):
    empty = event('response.output_text.delta', output_index=0, item_id='msg_1', content_index=0, delta='')
    async with FakeModelAPI([ResponseScript(events=[empty, *text_events('answer')])]) as api:
        result, events = await turn(api, effort=effort, history=Transcript(MessageItem('assistant', 'prior')))
        assert result.outcome == Outcome.ANSWERED and result.final_text == 'answer'
        assert ''.join(e.text for e in events if isinstance(e, TextDelta)) == 'answer'
        request = api.requests[0]
        assert request.path == '/v1/responses' and request.headers['authorization'] == 'Bearer test-key'
        expected = dict(model='test-model', instructions='system', store=False, prompt_cache_key='session',
            parallel_tool_calls=False, max_output_tokens=16000, stream=True,
            input=[{'role': 'assistant', 'content': 'prior'}, {'role': 'user', 'content': 'input'}],
            tools=[dict(type='function', name='lookup', description='verbatim description', parameters=SCHEMA, strict=False)])
        if effort is not None:
            expected.update(reasoning={'effort': effort}, include=['reasoning.encrypted_content'])
        assert request.body == expected and len(api.requests) == 1


@pytest.mark.parametrize('arguments', ['{ "optional": "✓" }', '{malformed'])
@async_test
async def test_tool_turn_replays_raw_arguments_and_result_ids(arguments):
    tools, history = Tools(), Transcript()
    async with FakeModelAPI([ResponseScript(events=call_events(arguments)), ResponseScript(events=text_events())]) as api:
        result, _ = await turn(api, tools=tools, history=history)
        assert result.outcome == Outcome.ANSWERED and tools.calls[0].arguments == arguments
        assert api.requests[1].body['input'] == [
            {'role': 'user', 'content': 'input'},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': arguments},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'result'}]
        assert all('previous_response_id' not in r.body for r in api.requests)
        assert history.history[-2] == ToolResultItem('call_1', 'result', True, False)


@async_test
async def test_done_call_waits_for_terminal_response_before_dispatch():
    script, tools = ResponseScript(events=call_events(), gate=asyncio.Event()), Tools()
    async with FakeModelAPI([script, ResponseScript(events=text_events())]) as api:
        task = asyncio.create_task(turn(api, tools=tools))
        try:
            await script.gate_reached.wait()
            assert not tools.calls
            script.gate.set()
            result, _ = await task
            assert result.outcome == Outcome.ANSWERED and len(tools.calls) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@async_test
async def test_native_output_order_is_authoritative_without_done_duplication():
    output = [reasoning_item(), text_item('answer'), call_item('{bad')]
    events = [event('response.output_item.added', output_index=i, item=item) for i, item in enumerate(output)]
    events += [event('response.reasoning_summary_text.delta', output_index=0, item_id='rs_1', summary_index=0, delta='Think'),
        event('response.reasoning_summary_text.done', output_index=0, item_id='rs_1', summary_index=0, text='Thinking'),
        event('response.output_text.delta', output_index=1, item_id='msg_1', content_index=0, delta='answer'),
        event('response.function_call_arguments.delta', output_index=2, item_id='fc_1', delta='{'),
        event('response.function_call_arguments.delta', output_index=2, item_id='fc_1', delta='bad'),
        event('response.function_call_arguments.done', output_index=2, item_id='fc_1', arguments='{bad')]
    events += [event('response.output_item.done', output_index=i, item=item) for i, item in enumerate(output)]
    events += [completed(output)]
    history = Transcript()
    async with FakeModelAPI([ResponseScript(events=events)]) as api:
        result, visible = await turn(api, history=history, max_requests=1)
        assert result.outcome == Outcome.MAX_REQUESTS
        assert [type(i) for i in history.history] == [MessageItem, ReasoningItem, MessageItem, ToolCallItem, ToolResultItem]
        done = [e for e in visible if isinstance(e, RequestDone)]
        assert len(done) == 1 and done[0].text == 'answer' and done[0].reasoning_summary == 'Thinking'
        assert ''.join(e.text for e in visible if isinstance(e, TextDelta)) == 'answer'
        assert 'opaque' not in repr(visible)


@pytest.mark.parametrize('model', ['test-model', 'other-model'])
@async_test
async def test_opaque_reasoning_replays_only_to_its_origin_model(model):
    item = ReasoningItem(('summary',), ReplayState(PROTOCOL, 'test-model', 'rs_1', 'encrypted-secret'))
    foreign = ReasoningItem(('foreign',), ReplayState(ApiProtocol.OPENAI_CHAT, model, 'rs_2', 'foreign-secret'))
    history = Transcript(item, foreign)
    async with FakeModelAPI([ResponseScript(events=text_events())]) as api:
        _, visible = await turn(api, history=history, model=model)
        expected = [dict(type='reasoning', id='rs_1', summary=[dict(type='summary_text', text='summary')], encrypted_content='encrypted-secret')] if model == 'test-model' else []
        assert api.requests[0].body['input'] == [*expected, {'role': 'user', 'content': 'input'}]
        assert history.history[:2] == [item, foreign] and 'encrypted-secret' not in repr(visible)


@async_test
async def test_refusal_is_an_answer_without_tools():
    tools = Tools()
    async with FakeModelAPI([ResponseScript(events=[completed([text_item('refused', refusal=True)])])]) as api:
        result, _ = await turn(api, tools=tools)
        assert result.final_text == 'refused' and result.outcome == Outcome.ANSWERED and not tools.calls


@pytest.mark.parametrize('tail', [[], ['{invalid'], [event('response.output_text.delta', delta='bad')],
    [failed()], [completed([call_item(call_id='')])], [completed([call_item(id='different')])]])
@async_test
async def test_invalid_or_unfinished_response_never_dispatches_calls(tail):
    tools, history = Tools(), Transcript()
    async with FakeModelAPI([ResponseScript(events=[*call_events()[:-1], *tail])]) as api:
        result, _ = await turn(api, tools=tools, history=history)
        assert result.outcome == Outcome.FAILED and not tools.calls and not history.history
        assert result.error_kind == (ErrorKind.UNAVAILABLE if tail and tail[0] == failed() else ErrorKind.PROTOCOL)
        assert len(result.requests) == 1


@pytest.mark.parametrize('status,code,kind', [
    (401, 'context_length_exceeded', ErrorKind.AUTH), (403, None, ErrorKind.AUTH),
    (404, None, ErrorKind.NOT_FOUND), (429, None, ErrorKind.RATE_LIMITED), (503, None, ErrorKind.UNAVAILABLE),
    (400, 'context_length_exceeded', ErrorKind.CONTEXT_OVERFLOW), (400, 'model_not_found', ErrorKind.NOT_FOUND),
    (400, None, ErrorKind.INVALID_REQUEST), (422, 'unknown', ErrorKind.INVALID_REQUEST),
    (408, None, ErrorKind.UNAVAILABLE), (409, None, ErrorKind.INVALID_REQUEST)])
@async_test
async def test_status_precedes_structured_error_classification(status, code, kind):
    count = 3 if status in (408, 409, 429, 503) else 1
    error = {'error': {'code': code, 'message': 'context overflow model missing'}}
    script = ResponseScript(status=status, error=error, headers={'Retry-After': '0.01', 'x-request-id': 'req_error', 'x-should-retry': 'false' if count > 1 else 'true'})
    async with FakeModelAPI([script] * count) as api:
        result, _ = await turn(api)
        assert result.error_kind == kind and result.error_status == status
        record = result.requests[0]
        assert record.retries == count - 1 and record.upstream_id == 'req_error'
        assert not record.usage.complete and len(api.requests) == count


@pytest.mark.parametrize('terminal,kind', [(failed('context_length_exceeded'), ErrorKind.CONTEXT_OVERFLOW),
    (failed('model_not_found'), ErrorKind.NOT_FOUND), (failed('unknown'), ErrorKind.UNAVAILABLE),
    (event('response.incomplete', response={'id': 'resp_1', 'status': 'incomplete', 'output': [call_item()]}), ErrorKind.TRUNCATED)])
@async_test
async def test_provider_terminal_failure_is_not_a_broken_stream_retry(terminal, kind):
    tools = Tools()
    async with FakeModelAPI([ResponseScript(events=[*call_events()[:-1], terminal])]) as api:
        result, _ = await turn(api, tools=tools)
        assert result.error_kind == kind and len(api.requests) == 1 and not tools.calls


@async_test
async def test_sdk_retry_count_and_short_retry_after_are_observed_at_http_boundary():
    async with FakeModelAPI([ResponseScript(status=503, headers={'Retry-After': '0.05'}), ResponseScript(events=text_events())]) as api:
        result, _ = await turn(api)
        assert api.requests[1].received_at - api.requests[0].received_at >= 0.045
        assert result.requests[0].retries == len(api.requests) - 1 == 1
        assert [r.headers['x-stainless-retry-count'] for r in api.requests] == ['0', '1']


@async_test
async def test_broken_transport_resends_once_and_retains_both_records_without_partial_tools():
    tools, history = Tools(), Transcript()
    async with FakeModelAPI([ResponseScript(events=call_events(), disconnect_after=4), ResponseScript(events=text_events())]) as api:
        result, _ = await turn(api, tools=tools, history=history)
        assert result.outcome == Outcome.ANSWERED and not tools.calls
        assert len(result.requests) == 2 and [r.retries for r in result.requests] == [0, 0]
        assert not result.requests[0].usage.complete
        assert history.history == [MessageItem('user', 'input'), MessageItem('assistant', 'Hello')]


@pytest.mark.parametrize('usage,expected', [
    ({'input_tokens': 20, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 4, 'cache_write_tokens': 3}}, (13, 5, 4, 3, True, 20)),
    ({'input_tokens': 20, 'output_tokens': 5}, (20, 5, 0, 0, True, 20)),
    ({'input_tokens': 2, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 4}}, (0, 5, 4, 0, False, 2)),
    ({'output_tokens': 5}, (None, 5, 0, 0, False, None)),
    ({'input_tokens': 2}, (2, None, 0, 0, False, 2)),
    (None, (None, None, None, None, False, None))])
@async_test
async def test_usage_preserves_unknown_totals_and_separate_cache_buckets(usage, expected):
    async with FakeModelAPI([ResponseScript(events=text_events(usage=usage))]) as api:
        result, _ = await turn(api)
        record = result.requests[0]
        u = record.usage
        assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.complete, record.prompt_tokens) == expected
        assert record.first_token_ms is not None and record.duration_ms >= record.first_token_ms >= 0
        assert record.upstream_id == 'resp_1'


@pytest.mark.parametrize('deadline', ['REQUEST_TIMEOUT', 'IDLE_TIMEOUT', 'SDK_RETRY'])
@async_test
async def test_deadlines_abort_io_and_close_server_connections(monkeypatch, deadline):
    from looper.agents.runtimes.api import responses
    monkeypatch.setattr(responses, 'REQUEST_TIMEOUT' if deadline == 'SDK_RETRY' else deadline, 0.05)
    scripts = ([ResponseScript(status=503, headers={'Retry-After': '1'})] if deadline == 'SDK_RETRY' else
               [ResponseScript(events=call_events(), gate=asyncio.Event()) for _ in range(2)])
    async with FakeModelAPI(scripts) as api:
        result, _ = await turn(api)
        assert result.outcome == Outcome.FAILED and result.error_kind == ErrorKind.UNAVAILABLE
        assert all(not r.usage.complete for r in result.requests)
        for script in scripts:
            await asyncio.wait_for(script.closed.wait(), 1)


@async_test
async def test_key_echo_is_bounded_and_redacted_in_results_and_diagnostics(caplog):
    unknown = [event('unrelated', encrypted_content='opaque-secret')] * 3
    script = ResponseScript(events=[*unknown, failed('unknown', 'test-key ' * 100)])
    async with FakeModelAPI([script]) as api:
        result, visible = await turn(api)
        assert result.error_message and len(result.error_message) <= 300 and '***' in result.error_message
        assert 'test-key' not in repr(result) + repr(visible) + caplog.text
        assert 'opaque-secret' not in caplog.text
        assert len([r for r in caplog.records if 'ignored' in r.message]) == 1


@pytest.mark.parametrize('bad', [
    event('response.output_item.added', output_index=0, item={**call_item(), 'call_id': ''}),
    event('response.output_item.done', output_index=0, item={**call_item(), 'call_id': None}),
    event('response.function_call_arguments.delta', output_index=0, item_id='fc_1', delta=42),
    event('response.reasoning_summary_text.delta', output_index=0, item_id='fc_1', summary_index=0, delta='wrong type'),
])
@async_test
async def test_invalid_event_identity_or_payload_never_grants_tool_execution(bad):
    tools = Tools()
    async with FakeModelAPI([ResponseScript(events=[bad, completed([call_item()])])]) as api:
        result, _ = await turn(api, tools=tools)
        assert result.error_kind == ErrorKind.PROTOCOL and not tools.calls


@pytest.mark.parametrize('stage', ['stream', 'retry_wait', 'retried_stream'])
@async_test
async def test_cancellation_closes_io_and_retains_observed_retries_without_partial_items(stage):
    loop, cancel, history = loop_for(PROTOCOL), TurnCancel(), Transcript()
    script = ResponseScript(status=503, headers={'Retry-After': '0.1'})
    gate = ResponseScript(events=call_events(), gate=asyncio.Event())
    scripts = [gate] if stage == 'stream' else [script, gate]
    async with FakeModelAPI(scripts) as api:
        task = asyncio.create_task(turn(api, loop=loop, cancel=cancel, history=history))
        try:
            await (script.closed.wait() if stage == 'retry_wait' else gate.gate_reached.wait())
            cancel.request()
            result, _ = await asyncio.wait_for(task, 1)
            assert result.requests[0].retries == len(api.requests) - 1 == int(stage == 'retried_stream')
            assert result.outcome == Outcome.CANCELLED and not history.history and not result.requests[0].usage.complete
            await asyncio.wait_for(scripts[len(api.requests) - 1].closed.wait(), 1)
            await loop.aclose()
            await loop.aclose()
            assert loop.codec._client.is_closed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await loop.aclose()


@async_test
async def test_session_reuses_owned_client_and_completes_without_background_errors():
    errors, history = [], Transcript()
    asyncio.get_running_loop().set_exception_handler(lambda _, context: errors.append(context))
    loop, other = loop_for(PROTOCOL), loop_for(PROTOCOL)
    assert loop is not other and loop.codec is not other.codec
    try:
        async with FakeModelAPI([ResponseScript(events=text_events()) for _ in range(2)]) as api:
            await turn(api, loop=loop, history=history)
            client = loop.codec._client
            await turn(api, loop=loop, history=history)
            assert loop.codec._client is client and not client.is_closed()
            assert len(api.requests[1].body['input']) == 3
            await asyncio.sleep(0)  # Allow async-generator finalizers to report failures.
            assert not errors
    finally:
        await loop.aclose()
        await other.aclose()
    assert client.is_closed()


@async_test
async def test_broken_record_preserves_response_id_seen_before_terminal():
    events = [event('response.created', response={'id': 'resp_partial'}), *call_events()]
    async with FakeModelAPI([ResponseScript(events=events, disconnect_after=4)]) as api:
        result, _ = await turn(api, max_requests=1)
        assert result.requests[0].upstream_id == 'resp_partial'
        assert result.outcome == Outcome.MAX_REQUESTS
