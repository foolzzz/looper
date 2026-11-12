"""Stateless Anthropic Messages ↔ Chat Completions codecs.

Cache stability contracts:
1. Requests are pure functions of their input, dialect, model map and session.
2. A historical message is independent of its position (except explicit TOOL_LOOP).
3. JSON uses fixed insertion order and compact separators, never sorted keys.
4. No request timestamps, random ids or other per-request variables are injected.
5. Unsupported metadata is discarded without rewriting the accompanying text.
6. System, tool, message and content order is preserved.
7. Tool inputs use json.dumps(input, ensure_ascii=False), preserving key order.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .dialects import CacheDialect, ChatDialect, DIALECTS, Dialect, ReasoningReplay


class RejectionCode(StrEnum):
    TOOL_ARGS_INVALID = 'tool_args_invalid'


class Rejection(Exception):
    def __init__(self, status: int, error_type: str, message: str, *, code: RejectionCode | None = None):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.type = error_type
        self.message = message
        self.code = code


@dataclass(frozen=True)
class ChatRequest:
    body: bytes
    upstream_model: str
    stream: bool
    notes: dict


def _dialect(value: Dialect | ChatDialect | str) -> Dialect:
    return value if isinstance(value, Dialect) else DIALECTS[ChatDialect(value)]


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _bad(message: str) -> Rejection:
    return Rejection(400, 'invalid_request_error', message + ' through the Looper gateway')


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def thinking_signature(text: str) -> str:
    return 'lgw1.t.' + _b64(hashlib.sha256(text.encode()).digest())


def encode_tool_id(tool_id: str) -> str:
    # Escape the reserved prefix too: otherwise a legitimate upstream tlx_* id
    # would be decoded as a different id on the next request.
    if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tool_id) and not tool_id.startswith('tlx_'):
        return tool_id
    return 'tlx_' + _b64(tool_id.encode('utf-8'))


def decode_tool_id(tool_id: str) -> str:
    if not tool_id.startswith('tlx_'):
        return tool_id
    encoded = tool_id[4:]
    try:
        return base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return tool_id


def apply_cache_markers(chat_messages: list[dict], anthropic_messages: list[dict], dialect: Dialect) -> list[dict]:
    """Automatic prefix caching requires no markers and changes no bytes."""
    if dialect.cache is not CacheDialect.AUTOMATIC:
        raise ValueError('unsupported cache dialect')
    return chat_messages


def _count(notes: dict, category: str, field: str) -> None:
    counts = notes[category]
    counts[field] = counts.get(field, 0) + 1


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise _bad('invalid message content')
    return content


def _text(block: dict) -> str:
    text = block.get('text', '')
    if not isinstance(text, str):
        raise _bad('invalid text block')
    return text


def _content(content: Any, dialect: Dialect, notes: dict, *, tool_result: bool = False) -> str | list[dict]:
    if isinstance(content, str):
        return content
    blocks = _blocks(content)
    translated: list[dict] = []
    for block in blocks:
        tag = block.get('type')
        if tag == 'text':
            translated.append({'type': 'text', 'text': _text(block)})
        elif tag == 'tool_reference':
            raise _bad('tool search (tool_reference) is not supported')
        elif tag == 'document':
            translated.append({'type': 'text', 'text': '[document omitted: not supported through the gateway]'})
            _count(notes, 'dropped', 'document')
        elif tag == 'image':
            source = block.get('source', {})
            if not isinstance(source, dict):
                raise _bad('invalid image source')
            supported = (dialect.images and not tool_result and
                not (dialect.base64_images_only and source.get('type') != 'base64'))
            if supported and source.get('type') in ('base64', 'url'):
                url = (f"data:{source.get('media_type', '')};base64,{source.get('data', '')}"
                    if source.get('type') == 'base64' else source.get('url', ''))
                translated.append({'type': 'image_url', 'image_url': {'url': url}})
            else:
                placeholder = ('[image omitted: tool results cannot carry images]' if tool_result else
                    '[image omitted: this model does not accept images]')
                translated.append({'type': 'text', 'text': placeholder})
                _count(notes, 'dropped', 'image')
        else:
            _count(notes, 'dropped', str(tag))
    if any(block['type'] == 'image_url' for block in translated):
        return translated
    return '\n\n'.join(block['text'] for block in translated)


def _messages(body: dict, dialect: Dialect, notes: dict) -> list[dict]:
    result: list[dict] = []
    system = body.get('system', [])
    blocks = _blocks(system)
    if blocks and blocks[0].get('type') == 'text' and _text(blocks[0]).startswith('x-anthropic-billing-header:'):
        blocks = blocks[1:]
        _count(notes, 'dropped', 'attribution')
    system_text = '\n\n'.join(_text(block) for block in blocks if block.get('type') == 'text' and _text(block))
    if system_text:
        result.append({'role': 'system', 'content': system_text})
    messages = body.get('messages')
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise _bad('messages must be an array of objects')
    tool_names: dict[str, str] = {}
    last_user = -1
    if dialect.reasoning_replay is ReasoningReplay.TOOL_LOOP:
        for index, message in enumerate(messages):
            if message.get('role') == 'user' and not any(b.get('type') == 'tool_result' for b in _blocks(message.get('content', []))):
                last_user = index
    for index, message in enumerate(messages):
        role = message.get('role')
        content = message.get('content', [])
        blocks = _blocks(content)
        if role == 'system':
            text = _content(content, dialect, notes)
            if text:
                result.append({'role': dialect.mid_system.value, 'content': text})
        elif role == 'user':
            remainder = []
            for block in blocks:
                if block.get('type') != 'tool_result':
                    remainder.append(block)
                    continue
                tool_id = block.get('tool_use_id')
                if not isinstance(tool_id, str):
                    raise _bad('tool_result requires a tool_use_id')
                decoded = decode_tool_id(tool_id)
                tool = {'role': 'tool', 'tool_call_id': decoded}
                if dialect.tool_message_name:
                    if decoded not in tool_names:
                        raise _bad('cannot find tool name for tool_result')
                    tool['name'] = tool_names[decoded]
                text = _content(block.get('content', ''), dialect, notes, tool_result=True)
                if block.get('is_error') and '<tool_use_error>' not in text:
                    text = '[error] ' + text
                tool['content'] = text
                result.append(tool)
            if remainder or not blocks:
                result.append({'role': 'user', 'content': _content(remainder, dialect, notes)})
        elif role == 'assistant':
            text = ''.join(_text(block) for block in blocks if block.get('type') == 'text')
            item: dict = {'role': 'assistant', 'content': text or dialect.empty_assistant_content}
            reasoning = ''.join(block.get('thinking', '') for block in blocks if block.get('type') == 'thinking'
                and isinstance(block.get('signature'), str) and block['signature'].startswith('lgw1.'))
            replay = (dialect.reasoning_replay is ReasoningReplay.ALL or
                (dialect.reasoning_replay is ReasoningReplay.TOOL_LOOP and index > last_user))
            if dialect.reasoning_field and replay and (reasoning or dialect.reasoning_fill_empty):
                item[dialect.reasoning_field] = reasoning
            tools = []
            for block in blocks:
                if block.get('type') == 'tool_reference':
                    raise _bad('tool search (tool_reference) is not supported')
                if block.get('type') != 'tool_use':
                    continue
                tool_id, name, arguments = block.get('id'), block.get('name'), block.get('input')
                if not isinstance(tool_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                    raise _bad('invalid tool_use block')
                decoded = decode_tool_id(tool_id)
                tool_names[decoded] = name
                tools.append({'id': decoded, 'type': 'function', 'function': {'name': name,
                    'arguments': json.dumps(arguments, ensure_ascii=False, allow_nan=False)}})
            if tools:
                item['tool_calls'] = tools
            result.append(item)
        else:
            raise _bad('unsupported message role')
    return apply_cache_markers(result, messages, dialect)


def resolve_session_id(body: dict, session_id: str | None = None) -> str | None:
    """Prefer the client header, then Claude Code's metadata session suffix."""
    if session_id:
        return session_id
    metadata = body.get('metadata', {})
    user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
    if isinstance(user_id, str) and 'session_' in user_id:
        return user_id.split('session_', 1)[1]
    return None


def to_chat(body: dict, dialect: Dialect, models: Mapping[str, str], *, session_id: str | None) -> ChatRequest:
    dialect = _dialect(dialect)
    if not isinstance(body, dict):
        raise _bad('request JSON must be an object')
    model = body.get('model')
    if not isinstance(model, str) or model not in models:
        raise Rejection(404, 'not_found_error', f'model {model} is not served by this route through the Looper gateway')
    notes: dict = {'dropped': {}, 'degraded': {}, 'clamped': {}}
    try:
        result = _to_chat(body, dialect, models, session_id, notes)
        return ChatRequest(_json(result), models[model], result['stream'], notes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _bad('invalid request JSON values') from exc


def _to_chat(body: dict, dialect: Dialect, models: Mapping[str, str], session_id: str | None, notes: dict) -> dict:
    result: dict = {'model': models[body['model']], 'messages': _messages(body, dialect, notes)}
    if 'tools' in body:
        if not isinstance(body['tools'], list):
            raise _bad('tools must be an array')
        tools = []
        for tool in body['tools']:
            if not isinstance(tool, dict):
                raise _bad('invalid tool definition')
            tag = tool.get('type')
            if tag is not None and tag != 'custom':
                raise Rejection(400, 'invalid_request_error', f"Input tag '{tag}' is not supported through the Looper gateway")
            if not isinstance(tool.get('name'), str) or not isinstance(tool.get('input_schema'), dict):
                raise _bad('invalid tool definition')
            function = {'name': tool['name']}
            if 'description' in tool:
                function['description'] = tool['description']
            function['parameters'] = tool['input_schema']
            tools.append({'type': 'function', 'function': function})
        result['tools'] = tools
    thinking = body.get('thinking', {})
    if not isinstance(thinking, dict):
        raise _bad('thinking must be an object')
    thinking_on = thinking.get('type') in ('adaptive', 'enabled')
    choice = body.get('tool_choice')
    if choice is not None:
        if not isinstance(choice, dict) or choice.get('type') not in ('auto', 'none', 'any', 'tool'):
            raise _bad('invalid tool_choice')
        if choice['type'] == 'tool' and (not isinstance(choice.get('name'), str) or not choice['name']):
            raise _bad('named tool_choice requires a tool name')
        selected = {'auto': 'auto', 'none': 'none', 'any': 'required', 'tool': 'named'}[choice['type']]
        original = selected
        allowed = dialect.tool_choice
        if dialect.name is ChatDialect.MOONSHOT and thinking_on:
            allowed = allowed - {'named'}
        while selected not in allowed:
            selected = {'named': 'required', 'required': 'auto', 'none': 'auto'}.get(selected)
            if selected is None:
                raise _bad('tool_choice is not supported')
        if selected != original:
            _count(notes, 'degraded', 'tool_choice')
        result['tool_choice'] = ({'type': 'function', 'function': {'name': choice['name']}}
            if selected == 'named' else selected)
        if choice.get('disable_parallel_tool_use') and dialect.parallel_tool_calls:
            result['parallel_tool_calls'] = False
    stream = body.get('stream', False)
    if not isinstance(stream, bool):
        raise _bad('stream must be a boolean')
    result['stream'] = stream
    if stream and dialect.include_usage:
        result['stream_options'] = {'include_usage': True}
    if 'max_tokens' in body:
        tokens = body['max_tokens']
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise _bad('max_tokens must be a positive integer')
        if dialect.max_output_cap is not None and tokens > dialect.max_output_cap:
            tokens = dialect.max_output_cap
            _count(notes, 'clamped', 'max_tokens')
        result[dialect.max_tokens_field] = tokens
    rejected = dialect.rejects | (dialect.rejects_when_thinking if thinking_on else frozenset())
    for field in ('temperature', 'top_p', 'presence_penalty', 'frequency_penalty'):
        if field not in body:
            continue
        if field in rejected:
            _count(notes, 'dropped', field)
            continue
        value = body[field]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise _bad(f'{field} must be a finite number')
        if field == 'temperature' and dialect.temperature_range:
            value = min(dialect.temperature_range[1], max(dialect.temperature_range[0], value))
            if value != body[field]:
                _count(notes, 'clamped', field)
        result[field] = value
    if 'stop_sequences' in body:
        stop = body['stop_sequences']
        if not isinstance(stop, list) or any(not isinstance(item, str) for item in stop):
            raise _bad('stop_sequences must be an array of strings')
        if dialect.max_stop is not None and len(stop) > dialect.max_stop:
            raise _bad(f'stop_sequences exceeds the maximum of {dialect.max_stop}')
        result['stop'] = stop
    fragment = dialect.thinking_on if thinking_on else dialect.thinking_off if thinking.get('type') == 'disabled' else None
    if fragment:
        result.update(fragment)
    output = body.get('output_config', {})
    if not isinstance(output, dict):
        raise _bad('output_config must be an object')
    if 'effort' in output:
        if dialect.effort_field:
            effort = output['effort']
            if not isinstance(effort, str):
                raise _bad('effort must be a string')
            if dialect.name in (ChatDialect.DEEPSEEK, ChatDialect.MOONSHOT):
                effort = {'minimal': 'low', 'medium': 'high', 'xhigh': 'high'}.get(effort, effort)
            elif dialect.name is ChatDialect.OPENAI:
                effort = {'xhigh': 'high', 'max': 'high'}.get(effort, effort)
            if effort not in dialect.effort_values:
                raise _bad('effort is not supported by the chat dialect')
            if effort != output['effort']:
                _count(notes, 'clamped', 'effort')
            result[dialect.effort_field] = effort
        else:
            _count(notes, 'dropped', 'effort')
    metadata = body.get('metadata', {})
    if not isinstance(metadata, dict):
        raise _bad('metadata must be an object')
    user_id = metadata.get('user_id')
    session_id = resolve_session_id(body, session_id)
    if dialect.prompt_cache_key and session_id:
        result['prompt_cache_key'] = 'lg-' + hashlib.sha256(session_id.encode()).hexdigest()[:32]
    if dialect.send_user and isinstance(user_id, str):
        result['user'] = hashlib.sha256(user_id.encode()).hexdigest()[:32]
    result.update(dialect.extra)
    known = {'model', 'messages', 'system', 'tools', 'tool_choice', 'stream', 'max_tokens', 'temperature',
        'top_p', 'presence_penalty', 'frequency_penalty', 'stop_sequences', 'thinking', 'output_config', 'metadata'}
    for field in body.keys() - known:
        _count(notes, 'dropped', field)
    for field in output.keys() - {'effort'}:
        _count(notes, 'dropped', 'output_config.' + field)
    return result


def _usage_value(raw: dict, path: str) -> Any:
    value = raw
    for field in path.split('.'):
        if not isinstance(value, dict) or field not in value:
            return None
        value = value[field]
    return value


def cache_fields_present(raw: dict | None, dialect: Dialect) -> bool:
    return isinstance(raw, dict) and (any(_usage_value(raw, field.value) is not None
        for field in _dialect(dialect).usage_fields)
        or _usage_value(raw, 'prompt_tokens_details.cache_write_tokens') is not None)


def anthropic_usage(raw: dict | None, dialect: Dialect) -> tuple[dict, bool]:
    """Return client usage and whether the upstream reported cache counts."""
    raw = raw if isinstance(raw, dict) else {}
    dialect = _dialect(dialect)
    cached = next((value for field in dialect.usage_fields if (value := _usage_value(raw, field.value)) is not None), 0)
    written = _usage_value(raw, 'prompt_tokens_details.cache_write_tokens') or 0
    def tokens(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    cached, written = tokens(cached), tokens(written)
    usage = {'input_tokens': max(0, tokens(raw.get('prompt_tokens')) - cached - written),
        'output_tokens': tokens(raw.get('completion_tokens'))}
    reported = cache_fields_present(raw, dialect)
    if reported:
        usage.update(cache_creation_input_tokens=written, cache_read_input_tokens=cached)
    return usage, reported


def _invalid_json_constant(value: str):
    raise ValueError('invalid JSON constant: ' + value)


def from_chat(response_json: dict, *, client_model: str, dialect: Dialect) -> dict:
    dialect = _dialect(dialect)
    try:
        choice = response_json['choices'][0]
        message = choice['message']
        if not isinstance(message, dict):
            raise ValueError('invalid message')
        finish = choice.get('finish_reason')
        if finish == 'insufficient_system_resource':
            raise Rejection(529, 'overloaded_error', 'upstream has insufficient system resources')
        content = []
        reasoning = message.get(dialect.reasoning_field) if dialect.reasoning_field else None
        if reasoning:
            if not isinstance(reasoning, str):
                raise ValueError('invalid reasoning')
            content.append({'type': 'thinking', 'thinking': reasoning, 'signature': thinking_signature(reasoning)})
        text = message.get('content')
        if text:
            if not isinstance(text, str):
                raise ValueError('invalid content')
            content.append({'type': 'text', 'text': text})
        refusal = message.get('refusal')
        if refusal:
            if not isinstance(refusal, str):
                raise ValueError('invalid refusal')
            content.append({'type': 'text', 'text': refusal})
        response_id = str(response_json.get('id', ''))
        tools = message.get('tool_calls') or []
        for index, call in enumerate(tools):
            try:
                arguments = call['function'].get('arguments', '')
                if not isinstance(arguments, str):
                    raise ValueError('tool arguments must be a JSON string')
                arguments = json.loads(arguments, parse_constant=_invalid_json_constant) if arguments else {}
                if not isinstance(arguments, dict):
                    raise ValueError('tool arguments must be an object')
            except (KeyError, TypeError, ValueError) as exc:
                raise Rejection(502, 'api_error', 'upstream returned invalid tool arguments',
                    code=RejectionCode.TOOL_ARGS_INVALID) from exc
            tool_id = call.get('id') or 'call_' + hashlib.sha256(f'{response_id}:{index}'.encode()).hexdigest()[:24]
            content.append({'type': 'tool_use', 'id': encode_tool_id(tool_id), 'name': call['function']['name'], 'input': arguments})
        stops = {'stop': 'end_turn', 'length': 'max_tokens', 'tool_calls': 'tool_use',
            'function_call': 'tool_use', 'content_filter': 'refusal'}
        if not tools and finish not in stops:
            raise Rejection(502, 'api_error', 'upstream response has no supported finish_reason')
        return {'id': 'msg_lgw_' + hashlib.sha256(response_id.encode()).hexdigest()[:24],
            'type': 'message', 'role': 'assistant', 'model': client_model, 'content': content,
            'stop_reason': 'tool_use' if tools else stops[finish], 'stop_sequence': None,
            'usage': anthropic_usage(response_json.get('usage'), dialect)[0]}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise Rejection(502, 'api_error', 'upstream returned an invalid chat response') from exc


def error_response(status: int, body_bytes: bytes, headers: Mapping[str, str], dialect: Dialect) -> tuple[int, dict, bytes]:
    dialect = _dialect(dialect)
    headers = {name.lower(): value for name, value in headers.items()}
    try:
        parsed = json.loads(body_bytes)
        error = parsed.get('error', {}) if isinstance(parsed, dict) else {}
        error = error if isinstance(error, dict) else {}
    except (ValueError, UnicodeDecodeError):
        error = {}
    message = error.get('message')
    message = message if isinstance(message, str) else 'upstream request failed'
    message = re.sub(r'''https?://[^\s<>"']+''',
        lambda match: match.group().split('?', 1)[0].split('#', 1)[0], message)
    code = error.get('code')
    outgoing = {'content-type': 'application/json'}
    if 'x-should-retry' in headers:
        outgoing['x-should-retry'] = headers['x-should-retry']
    if status in (400, 422):
        status, error_type = 400, 'invalid_request_error'
        if isinstance(code, str) and code in dialect.context_overflow_codes:
            message = f'prompt is too long: capability_rejected: prompt_too_long (upstream: {message})'
    elif status == 402 or (status == 429 and isinstance(code, str) and code in dialect.billing_codes):
        status, error_type = 403, 'permission_error'
        outgoing['x-should-retry'] = 'false'
    elif status in (503, 529):
        status, error_type = 529, 'overloaded_error'
    else:
        error_type = {401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error',
            413: 'request_too_large', 429: 'rate_limit_error'}.get(status, 'api_error')
    if 'retry-after' in headers:
        retry = headers['retry-after']
        try:
            outgoing['retry-after'] = str(max(0, math.ceil(float(retry))))
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(retry)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                outgoing['retry-after'] = str(max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds())))
            except (TypeError, ValueError, OverflowError):
                pass
    body: dict = {'type': 'error', 'error': {'type': error_type, 'message': message}}
    request_id = headers.get('x-request-id') or headers.get('request-id')
    if request_id:
        body['request_id'] = request_id
    return status, outgoing, _json(body)


def local_response(method: str, path: str, route) -> tuple[int, dict, bytes] | None:
    path = path.partition('?')[0]
    headers = {'content-type': 'application/json'}
    if method in ('HEAD', 'GET') and path == '/api/hello':
        return 200, headers, b''
    if method == 'GET' and path == '/v1/models':
        models = route.models or {}
        data = [{'type': 'model', 'id': model, 'display_name': model, 'created_at': route.created_at} for model in models]
        return 200, headers, _json({'data': data, 'has_more': False, 'first_id': next(iter(models), None),
            'last_id': next(reversed(models), None) if models else None})
    if method == 'POST' and path == '/v1/messages/count_tokens':
        return 404, headers, _json({'type': 'error', 'error': {'type': 'not_found_error',
            'message': 'token counting is not available through the gateway'}})
    if method == 'POST' and path == '/v1/messages':
        return None
    return 404, headers, _json({'type': 'error', 'error': {'type': 'not_found_error',
        'message': 'endpoint is not supported through the Looper gateway'}})
