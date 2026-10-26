"""Observable turn contracts using an injected codec; no provider traffic."""
import asyncio
import json
from dataclasses import replace
from functools import wraps

import pytest

from looper.agents.runtimes import ApiProtocol, RuntimeUnsupported
from looper.agents.runtimes.cancel import TurnCancel
from looper.agents.runtimes.api.loop import (
    CodecDone, CodecError, ErrorKind, MessageItem, ModelTarget, NormaAgentLoop,
    Outcome, ReasoningItem, ReplayState, RequestDone, RequestRecord, RequestUsage,
    TextDelta, ToolCall, ToolCallItem, ToolDone, ToolOutcome, ToolResultItem,
    ToolSpec, TranscriptFormatError, item_from_json, item_to_json, loop_for,
)

PROTOCOL = ApiProtocol.OPENAI_RESPONSES
TARGET = ModelTarget(PROTOCOL, 'http://127.0.0.1:1', 'test-model', None, None, 'test-key')
CALLS = (ToolCall('a', 'one', '{}'), ToolCall('b', 'two', '{}'))
UNKNOWN = RequestUsage(None, None, None, None, False)


def async_test(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 2))
    return wrapped


def done(text='answer', calls=()):
    items = (MessageItem('assistant', text), *(ToolCallItem(c) for c in calls))
    return CodecDone(items, RequestRecord(0, 200, 1, 0, 0, 'response',
                     RequestUsage(1, 2, 0, 0, True), 1), text, calls, None)


def broken():
    return CodecError(ErrorKind.UNAVAILABLE, 502, 'stream cut', retryable_stream=True)


class Stream:
    def __init__(self, script, index):
        self.script, self.index, self.closed = script, index, False
        self.closed_event = asyncio.Event()
        self.record = RequestRecord(index, None, 1, None, 0, None, UNKNOWN, None)

    async def __aiter__(self):
        for event in self.script:
            if isinstance(event, Exception):
                raise event
            if callable(event):
                await event()
            else:
                if isinstance(event, CodecDone):
                    event = replace(event, record=replace(event.record, index=self.index))
                    self.record = event.record
                yield event

    def snapshot(self):
        return self.record

    async def aclose(self):
        if self.close_hook:
            await self.close_hook()
        self.closed = True
        self.closed_event.set()


class Codec:
    protocol = PROTOCOL

    def __init__(self, *scripts):
        self.scripts, self.requests, self.streams, self.closed = scripts, [], [], False

    def stream(self, **request):
        self.requests.append(request)
        stream = Stream(self.scripts[len(self.requests) - 1], request['index'])
        self.streams.append(stream)
        stream.close_hook = getattr(self, 'close_hook', None)
        return stream

    async def aclose(self):
        self.closed = True


class Transcript:
    protocol = PROTOCOL

    def __init__(self):
        self.history, self.batches = [], []
        self.fail = False

    async def items(self):
        return list(self.history)

    async def append(self, items):
        if self.fail:
            raise OSError('persistence failed')
        self.batches.append(items)
        self.history.extend(items)


class Tools:
    def __init__(self, action=None):
        self.calls, self.action = [], action

    async def specs(self):
        return [ToolSpec('one', 'description', {'type': 'object'})]

    async def call(self, call):
        self.calls.append(call)
        if self.action:
            return await self.action(call)
        return ToolOutcome(call.name, False, True)


async def turn(codec, *, transcript=None, tools=None, cancel=None, emit=None, **kw):
    events = []

    async def collect(event):
        events.append(event)
        if emit:
            await emit(event)

    result = await NormaAgentLoop(codec).run(
        target=kw.pop('target', TARGET), system_prompt='system', user_text='input',
        session_id='session', transcript=transcript or Transcript(), tools=tools or Tools(),
        cancel=cancel or TurnCancel(), emit=collect, over_cap=kw.pop('over_cap', lambda: False), **kw)
    return result, events


@pytest.mark.parametrize('text', ['answer', ''])
@async_test
async def test_text_response_persists_one_user_and_completed_answer(text):
    history = Transcript()
    response = done(text) if text else replace(done(text), items=())
    result, _ = await turn(Codec([response]), transcript=history)
    assert result.outcome == Outcome.ANSWERED and result.final_text == text
    assert history.history == [MessageItem('user', 'input'), MessageItem('assistant', text)]
    assert len(result.requests) == 1


@async_test
async def test_calls_wait_for_completion_and_execute_serially_in_source_order():
    order = []

    async def action(call):
        order.append(call.call_id)
        await asyncio.sleep(0)
        assert order[-1] == call.call_id
        return ToolOutcome(call.name, False, True)

    async def incomplete():
        assert not order

    codec = Codec([incomplete, done(calls=CALLS)], [done()])
    result, _ = await turn(codec, tools=Tools(action))
    assert result.outcome == Outcome.ANSWERED and order == ['a', 'b']
    assert codec.requests[1]['items'][-2:] == [ToolResultItem('a', 'one', False, True),
                                                          ToolResultItem('b', 'two', False, True)]
    assert codec.requests[1]['items'].count(MessageItem('user', 'input')) == 1
    assert all(r['system_prompt'] == 'system' and r['specs'] == codec.requests[0]['specs']
               for r in codec.requests)


@async_test
async def test_tool_error_reaches_next_request_without_replaying_call():
    async def action(call):
        return ToolOutcome('refused', True, False)

    tools, codec = Tools(action), Codec([done(calls=CALLS[:1])], [done()])
    await turn(codec, tools=tools)
    assert tools.calls == list(CALLS[:1])
    assert codec.requests[1]['items'][-1] == ToolResultItem('a', 'refused', True, False)


@pytest.mark.parametrize('arguments', ['{invalid', '[]'])
@async_test
async def test_invalid_arguments_reach_dispatch_verbatim(arguments):
    call, tools = replace(CALLS[0], arguments=arguments), Tools()
    await turn(Codec([done(calls=(call,))], [done()]), tools=tools)
    assert tools.calls == [call]


@async_test
async def test_events_follow_delta_request_tool_order():
    observed = []

    async def emit(event):
        observed.append(type(event))

    async def action(call):
        assert observed == [TextDelta, RequestDone]
        return ToolOutcome('ok', False, True)

    _, events = await turn(Codec([TextDelta('a'), done('a', CALLS[:1])], [done()]),
                           tools=Tools(action), emit=emit)
    assert [type(e) for e in events] == [TextDelta, RequestDone, ToolDone, RequestDone]


@pytest.mark.parametrize('cap,resend', [(1, False), (30, False), (1, True)])
@async_test
async def test_request_cap_counts_every_invocation(cap, resend):
    codec = Codec(*([[TextDelta('x'), broken()]] if resend else [[done(calls=CALLS[:1])]] * cap))
    result, _ = await turn(codec, **({'max_requests': cap} if cap != 30 else {}))
    assert result.outcome == Outcome.MAX_REQUESTS
    assert len(result.requests) == len(codec.requests) == cap
    with pytest.raises(ValueError):
        await turn(Codec(), max_requests=0)


@async_test
async def test_soft_cap_is_checked_after_request_done():
    capped = False

    async def emit(event):
        nonlocal capped
        if isinstance(event, RequestDone):
            capped = True

    codec = Codec([done(calls=CALLS[:1])])
    result, _ = await turn(codec, over_cap=lambda: capped, emit=emit)
    assert result.outcome == Outcome.CAPPED and len(codec.requests) == 1


@async_test
async def test_cancel_before_request_persists_nothing():
    cancel, codec, history = TurnCancel(), Codec(), Transcript()
    cancel.request()
    result, events = await turn(codec, cancel=cancel, transcript=history)
    assert result.outcome == Outcome.CANCELLED
    assert not codec.requests and not history.batches and not events


@async_test
async def test_cancel_during_request_closes_stream_and_records_unknown_usage():
    cancel, history = TurnCancel(), Transcript()

    async def block():
        cancel.request()
        try:
            await asyncio.Future()
        finally:
            await codec.streams[0].closed_event.wait()

    codec = Codec([TextDelta('partial'), block])
    result, _ = await asyncio.wait_for(turn(codec, cancel=cancel, transcript=history), 1)
    assert result.outcome == Outcome.CANCELLED and codec.streams[0].closed
    assert result.requests[0].usage == UNKNOWN and not history.batches


@async_test
async def test_cancel_during_tool_finishes_active_call_and_stores_stopped_remainder():
    cancel, history = TurnCancel(), Transcript()

    async def action(call):
        cancel.request()
        await asyncio.sleep(0)
        return ToolOutcome('finished', False, True)

    tools, codec = Tools(action), Codec([done(calls=CALLS)])
    result, events = await turn(codec, tools=tools, cancel=cancel, transcript=history)
    stopped = ToolResultItem('b', json.dumps({'status': 'error', 'error': {'code': 'stopped',
        'message': 'the user stopped this turn: this call did not run'}}, ensure_ascii=False), True, False)
    assert result.outcome == Outcome.CANCELLED and tools.calls == [CALLS[0]]
    assert history.history[-2:] == [ToolResultItem('a', 'finished', False, True), stopped]
    assert [type(e) for e in events] == [RequestDone]


@pytest.mark.parametrize('after_tools', [False, True])
@async_test
async def test_broken_stream_resends_once_suppressing_only_current_request_prefix(after_tools):
    scripts = [[TextDelta('old'), done('old', CALLS[:1])]] if after_tools else []
    codec = Codec(*scripts, [TextDelta('ab'), broken()],
                  [TextDelta('a'), TextDelta('bc'), done('abc', CALLS[:1])], [broken()])
    result, events = await turn(codec)
    assert result.outcome == Outcome.FAILED
    assert ''.join(e.text for e in events if isinstance(e, TextDelta)) == ('old' if after_tools else '') + 'abc'
    assert len(result.requests) == (4 if after_tools else 3)
    assert not result.requests[int(after_tools)].usage.complete


@pytest.mark.parametrize('text', ['different', 'a'])
@async_test
async def test_resend_rejects_divergent_or_shorter_prefix(text):
    result, events = await turn(Codec([TextDelta('abc'), broken()], [TextDelta(text), done(text)]))
    assert result.outcome == Outcome.FAILED and result.error_kind == ErrorKind.PROTOCOL
    assert ''.join(e.text for e in events if isinstance(e, TextDelta)) == 'abc'


@pytest.mark.parametrize('completed', [False, True])
@async_test
async def test_first_request_failure_counts_successful_completions_not_attempt_indices(completed):
    tail = done(calls=CALLS[:1]) if completed else broken()
    codec = Codec([broken()], [tail], [CodecError(ErrorKind.AUTH, 401, 'denied')])
    result, _ = await turn(codec)
    assert result.first_request_failed is (not completed)
    assert len(result.requests) == (3 if completed else 2)


@async_test
async def test_atomic_batch_and_persistence_failure_never_repeat_operations():
    history, tools = Transcript(), Tools()
    previous = MessageItem('assistant', 'previous')
    history.history.append(previous)
    response = done(calls=CALLS)
    response = replace(response, items=(ReasoningItem(('summary',), None), *response.items))
    codec = Codec([response], [done()])
    await turn(codec, transcript=history, tools=tools)
    assert codec.requests[0]['items'] == [previous, MessageItem('user', 'input')]
    assert history.batches[0] == [MessageItem('user', 'input'), *response.items,
                               ToolResultItem('a', 'one', False, True), ToolResultItem('b', 'two', False, True)]
    history.fail = True
    codec = Codec([done(calls=CALLS)])
    with pytest.raises(OSError, match='persistence'):
        await turn(codec, transcript=history, tools=tools)
    assert tools.calls == list(CALLS) * 2 and len(codec.requests) == 1


@pytest.mark.parametrize('bad', [None, {'version': 2, 'kind': 'message'},
    {'version': 1, 'kind': 'unknown'}, {'version': 1, 'kind': 'message', 'role': 'system', 'text': ''},
    {'version': 1, 'kind': 'tool_call', 'call': {'call_id': 'a', 'name': 'one', 'arguments': []}},
    {'version': 1, 'kind': 'tool_result', 'call_id': 'a', 'text': '', 'is_error': 1, 'ran': True},
    {'version': 1, 'kind': 'reasoning', 'summary': ['ok'], 'state': {'protocol': 'unknown'}}])
def test_neutral_json_roundtrips_and_rejects_malformed_required_fields(bad):
    items = [MessageItem('user', ''), ToolCallItem(replace(CALLS[0], arguments='{invalid')),
             ToolResultItem('a', 'oops', True, False), ReasoningItem(('summary',),
             ReplayState(PROTOCOL, 'test-model', 'reason', 'opaque')), ReasoningItem((), None)]
    assert [item_from_json(json.loads(json.dumps(item_to_json(i)))) for i in items] == items
    assert all(item_to_json(i)['version'] == 1 for i in items)
    with pytest.raises(TranscriptFormatError):
        item_from_json(bad)


@async_test
async def test_protocol_mismatch_fails_before_request():
    codec = Codec()
    with pytest.raises(RuntimeUnsupported):
        await turn(codec, target=replace(TARGET, protocol=ApiProtocol.OPENAI_CHAT))
    history = Transcript()
    history.protocol = ApiProtocol.OPENAI_CHAT
    with pytest.raises(RuntimeUnsupported):
        await turn(codec, transcript=history)
    codec.protocol = ApiProtocol.OPENAI_CHAT
    with pytest.raises(RuntimeUnsupported):
        await turn(codec)
    assert not codec.requests
    for protocol in ApiProtocol:
        if protocol != PROTOCOL:
            with pytest.raises(RuntimeUnsupported):
                loop_for(protocol)


@pytest.mark.parametrize('ending', ['success', 'error', 'cancel', 'shutdown', 'close_shutdown', 'eof'])
@async_test
async def test_stream_and_client_lifecycle_is_safe_on_every_exit(ending):
    cancel = TurnCancel()
    parent = asyncio.current_task()

    async def stop():
        if ending == 'cancel':
            cancel.request()
        else:
            parent.cancel()
        await asyncio.Future()

    script = [done()] if ending in ('success', 'close_shutdown') else [stop]
    if ending == 'error':
        script = [CodecError(ErrorKind.AUTH, 401, 'denied')]
    elif ending == 'eof':
        script = []
    codec = Codec(script, [])
    if ending == 'close_shutdown':
        async def close_hook():
            parent.cancel()
            await asyncio.sleep(0)
        codec.close_hook = close_hook
    loop = NormaAgentLoop(codec)
    if ending in ('shutdown', 'close_shutdown'):
        with pytest.raises(asyncio.CancelledError):
            await turn(codec, cancel=cancel)
    else:
        result, _ = await turn(codec, cancel=cancel)
        if ending == 'eof':
            assert result.outcome == Outcome.FAILED and result.error_kind == ErrorKind.PROTOCOL
    await loop.aclose()
    await loop.aclose()
    assert len(codec.streams) == (2 if ending == 'eof' else 1)
    assert codec.closed and all(s.closed for s in codec.streams)


@async_test
async def test_provider_error_redacts_key_before_bounding_message():
    result, events = await turn(Codec([CodecError(ErrorKind.AUTH, 401, 'test-key ' * 100)]))
    assert result.error_kind == ErrorKind.AUTH and result.error_status == 401
    assert 'test-key' not in repr(result) + repr(TARGET) + repr(events)
    assert len(result.error_message) <= 300 and '***' in result.error_message
