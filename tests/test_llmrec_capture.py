import gzip
import json

import pytest

from looper.agents.llmrec.capture import (assemble, build_capture, decode, join_request, redact_headers, redact_query,
                                        split_request, tokens)


def sse(*events):
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


def test_header_allowlist_and_deny_backstop():
    result = redact_headers([('Authorization', 'Bearer secret'), ('x-api-key', 'secret'), ('cookie', 'secret'),
        ('x-stainless-auth', 'secret'), ('x-stainless-lang', 'python'), ('Content-Type', 'application/json'),
        ('Anthropic-Beta', 'one'), ('Anthropic-Beta', 'two'), ('unknown', 'private')])
    for name in ('authorization', 'x-api-key', 'cookie', 'x-stainless-auth', 'unknown'):
        assert result[name] == '<redacted>'
    assert result['x-stainless-lang'] == 'python'
    assert result['content-type'] == 'application/json'
    assert result['anthropic-beta'] == 'one, two'
    result = redact_headers([('x-ratelimit-remaining', '1'), ('x-ratelimit-secret', 'private'), ('set-cookie', 'private')], response=True)
    assert result == {'x-ratelimit-remaining': '1', 'x-ratelimit-secret': '<redacted>', 'set-cookie': '<redacted>'}
    assert redact_query('/v1/messages?api_key=private&beta=ok') == '/v1/messages?api_key=<redacted>&beta=ok'


@pytest.mark.parametrize('path,body', [
    ('/v1/messages', {'system': 'system', 'tools': [{'name': 'read'}], 'messages': [{'role': 'user', 'content': 'Hi'}]}),
    ('/v1/messages/count_tokens', {'system': [], 'messages': [{'content': 'Hi'}]}),
    ('/responses', {'instructions': 'system', 'tools': [], 'input': [{'type': 'message', 'content': 'Hi'}]}),
    ('/responses', {'input': 'Hi'}),
    ('/chat/completions', {'tools': [], 'messages': [{'content': {'$llmrec_part': 'literal'}}]}),
    ('/other', {'arbitrary': {'$llmrec_part': 'literal'}}),
])
def test_split_join_roundtrip(path, body):
    skeleton, parts = split_request(body, path)
    assert join_request(skeleton, parts, path) == body


def test_shared_messages_share_hashes():
    first = {'messages': [{'role': 'user', 'content': 'shared'}]}
    second = {'messages': first['messages'] + [{'role': 'assistant', 'content': 'new'}]}
    _, a = split_request(first, '/v1/messages')
    _, b = split_request(second, '/v1/messages')
    assert set(a) <= set(b)


def test_anthropic_text_tools_thinking_and_usage():
    result = assemble('/v1/messages', 'text/event-stream', sse(
        {'type': 'message_start', 'message': {'id': 'msg', 'usage': {'input_tokens': 7}, 'content': []}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hello'}},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 't', 'name': 'read', 'input': {}}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"x":1}'}},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'thinking_delta', 'thinking': 'reason'}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'signature_delta', 'signature': 'sig'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
        {'type': 'message_stop'}))
    assert result.gap is None
    assert result.body['content'] == [{'type': 'text', 'text': 'Hello'},
        {'type': 'tool_use', 'id': 't', 'name': 'read', 'input': {'x': 1}},
        {'type': 'thinking', 'thinking': 'reason', 'signature': 'sig'}]
    assert tokens('/v1/messages', result.body)[:2] == (7, 3)


def test_responses_and_chat_usage():
    response = {'id': 'r', 'output': [], 'usage': {'input_tokens': 10, 'output_tokens': 2,
                'input_tokens_details': {'cached_tokens': 4, 'cache_write_tokens': 0}}}
    assert assemble('/responses', 'text/event-stream', sse({'type': 'response.completed', 'response': response})).body == response
    assert tokens('/responses', response)[:4] == (6, 2, 4, 0)
    result = assemble('/chat/completions', 'text/event-stream', sse(
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 't', 'function': {'name': 'read', 'arguments': '{'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 3}}}))
    assert result.body['choices'][0]['message']['tool_calls'][0]['function'] == {'name': 'read', 'arguments': '{}'}
    assert tokens('/chat/completions', result.body)[:4] == (7, 2, 3, 0)
    assert tokens('/chat/completions', {'usage': {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 12,
        'prompt_cache_miss_tokens': 8, 'completion_tokens': 4}})[:4] == (8, 4, 12, 0)


def test_decode_and_bad_stream():
    data = b'hello'
    assert decode(gzip.compress(data), 'gzip') == data
    assert decode(data, 'unknown') is None
    assert assemble('/v1/messages', 'text/event-stream', b'data: invalid\n\n').gap == 'unparsable_stream'


def test_route_file_validation_and_private_modes(tmp_path):
    from datetime import datetime, timezone
    from dataclasses import replace
    import stat
    from looper.agents.llmrec import ROUTES_DIR, RouteFile
    route = RouteFile(id='a' * 22, agent_id='worker-test', upstream='https://example.test',
                      created_at=datetime.now(timezone.utc).isoformat(),
                      proxy_env={'HTTPS_PROXY': 'http://proxy.example:8080', 'NO_PROXY': '.internal.example'},
                      tls_env={'NODE_EXTRA_CA_CERTS': 'certs/custom.pem'})
    path = tmp_path / ROUTES_DIR / f'{route.id}.json'
    route.write(path)
    assert RouteFile.read(path) == route
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.parent.stat().st_mode) == 0o700
    for invalid in (replace(route, format=route.format + 1), replace(route, upstream='https://user:secret@example.test'),
                    replace(route, created_at='invalid'), replace(route, id='../invalid'), replace(route, record_id=True),
                    replace(route, proxy_env=[]), replace(route, proxy_env={'HTTPS_PROXY': 1}),
                    replace(route, proxy_env={'ANTHROPIC_API_KEY': 'secret'}),
                    replace(route, tls_env=[]), replace(route, tls_env={'NODE_EXTRA_CA_CERTS': 1}),
                    replace(route, tls_env={'ANTHROPIC_API_KEY': 'secret'})):
        invalid.write(path)
        assert RouteFile.read(path) is None


def test_malformed_usage_keeps_raw_usage_without_bad_counts():
    usage = {'input_tokens': 'invalid', 'output_tokens': True, 'input_tokens_details': None}
    assert tokens('/responses', {'usage': usage}) == (None, None, None, None, usage)
    usage = {'prompt_tokens': -1, 'prompt_cache_hit_tokens': 'invalid', 'prompt_tokens_details': None}
    assert tokens('/chat/completions', {'usage': usage}) == (None, None, None, 0, usage)


@pytest.mark.parametrize('details,expected', [
    ({}, (None, None, None)),
    ({'cached_tokens': 0}, (None, 0, None)),
    ({'cache_write_tokens': 0}, (None, None, 0)),
    ({'cached_tokens': 0, 'cache_write_tokens': 0}, (10, 0, 0)),
    ({'cached_tokens': 3, 'cache_write_tokens': 2}, (5, 3, 2)),
    ({'cached_tokens': 'invalid', 'cache_write_tokens': 0}, (None, None, 0)),
    ({'cached_tokens': 0, 'cache_write_tokens': True}, (None, 0, None)),
])
def test_responses_unknown_cache_fields_are_not_zero(details, expected):
    result = tokens('/responses', {'usage': {'input_tokens': 10, 'output_tokens': 2,
                                           'input_tokens_details': details}})
    assert (result[0], result[2], result[3]) == expected
    assert result[1] == 2


@pytest.mark.parametrize('usage', [None, [], 'invalid', 0, True])
def test_invalid_usage_is_unknown(usage):
    assert tokens('/responses', {'usage': usage}) == (None, None, None, None, None)


def test_other_large_request_has_explicit_gap():
    body = json.dumps({'payload': 'x' * (64 * 1024)}).encode()
    capture = build_capture(request_body=body, response_data=b'{}', path='/other')
    assert capture.skeleton is None
    assert capture.parts == {}
    assert capture.request_bytes == len(body)
    assert capture.record_gap == 'request_too_large'


@pytest.mark.parametrize('method', ['HEAD', 'GET'])
def test_bodyless_non_model_request_is_skipped(method):
    from looper.agents.llmrec import RouteFile

    route = RouteFile(model='sonnet')
    assert build_capture(request_body=b'', response_data=b'', route=route,
                         method=method, path='/api/hello', kind='other') is None
    assert route.model == 'sonnet'


@pytest.mark.parametrize('path', ['/v1/messages', '/v1/messages/count_tokens', '/v1/responses'])
def test_empty_model_request_still_records_gap(path):
    capture = build_capture(request_body=b'', response_data=b'{}', path=path)
    assert capture.record_gap == 'unparsable_request'


@pytest.mark.parametrize('explicit_miss', [False, True])
def test_chat_cache_split_preserves_prompt_total(explicit_miss):
    usage = {'prompt_tokens': 100, 'completion_tokens': 7,
             'prompt_tokens_details': {'cached_tokens': 40, 'cache_write_tokens': 10}}
    if explicit_miss:
        usage['prompt_cache_miss_tokens'] = 60
    capture = build_capture(request_body=b'{"model":"client","messages":[]}',
                            response_data=json.dumps({'usage': usage}).encode(), path='/v1/chat/completions')
    assert (capture.input_tokens, capture.cache_read_tokens, capture.cache_write_tokens) == (50, 40, 10)
    assert capture.input_tokens + capture.cache_read_tokens + capture.cache_write_tokens == usage['prompt_tokens']
