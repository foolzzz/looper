"""Chat stream conversion contracts, including replayable tool arguments."""
import asyncio
import json

import pytest

from looper.agents.llmrec.capture import _anthropic
from looper.agents.llmrec.dialects import ChatDialect, DIALECTS
from looper.agents.llmrec.translate import Rejection, from_chat, thinking_signature
from looper.agents.llmrec.translate_stream import StreamTranslator, sse_events


DIALECT = DIALECTS[ChatDialect.GENERIC]
USAGE = {'prompt_tokens': 20, 'completion_tokens': 7,
         'prompt_tokens_details': {'cached_tokens': 5, 'cache_write_tokens': 2}}


def events(frames):
    return [json.loads(frame.decode().split('data: ', 1)[1]) for frame in frames]


def chunk(delta=None, finish=None, **extra):
    return {'id': 'completion', 'choices': [{'index': 0, 'delta': delta or {},
                                            'finish_reason': finish}], **extra}


def translated(chunks, dialect=DIALECT):
    stream = StreamTranslator(dialect, 'client-model', 'msg-test')
    frames = stream.start()
    for item in chunks:
        frames.extend(stream.feed(item))
    frames.extend(stream.finish())
    return stream, events(frames)


def test_start_immediately_announces_message_and_ping():
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    start = events(stream.start())
    assert [event['type'] for event in start] == ['message_start', 'ping']
    assert start[0]['message'] == {
        'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'model': 'client-model',
        'content': [], 'stop_reason': None, 'stop_sequence': None,
        'usage': {'input_tokens': 0, 'cache_creation_input_tokens': 0,
                  'cache_read_input_tokens': 0, 'output_tokens': 0}}
    assert events([stream.ping()]) == [{'type': 'ping'}]


@pytest.mark.parametrize('reasoning', ['', '思考'])
@pytest.mark.parametrize('dialect', [DIALECT, DIALECTS[ChatDialect.VLLM]])
def test_text_and_reasoning_assemble_like_nonstream_response(reasoning, dialect):
    chunks = []
    if reasoning:
        chunks += [chunk({dialect.reasoning_field: reasoning[:1]}), chunk({dialect.reasoning_field: reasoning[1:]})]
    chunks += [chunk({'content': '你'}), chunk({'content': '好'}, 'stop'),
               {'choices': [], 'usage': USAGE}]
    stream, result = translated(chunks, dialect)
    expected = from_chat({'id': 'completion', 'choices': [{'message': {
        'content': '你好', dialect.reasoning_field: reasoning}, 'finish_reason': 'stop'}],
        'usage': USAGE}, client_model='client-model', dialect=dialect)
    expected['id'] = 'msg-test'
    assert _anthropic(result) == expected
    assert result[-2] == {'type': 'message_delta',
        'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
        'usage': {'input_tokens': 13, 'cache_creation_input_tokens': 2,
                  'cache_read_input_tokens': 5, 'output_tokens': 7}}
    assert stream.usage == USAGE
    assert not stream.usage_missing
    if reasoning:
        signature = next(e for e in result if e.get('delta', {}).get('type') == 'signature_delta')
        assert signature['delta']['signature'] == thinking_signature(reasoning)


def test_parallel_tools_buffer_by_index_and_normalize_arguments():
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    frames = stream.start()
    frames += stream.feed(chunk({'content': '工具'}))
    frames += stream.feed(chunk({'tool_calls': [
        {'index': 1, 'id': 'functions.Bash:0', 'function': {'name': 'Bash', 'arguments': '{"z":'}},
        {'index': 0, 'function': {'arguments': '{"b":2,'}},
    ]}))
    frames += stream.feed(chunk({'tool_calls': [
        {'index': 0, 'id': 'call-zero', 'function': {'name': 'Read', 'arguments': '"a":"中"}'}},
        {'index': 1, 'id': 'ignored', 'function': {'name': 'ignored', 'arguments': '1}'}},
    ]}, 'stop'))
    assert not any(e.get('content_block', {}).get('type') == 'tool_use' for e in events(frames))
    frames += stream.feed({'choices': [], 'usage': USAGE})
    frames += stream.finish()
    result = events(frames)
    expected = from_chat({'id': 'completion', 'choices': [{'message': {'content': '工具', 'tool_calls': [
        {'id': 'call-zero', 'function': {'name': 'Read', 'arguments': '{"b":2,"a":"中"}'}},
        {'id': 'functions.Bash:0', 'function': {'name': 'Bash', 'arguments': '{"z":1}'}},
    ]}, 'finish_reason': 'stop'}], 'usage': USAGE}, client_model='client-model', dialect=DIALECT)
    expected['id'] = 'msg-test'
    assert _anthropic(result) == expected
    args = [e['delta']['partial_json'] for e in result if e.get('delta', {}).get('type') == 'input_json_delta']
    assert args == ['{"b": 2, "a": "中"}', '{"z": 1}']
    assert result[-2]['delta']['stop_reason'] == 'tool_use'


@pytest.mark.parametrize('arguments', ['broken', '[]', 'null'])
def test_invalid_tool_arguments_emit_error_without_message_stop(arguments):
    stream, result = translated([chunk({'tool_calls': [
        {'index': 0, 'id': 'call', 'function': {'name': 'Read', 'arguments': arguments}},
    ]}, 'tool_calls')])
    assert result[-1] == {'type': 'error', 'error': {'type': 'api_error',
        'message': 'upstream returned invalid tool arguments'}}
    assert 'message_stop' not in [e['type'] for e in result]
    assert stream.tool_args_invalid
    assert stream.error_type == 'api_error'


def test_missing_tool_id_is_deterministic_and_empty_arguments_are_object():
    _, result = translated([chunk({'tool_calls': [
        {'index': 0, 'function': {'name': 'Read', 'arguments': ''}},
    ]}, 'tool_calls')])
    tool = _anthropic(result)['content'][0]
    expected = from_chat({'id': 'completion', 'choices': [{'message': {'tool_calls': [
        {'function': {'name': 'Read', 'arguments': ''}}]}, 'finish_reason': 'tool_calls'}]},
        client_model='client-model', dialect=DIALECT)
    assert tool == expected['content'][0]
    assert tool['input'] == {}


@pytest.mark.parametrize('finish,stop', [('stop', 'end_turn'), ('length', 'max_tokens'),
    ('content_filter', 'refusal'), ('function_call', 'tool_use')])
def test_finish_reason_contract(finish, stop):
    _, result = translated([chunk({'content': 'text'}, finish)])
    assert result[-2]['delta']['stop_reason'] == stop


def test_missing_finish_reason_is_stream_error():
    stream, result = translated([chunk({'content': 'partial'})])
    assert result[-1] == {'type': 'error', 'error': {'type': 'api_error',
        'message': 'upstream stream ended without finish_reason'}}
    assert stream.done
    assert stream.finish() == []


@pytest.mark.parametrize('upstream,error_type', [
    ({'error': {'type': 'api_error', 'message': 'interrupted'}}, 'api_error'),
    (chunk(finish='insufficient_system_resource'), 'overloaded_error'),
])
def test_upstream_errors_are_terminal(upstream, error_type):
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    stream.start()
    result = events(stream.feed(upstream) + stream.finish())
    assert result[-1]['type'] == 'error'
    assert result[-1]['error']['type'] == error_type
    assert stream.error_type == error_type
    assert stream.feed(chunk({'content': 'late'}, 'stop')) == []
    assert stream.fail('api_error', 'late') == []


def test_interruption_does_not_emit_message_stop():
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    result = stream.start() + stream.feed(chunk({'content': 'partial'}))
    result += stream.fail('api_error', 'upstream stream interrupted')
    assert events(result)[-1] == {'type': 'error', 'error': {'type': 'api_error',
        'message': 'upstream stream interrupted'}}
    assert 'message_stop' not in [e['type'] for e in events(result)]


def test_usage_missing_and_cache_fields_missing_are_observable():
    missing, result = translated([chunk(finish='stop')])
    assert missing.usage_missing
    assert _anthropic(result)['usage'] == {'input_tokens': 0, 'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': 0, 'output_tokens': 0}
    cache_missing, _ = translated([chunk(finish='stop', usage={'prompt_tokens': 12, 'completion_tokens': 3})])
    assert not cache_missing.usage_missing
    assert cache_missing.cache_fields_missing


def test_ignores_nonzero_choices():
    _, result = translated([{'id': 'completion', 'choices': [
        {'index': 1, 'delta': {'content': 'other'}, 'finish_reason': 'length'},
        {'index': 0, 'delta': {'content': 'first'}, 'finish_reason': 'stop'}]}])
    assert _anthropic(result)['content'] == [{'type': 'text', 'text': 'first'}]


def test_sse_parser_handles_fragmented_utf8_crlf_multiline_and_comments():
    async def run():
        payload = ': keep-alive\r\ndata: {"choices": [],\r\ndata: "text": "你好"}\r\n\r\ndata: [DONE]\n\n'
        async def source():
            for byte in payload.encode():
                yield bytes([byte])
            raise AssertionError('parser read beyond DONE')
        return [event async for event in sse_events(source())]
    assert asyncio.run(run()) == [None, {'choices': [], 'text': '你好'}]


@pytest.mark.parametrize('payload', [b'data: nope\n\n', b'data: []\n\n'])
def test_sse_parser_rejects_invalid_event(payload):
    async def run():
        async def source():
            yield payload
        return [event async for event in sse_events(source())]
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_sse_parser_dispatches_final_event_without_trailing_delimiter():
    async def run():
        async def source():
            yield b'data: {"choices": []}'
        return [event async for event in sse_events(source())]
    assert asyncio.run(run()) == [{'choices': []}]


@pytest.mark.parametrize('malformed', [
    None, [], {'choices': {}}, {'choices': [None]},
    {'choices': 'bad'}, chunk({'tool_calls': [None]}),
    {'choices': [{'delta': []}]}, {'choices': [{'delta': ['bad']}]},
    chunk({'tool_calls': {}}), chunk({'tool_calls': 'bad'}),
    chunk({'tool_calls': [{'index': 0, 'function': []}]}),
    chunk({'tool_calls': [{'index': 0, 'function': 'bad'}]}),
])
def test_malformed_upstream_chunk_rejects_at_stream_boundary(malformed):
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    stream.start()
    with pytest.raises(Rejection) as caught:
        stream.feed(malformed)
    assert caught.value.status == 502
    assert caught.value.error_type == 'api_error'
    assert caught.value.message == 'upstream returned invalid stream data'
    result = events(stream.fail(caught.value.error_type, caught.value.message))
    assert result[-1]['type'] == 'error'
    assert result[-1]['error']['type'] == 'api_error'
    assert not any(event['type'] == 'message_stop' for event in result)


@pytest.mark.parametrize('malformed', [
    {'id': 7}, {'id': []},
    chunk(finish=1), chunk(finish=[]),
    chunk({'reasoning_content': [1]}), chunk({'reasoning_content': []}),
    chunk({'content': 1}), chunk({'content': []}),
    *[chunk({'tool_calls': [{'index': index, 'function': {'name': 'Read'}}]})
      for index in (-1, True, '0', [], None)],
    *[chunk({'tool_calls': [{'index': 0, 'id': value, 'function': {'name': 'Read'}}]})
      for value in (7, [])],
    *[chunk({'tool_calls': [{'index': 0, 'function': {'name': value}}]})
      for value in (7, [])],
    *[chunk({'tool_calls': [{'index': 0, 'function': {'name': 'Read', 'arguments': value}}]})
      for value in ({'a': 1}, [], 0)],
    chunk({'reasoning_content': 'valid reasoning', 'content': [1]}),
    chunk({'content': 'valid text', 'tool_calls': [{'index': 0, 'function': {'arguments': {}}}]}),
])
def test_invalid_scalars_reject_before_changing_stream_state(malformed):
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    stream.start()
    with pytest.raises(Rejection) as caught:
        stream.feed(malformed)
    assert caught.value.status == 502
    assert caught.value.error_type == 'api_error'
    assert stream.open is None
    assert stream.index == 0
    assert stream.tools == {}
    assert stream.finish_reason is None
    assert stream._upstream_id == ''
    result = events(stream.fail(caught.value.error_type, caught.value.message))
    assert [event['type'] for event in result] == ['error']


def test_delta_checks_text_before_opening_a_block():
    stream = StreamTranslator(DIALECT, 'client-model', 'msg-test')
    with pytest.raises(Rejection):
        stream._delta('thinking', [1])
    assert stream.open is None
    assert stream.index == 0
