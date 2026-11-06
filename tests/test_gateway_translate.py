"""Gateway contracts at the pure codec boundary; no provider or account is used."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json

import pytest

from looper.agents.llmrec.dialects import (ChatDialect, DIALECTS, MidSystem, ReasoningReplay, default_dialect)
from looper.agents.llmrec.translate import (Rejection, RejectionCode, anthropic_usage, apply_cache_markers, decode_tool_id,
    encode_tool_id, error_response, from_chat, local_response, thinking_signature, to_chat)
from looper.agents.runtimes import VendorType


def request(dialect='generic', **fields):
    body = {'model': 'client', 'messages': [{'role': 'user', 'content': 'hello'}], **fields}
    result = to_chat(body, DIALECTS[ChatDialect(dialect)], {'client': 'upstream'}, session_id='session')
    return json.loads(result.body), result


def assistant(content):
    return {'role': 'assistant', 'content': content}


def think(text='reason', signature=None):
    return {'type': 'thinking', 'thinking': text, 'signature': signature or thinking_signature(text)}


def chat_response(message=None, reason='stop', usage=None):
    return {'id': 'completion', 'choices': [{'message': message or {'content': 'answer'}, 'finish_reason': reason}],
            'usage': usage}


@pytest.mark.parametrize('system,expected', [
    (' a\n b ', ' a\n b '),
    ([{'type': 'text', 'text': 'x-anthropic-billing-header: varies'},
      {'type': 'text', 'text': 'first', 'cache_control': {'type': 'ephemeral'}},
      {'type': 'text', 'text': 'second'}], 'first\n\nsecond'),
    ([{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'x-anthropic-billing-header: keep'}],
     'first\n\nx-anthropic-billing-header: keep'),
    ([], None), ([{'type': 'text', 'text': ''}], None),
])
def test_system_and_first_attribution(system, expected):
    body, _ = request(system=system)
    assert body['messages'][:-1] == ([] if expected is None else [{'role': 'system', 'content': expected}])


@pytest.mark.parametrize('content,expected', [
    (' text ', ' text '),
    ([{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}], 'a\n\nb'),
    ([{'type': 'document', 'source': {}}], '[document omitted: not supported through the gateway]'),
    ([{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.org/a.png'}}],
     '[image omitted: this model does not accept images]'),
])
def test_user_content(content, expected):
    body, _ = request(messages=[{'role': 'user', 'content': content}])
    assert body['messages'] == [{'role': 'user', 'content': expected}]


@pytest.mark.parametrize('dialect,source,expected', [
    ('openai', {'type': 'base64', 'media_type': 'image/png', 'data': 'YWJj'}, 'data:image/png;base64,YWJj'),
    ('openai', {'type': 'url', 'url': 'https://example.org/a.png'}, 'https://example.org/a.png'),
    ('moonshot', {'type': 'base64', 'media_type': 'image/png', 'data': 'YWJj'}, 'data:image/png;base64,YWJj'),
])
def test_images(dialect, source, expected):
    body, _ = request(dialect, messages=[{'role': 'user', 'content': [
        {'type': 'text', 'text': 'look'}, {'type': 'image', 'source': source}]}])
    assert body['messages'][0]['content'] == [{'type': 'text', 'text': 'look'},
        {'type': 'image_url', 'image_url': {'url': expected}}]


def test_moonshot_url_image_is_omitted():
    body, result = request('moonshot', messages=[{'role': 'user', 'content': [
        {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.org/a.png'}}]}])
    assert body['messages'][0]['content'] == '[image omitted: this model does not accept images]'
    assert result.notes['dropped']['image'] == 1


@pytest.mark.parametrize('dialect,with_name', [('generic', False), ('moonshot', True)])
@pytest.mark.parametrize('result_content,is_error,expected', [
    ('ok', False, 'ok'), ('bad', True, '[error] bad'),
    ('<tool_use_error>bad</tool_use_error>', True, '<tool_use_error>bad</tool_use_error>'),
    ([{'type': 'text', 'text': 'a'}, {'type': 'image', 'source': {}}, {'type': 'document'}], False,
     'a\n\n[image omitted: tool results cannot carry images]\n\n[document omitted: not supported through the gateway]'),
])
def test_tool_result_split_and_name(dialect, with_name, result_content, is_error, expected):
    tool_id = encode_tool_id('functions.Bash:0')
    body, _ = request(dialect, messages=[assistant([{'type': 'tool_use', 'id': tool_id, 'name': 'Bash', 'input': {}}]),
        {'role': 'user', 'content': [{'type': 'text', 'text': 'after'},
            {'type': 'tool_result', 'tool_use_id': tool_id, 'content': result_content, 'is_error': is_error}]}])
    expected_tool = {'role': 'tool', 'tool_call_id': 'functions.Bash:0', 'content': expected}
    if with_name:
        expected_tool['name'] = 'Bash'
    assert body['messages'][1:] == [expected_tool, {'role': 'user', 'content': 'after'}]


def test_tool_result_name_missing_is_rejected():
    with pytest.raises(Rejection, match='tool name') as error:
        request('moonshot', messages=[{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'missing', 'content': 'output'}]}])
    assert error.value.status == 400


@pytest.mark.parametrize('content', [
    [{'type': 'tool_reference', 'tool_name': 'Bash'}],
    [{'type': 'tool_result', 'tool_use_id': 'id', 'content': [{'type': 'tool_reference'}]}],
])
def test_tool_reference_names_tool_search(content):
    with pytest.raises(Rejection, match='tool search'):
        request(messages=[{'role': 'user', 'content': content}])


@pytest.mark.parametrize('tag', ['web_search_20250305', 'bash_20250124', 'text_editor_20250429', 'computer_20250124', 'advisor'])
def test_server_tools_are_rejected(tag):
    with pytest.raises(Rejection, match=f"Input tag '{tag}' is not supported through the Looper gateway"):
        request(tools=[{'type': tag, 'name': 'server'}])


def test_tool_order_flags_and_argument_key_order():
    body, result = request(tools=[{'name': 'Z', 'input_schema': {'type': 'object'}, 'strict': True},
        {'type': 'custom', 'name': 'A', 'description': 'desc', 'input_schema': {}, 'defer_loading': True}],
        messages=[assistant([{'type': 'tool_use', 'id': 'call', 'name': 'Z', 'input': {'z': '中', 'a': 1}}])])
    assert [t['function']['name'] for t in body['tools']] == ['Z', 'A']
    assert body['tools'][0] == {'type': 'function', 'function': {'name': 'Z', 'parameters': {'type': 'object'}}}
    assert body['messages'][0]['tool_calls'][0]['function']['arguments'] == '{"z": "中", "a": 1}'
    assert result.body.startswith(b'{"model":"upstream","messages":[')


@pytest.mark.parametrize('dialect,choice,thinking,expected,degraded', [
    ('generic', {'type': 'auto'}, None, 'auto', False),
    ('generic', {'type': 'any'}, None, 'required', False),
    ('generic', {'type': 'none'}, None, 'none', False),
    ('generic', {'type': 'tool', 'name': 'Bash'}, None, {'type': 'function', 'function': {'name': 'Bash'}}, False),
    ('zhipu', {'type': 'tool', 'name': 'Bash'}, None, 'auto', True),
    ('zhipu', {'type': 'none'}, None, 'auto', True),
    ('qwen', {'type': 'any'}, None, 'auto', True),
    ('moonshot', {'type': 'tool', 'name': 'Bash'}, {'type': 'adaptive'}, 'required', True),
    ('moonshot', {'type': 'tool', 'name': 'Bash'}, {'type': 'disabled'}, {'type': 'function', 'function': {'name': 'Bash'}}, False),
])
def test_tool_choice_degradation(dialect, choice, thinking, expected, degraded):
    fields = {'tool_choice': choice}
    if thinking is not None:
        fields['thinking'] = thinking
    body, result = request(dialect, **fields)
    assert body['tool_choice'] == expected
    assert bool(result.notes['degraded'].get('tool_choice')) is degraded


@pytest.mark.parametrize('supports', [True, False])
def test_disable_parallel_tool_use(supports):
    dialect = replace(DIALECTS[ChatDialect.GENERIC], parallel_tool_calls=supports)
    result = to_chat({'model': 'client', 'messages': [], 'tool_choice': {
        'type': 'auto', 'disable_parallel_tool_use': True}}, dialect, {'client': 'upstream'}, session_id=None)
    assert json.loads(result.body).get('parallel_tool_calls') is (False if supports else None)


@pytest.mark.parametrize('dialect,limit', [('openai', 4), ('deepseek', 16), ('moonshot', 5), ('zhipu', 1)])
def test_stop_limit(dialect, limit):
    assert request(dialect, stop_sequences=['x'] * limit)[0]['stop'] == ['x'] * limit
    with pytest.raises(Rejection, match='stop'):
        request(dialect, stop_sequences=['x'] * (limit + 1))


@pytest.mark.parametrize('dialect,field,cap', [('generic', 'max_tokens', None),
    ('openai', 'max_completion_tokens', None), ('deepseek', 'max_tokens', 393216),
    ('zhipu', 'max_tokens', 131072), ('moonshot', 'max_completion_tokens', None),
    ('qwen', 'max_completion_tokens', None)])
def test_max_tokens_field_and_clamp(dialect, field, cap):
    body, result = request(dialect, max_tokens=500000)
    assert body[field] == (cap or 500000)
    assert result.notes['clamped'].get('max_tokens', 0) == int(cap is not None)


@pytest.mark.parametrize('dialect,on,off', [
    ('deepseek', {'thinking': {'type': 'enabled'}}, {'thinking': {'type': 'disabled'}}),
    ('zhipu', {'thinking': {'type': 'enabled', 'clear_thinking': False}}, {'thinking': {'type': 'disabled'}}),
    ('qwen', {'enable_thinking': True}, {'enable_thinking': False}),
    ('vllm', {'chat_template_kwargs': {'enable_thinking': True}}, {'chat_template_kwargs': {'enable_thinking': False}}),
])
@pytest.mark.parametrize('mode', ['adaptive', 'enabled', 'disabled', None])
def test_thinking_follows_client(dialect, on, off, mode):
    body, _ = request(dialect, **({'thinking': {'type': mode}} if mode else {}))
    for key in on:
        if mode is None:
            assert key not in body
        else:
            assert body[key] == (off if mode == 'disabled' else on)[key]


@pytest.mark.parametrize('dialect,effort,expected', [('deepseek', 'medium', 'high'), ('deepseek', 'xhigh', 'high'),
    ('deepseek', 'minimal', 'low'), ('deepseek', 'max', 'max'), ('moonshot', 'medium', 'high'),
    ('openai', 'medium', 'medium'), ('generic', 'high', None)])
def test_effort(dialect, effort, expected):
    body, _ = request(dialect, output_config={'effort': effort, 'format': {}})
    assert body.get('reasoning_effort') == expected


@pytest.mark.parametrize('dialect,thinking,temperature,expected', [('generic', None, 2.5, 2.5),
    ('zhipu', None, -1, 0.01), ('qwen', None, 3, 1.99), ('moonshot', None, 0.5, None),
    ('deepseek', {'type': 'enabled'}, 0.5, None), ('openai', {'type': 'adaptive'}, 0.5, None)])
def test_temperature_rejections_and_clamp(dialect, thinking, temperature, expected):
    body, _ = request(dialect, temperature=temperature, **({'thinking': thinking} if thinking else {}))
    assert body.get('temperature') == expected


@pytest.mark.parametrize('dialect', ['generic', 'openai', 'deepseek', 'moonshot', 'vllm'])
def test_cache_affinity_and_privacy(dialect):
    body, _ = request(dialect, metadata={'user_id': 'private_session_metadata-session'})
    expected = 'lg-' + hashlib.sha256(b'session').hexdigest()[:32]
    assert body.get('prompt_cache_key') == (expected if dialect in ('openai', 'moonshot') else None)
    assert 'user' not in body
    result = to_chat({'model': 'client', 'messages': [], 'metadata': {'user_id': 'private_session_metadata-session'}},
        DIALECTS[ChatDialect(dialect)], {'client': 'upstream'}, session_id=None)
    assert json.loads(result.body).get('prompt_cache_key') == (
        'lg-' + hashlib.sha256(b'metadata-session').hexdigest()[:32] if dialect in ('openai', 'moonshot') else None)


def test_user_is_hashed_only_for_explicit_dialect():
    result = to_chat({'model': 'client', 'messages': [], 'metadata': {'user_id': 'private'}},
        replace(DIALECTS[ChatDialect.GENERIC], send_user=True), {'client': 'upstream'}, session_id=None)
    assert json.loads(result.body)['user'] == hashlib.sha256(b'private').hexdigest()[:32]


@pytest.mark.parametrize('mode,expected_role', [(MidSystem.USER, 'user'), (MidSystem.SYSTEM, 'system')])
def test_mid_system_and_empty_entries(mode, expected_role):
    result = to_chat({'model': 'client', 'messages': [{'role': 'system', 'content': []},
        {'role': 'system', 'content': [{'type': 'text', 'text': ''}]}, {'role': 'system', 'content': 'note'}]},
        replace(DIALECTS[ChatDialect.GENERIC], mid_system=mode), {'client': 'upstream'}, session_id=None)
    assert json.loads(result.body)['messages'] == [{'role': expected_role, 'content': 'note'}]


@pytest.mark.parametrize('replay,expected', [(ReasoningReplay.ALL, 'reason'), (ReasoningReplay.NONE, None),
    (ReasoningReplay.TOOL_LOOP, None)])
def test_reasoning_replay(replay, expected):
    dialect = replace(DIALECTS[ChatDialect.GENERIC], reasoning_replay=replay)
    result = to_chat({'model': 'client', 'messages': [assistant([think(), {'type': 'text', 'text': 'answer'}]),
        {'role': 'user', 'content': 'next'}]}, dialect, {'client': 'upstream'}, session_id=None)
    assert json.loads(result.body)['messages'][0].get('reasoning_content') == expected


@pytest.mark.parametrize('signature', ['foreign', 'lgw1.t.wrong-digest', 'lgw1.future.payload', ''])
def test_signatures_never_reject_and_foreign_are_dropped(signature):
    body, _ = request('deepseek', messages=[assistant([{'type': 'thinking', 'thinking': 'reason', 'signature': signature}])])
    assert body['messages'][0]['reasoning_content'] == ('reason' if signature.startswith('lgw1.') else '')


def test_adjacent_thinking_text_and_tools_preserve_order():
    body, _ = request(messages=[assistant([think('a'), think('b'), {'type': 'text', 'text': 'x'},
        {'type': 'redacted_thinking', 'data': 'private'}, {'type': 'text', 'text': 'y'}])])
    assert body['messages'][0] == {'role': 'assistant', 'content': 'xy', 'reasoning_content': 'ab'}


@pytest.mark.parametrize('field', ['top_k', 'context_management', 'mcp_servers', 'container', 'service_tier'])
def test_unsupported_top_level_is_observed(field):
    body, result = request(**{field: 1})
    assert field not in body
    assert result.notes['dropped'][field] == 1


def test_cache_marker_hook_is_identity():
    messages = [{'role': 'user', 'content': 'hi'}]
    before = json.dumps(messages).encode()
    assert apply_cache_markers(messages, [], DIALECTS[ChatDialect.GENERIC]) is messages
    assert json.dumps(messages).encode() == before


@pytest.mark.parametrize('tool_id', ['call_1', 'functions.Bash:0', '中文工具', 'tlx_YQ', '', 'x' * 129])
def test_tool_id_roundtrip(tool_id):
    assert decode_tool_id(encode_tool_id(tool_id)) == tool_id
    if tool_id == 'call_1':
        assert encode_tool_id(tool_id) == tool_id


@pytest.mark.parametrize('finish,expected', [('stop', 'end_turn'), ('length', 'max_tokens'),
    ('tool_calls', 'tool_use'), ('function_call', 'tool_use'), ('content_filter', 'refusal')])
def test_response_finish_reason(finish, expected):
    result = from_chat(chat_response(reason=finish), client_model='client', dialect=DIALECTS[ChatDialect.GENERIC])
    assert result['stop_reason'] == expected
    assert result['model'] == 'client'
    assert result['id'] == 'msg_lgw_' + hashlib.sha256(b'completion').hexdigest()[:24]
    assert result['content'] == [{'type': 'text', 'text': 'answer'}]


def test_response_reasoning_and_tool_id_and_choice_zero():
    raw = chat_response({'reasoning_content': 'reason', 'content': 'answer', 'tool_calls': [
        {'id': 'functions.Bash:0', 'function': {'name': 'Bash', 'arguments': '{"z": 1, "a": "中"}'}}]})
    raw['choices'].append({'message': {'content': 'ignore'}, 'finish_reason': 'length'})
    result = from_chat(raw, client_model='client', dialect=DIALECTS[ChatDialect.MOONSHOT])
    assert result['content'] == [think(), {'type': 'text', 'text': 'answer'},
        {'type': 'tool_use', 'id': encode_tool_id('functions.Bash:0'), 'name': 'Bash', 'input': {'z': 1, 'a': '中'}}]
    assert result['stop_reason'] == 'tool_use'


@pytest.mark.parametrize('arguments', ['{broken', '[1]', 'null', '1', '"string"', '{"nested": {"x": NaN}}', 0, False, []])
def test_invalid_tool_arguments(arguments):
    with pytest.raises(Rejection, match='upstream returned invalid tool arguments') as error:
        from_chat(chat_response({'tool_calls': [{'id': 'call', 'function': {'name': 'Bash', 'arguments': arguments}}]}),
            client_model='client', dialect=DIALECTS[ChatDialect.GENERIC])
    assert error.value.status == 502
    assert error.value.error_type == 'api_error'
    assert error.value.code is RejectionCode.TOOL_ARGS_INVALID


def test_empty_tool_arguments_and_missing_id_are_stable():
    raw = chat_response({'tool_calls': [{'function': {'name': 'Bash', 'arguments': ''}}]})
    result = from_chat(raw, client_model='client', dialect=DIALECTS[ChatDialect.GENERIC])
    assert result['content'][0]['input'] == {}
    assert result['content'][0]['id'] == 'call_' + hashlib.sha256(b'completion:0').hexdigest()[:24]


def test_insufficient_system_resource():
    with pytest.raises(Rejection) as error:
        from_chat(chat_response(reason='insufficient_system_resource'), client_model='client', dialect=DIALECTS[ChatDialect.DEEPSEEK])
    assert (error.value.status, error.value.error_type) == (529, 'overloaded_error')


@pytest.mark.parametrize('dialect,usage,expected', [
    ('deepseek', {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 70, 'prompt_cache_miss_tokens': 30,
                  'completion_tokens': 12}, (30, 0, 70, 12)),
    ('openai', {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 50, 'cache_write_tokens': 20},
               'completion_tokens': 12}, (30, 20, 50, 12)),
    ('moonshot', {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 50, 'cache_write_tokens': 10},
                 'completion_tokens': 12}, (40, 10, 50, 12)),
    ('generic', {'prompt_tokens': 100, 'completion_tokens': 12}, (100, 0, 0, 12)),
    ('deepseek', {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 0,
                  'prompt_tokens_details': {'cached_tokens': 10}}, (20, 0, 0, 0)),
    ('generic', None, (0, 0, 0, 0)),
])
def test_usage_mapping(dialect, usage, expected):
    result = anthropic_usage(usage, DIALECTS[ChatDialect(dialect)])
    assert tuple(result[field] for field in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')) == expected


@pytest.mark.parametrize('upstream,code,status,error_type', [
    (400, None, 400, 'invalid_request_error'), (422, None, 400, 'invalid_request_error'),
    (400, 'context_length_exceeded', 400, 'invalid_request_error'),
    (401, None, 401, 'authentication_error'), (402, None, 403, 'permission_error'),
    (403, None, 403, 'permission_error'), (404, None, 404, 'not_found_error'),
    (413, None, 413, 'request_too_large'), (429, None, 429, 'rate_limit_error'),
    (429, 'insufficient_quota', 403, 'permission_error'), (503, None, 529, 'overloaded_error'),
    (529, None, 529, 'overloaded_error'), (408, None, 408, 'api_error'),
    (500, None, 500, 'api_error'), (502, None, 502, 'api_error'), (504, None, 504, 'api_error'),
])
def test_error_mapping(upstream, code, status, error_type):
    actual_status, headers, raw = error_response(upstream, json.dumps({'error': {'message': 'provider message',
        'code': code}}).encode(), {'x-request-id': 'request', 'x-should-retry': 'true'}, DIALECTS[ChatDialect.GENERIC])
    body = json.loads(raw)
    assert actual_status == status
    assert body['error']['type'] == error_type
    assert body['request_id'] == 'request'
    assert headers['x-should-retry'] == ('false' if upstream == 402 or code == 'insufficient_quota' else 'true')
    assert body['error']['message'] == ('prompt is too long: capability_rejected: prompt_too_long (upstream: provider message)'
        if code == 'context_length_exceeded' else 'provider message')


@pytest.mark.parametrize('retry', ['7', format_datetime(datetime.now(timezone.utc) + timedelta(seconds=45), usegmt=True)])
def test_retry_after_seconds(retry):
    _, headers, _ = error_response(429, b'not json', {'Retry-After': retry}, DIALECTS[ChatDialect.GENERIC])
    assert headers['retry-after'].isdigit()
    assert int(headers['retry-after']) == 7 if retry == '7' else 0 < int(headers['retry-after']) <= 45


def test_unknown_model_and_invalid_body():
    with pytest.raises(Rejection) as error:
        to_chat({'model': 'unknown', 'messages': []}, DIALECTS[ChatDialect.GENERIC], {'client': 'upstream'}, session_id=None)
    assert (error.value.status, error.value.error_type) == (404, 'not_found_error')
    with pytest.raises(Rejection) as error:
        to_chat([], DIALECTS[ChatDialect.GENERIC], {'client': 'upstream'}, session_id=None)
    assert error.value.status == 400


@pytest.mark.parametrize('vendor,expected', [(v, 'deepseek' if v is VendorType.DEEPSEEK else
    'moonshot' if v in (VendorType.KIMI_MOONSHOT, VendorType.KIMI_MOONSHOT_CN) else
    'zhipu' if v in (VendorType.GLM_BIGMODEL, VendorType.GLM_ZAI) else 'generic') for v in VendorType])
def test_default_dialect(vendor, expected):
    assert default_dialect(vendor).value == expected


@pytest.mark.parametrize('method,path,status', [('HEAD', '/api/hello', 200), ('GET', '/api/hello', 200), ('GET', '/v1/models', 200),
    ('DELETE', '/v1/messages', 404), ('GET', '/unknown', 404),
    ('POST', '/v1/messages/count_tokens', 404)])
def test_local_endpoints(method, path, status):
    route = type('Route', (), {'models': {'client': 'upstream'}, 'created_at': '2026-01-01T00:00:00Z'})()
    result = local_response(method, path, route)
    assert result[0] == status
    if path == '/v1/models':
        assert json.loads(result[2])['data'][0]['id'] == 'client'
        assert json.loads(result[2])['data'][0]['created_at'] == route.created_at
    assert local_response('POST', '/v1/messages', route) is None


@pytest.mark.parametrize('name', [None, 1, '', [], {}])
def test_named_tool_choice_requires_name(name):
    with pytest.raises(Rejection) as error:
        request(tool_choice={'type': 'tool', **({'name': name} if name is not None else {})})
    assert error.value.status == 400


@pytest.mark.parametrize('url', ['https://example.org/api?token=private', 'http://example.org/path?secret=private#fragment'])
def test_error_message_drops_url_query(url):
    _, _, raw = error_response(400, json.dumps({'error': {'message': 'Failed at ' + url}}).encode(),
        {}, DIALECTS[ChatDialect.GENERIC])
    assert json.loads(raw)['error']['message'] == 'Failed at ' + url.split('?')[0]
    assert b'private' not in raw
