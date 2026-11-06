"""Pure request redaction, content deduplication and response assembly."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zlib

from looper.agents.run_usage import request_tokens
from . import RouteFile

SECRET_MARKERS = ('token', 'secret', 'key', 'auth', 'cookie')
REQUEST_VALUES = frozenset({'content-type', 'content-length', 'content-encoding', 'accept', 'accept-encoding', 'user-agent',
                            'anthropic-version', 'anthropic-beta', 'anthropic-dangerous-direct-browser-access', 'x-app',
                            'x-claude-code-session-id', 'openai-beta'})
RESPONSE_VALUES = frozenset({'content-type', 'content-length', 'content-encoding', 'request-id', 'x-request-id', 'retry-after',
                             'openai-processing-ms'})
REQUEST_PREFIXES = ('x-stainless-',)
RESPONSE_PREFIXES = ('anthropic-ratelimit-', 'x-ratelimit-')
MIB = 1024 ** 2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def redact_headers(pairs, *, response=False) -> dict[str, str]:
    allowed = RESPONSE_VALUES if response else REQUEST_VALUES
    prefixes = RESPONSE_PREFIXES if response else REQUEST_PREFIXES
    result = {}
    for name, value in pairs:
        name = name.decode('latin-1') if isinstance(name, bytes) else name
        value = value.decode('latin-1') if isinstance(value, bytes) else value
        name = name.lower()
        if any(marker in name for marker in SECRET_MARKERS) or not (name in allowed or name.startswith(prefixes)):
            value = '<redacted>'
        result[name] = result[name] + ', ' + value if name in result else value
    return result


def redact_query(path):
    split = urlsplit(path)
    query = [(k, '<redacted>' if any(m in k.lower() for m in SECRET_MARKERS) else v)
             for k, v in parse_qsl(split.query, keep_blank_values=True)]
    return urlunsplit(('', '', split.path, urlencode(query, safe='<>'), ''))


def kind_of(path):
    path = urlsplit(path).path
    if path.endswith('/messages/count_tokens'):
        return 'count_tokens'
    return 'model' if path.endswith(('/messages', '/responses', '/chat/completions')) else 'other'


def _positions(body, path):
    path = urlsplit(path).path
    if not isinstance(body, dict):
        return
    if path.endswith(('/messages', '/messages/count_tokens')):
        single, arrays = ('system', 'tools'), ('messages',)
    elif path.endswith('/responses'):
        single, arrays = ('instructions', 'tools'), ('input',)
    elif path.endswith('/chat/completions'):
        single, arrays = ('tools',), ('messages',)
    else:
        return
    for key in single:
        if key in body:
            yield body, key
    for key in arrays:
        if isinstance(body.get(key), list):
            for index in range(len(body[key])):
                yield body[key], index
        elif key == 'input' and key in body:
            yield body, key


def split_request(body, path):
    skeleton, parts = deepcopy(body), {}
    if kind_of(path) == 'other' and len(canonical(body)) > 64 * 1024:
        return None, parts
    for container, key in _positions(skeleton, path):
        part = container[key]
        sha = hashlib.sha256(canonical(part)).hexdigest()
        parts[sha] = part
        container[key] = {'$llmrec_part': sha}
    return skeleton, parts


def join_request(skeleton, parts, path):
    body = deepcopy(skeleton)
    for container, key in _positions(body, path):
        reference = container[key]
        if isinstance(reference, dict) and set(reference) == {'$llmrec_part'}:
            container[key] = deepcopy(parts[reference['$llmrec_part']])
    return body


def decode(data, encoding):
    encoding = (encoding or '').strip().lower()
    if encoding in ('', 'identity'):
        return data
    if encoding in ('gzip', 'deflate'):
        try:
            decoder = zlib.decompressobj(47)
            return decoder.decompress(data) + decoder.flush()
        except zlib.error:
            return None
    return None


@dataclass(frozen=True)
class Assembled:
    body: Any = None
    raw: str | None = None
    gap: str | None = None


def _anthropic(events):
    message = None
    blocks, fragments = {}, {}
    error = None
    for event in events:
        kind = event.get('type')
        index = event.get('index')
        if kind == 'message_start':
            message = deepcopy(event['message'])
        elif kind == 'content_block_start':
            blocks[index] = deepcopy(event['content_block'])
        elif kind == 'content_block_delta':
            block = blocks[index]
            delta = event['delta']
            dtype = delta.get('type')
            if dtype == 'input_json_delta':
                fragments[index] = fragments.get(index, '') + delta.get('partial_json', '')
            elif dtype in ('text_delta', 'thinking_delta', 'signature_delta'):
                key = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}[dtype]
                block[key] = block.get(key, '') + delta.get(key, '')
            elif dtype == 'citations_delta':
                block.setdefault('citations', []).append(delta['citation'])
        elif kind == 'message_delta' and message is not None:
            message.update(event.get('delta', {}))
            message.setdefault('usage', {}).update(event.get('usage', {}))
        elif kind == 'error':
            error = event.get('error')
    if message is None:
        if error is not None:
            return {'type': 'error', 'error': error}
        raise ValueError('missing message_start')
    for index, fragment in fragments.items():
        blocks[index]['input'] = json.loads(fragment)
    if blocks:
        message['content'] = [blocks[k] for k in sorted(blocks)]
    if error is not None:
        message['error'] = error
    return message


def _chat(events):
    body, choices = {}, {}
    for event in events:
        for key in ('id', 'object', 'created', 'model', 'usage', 'system_fingerprint'):
            if key in event:
                body[key] = event[key]
        for choice in event.get('choices', []):
            index = choice.get('index', 0)
            output = choices.setdefault(index, {'index': index, 'message': {}, 'finish_reason': None})
            if choice.get('finish_reason') is not None:
                output['finish_reason'] = choice['finish_reason']
            for key, value in choice.get('delta', {}).items():
                message = output['message']
                if key == 'tool_calls':
                    calls = message.setdefault('tool_calls', [])
                    for call in value:
                        at = call.get('index', 0)
                        while len(calls) <= at:
                            calls.append({'type': 'function', 'function': {}})
                        target = calls[at]
                        for field_name in ('id', 'type'):
                            if field_name in call:
                                target[field_name] = call[field_name]
                        for field_name, fragment in call.get('function', {}).items():
                            target['function'][field_name] = target['function'].get(field_name, '') + fragment
                elif isinstance(value, str) and key != 'role':
                    message[key] = (message.get(key) or '') + value
                else:
                    message[key] = value
    body['choices'] = [choices[k] for k in sorted(choices)]
    return body


def assemble(path, content_type, data):
    text = data.decode('utf-8', errors='replace')
    try:
        if 'text/event-stream' not in (content_type or '').lower():
            return Assembled(body=json.loads(text))
        # SSE data lines form one event, possibly spread over several lines.
        events, lines = [], []
        for line in text.replace('\r\n', '\n').split('\n') + ['']:
            if line.startswith('data:'):
                lines.append(line[5:].lstrip(' '))
            elif not line and lines:
                payload = '\n'.join(lines)
                lines = []
                if payload != '[DONE]':
                    events.append(json.loads(payload))
        if not events:
            raise ValueError('empty stream')
        endpoint = urlsplit(path).path
        if endpoint.endswith(('/messages', '/messages/count_tokens')):
            body = _anthropic(events)
        elif endpoint.endswith('/responses'):
            final = [e['response'] for e in events if e.get('type') in
                     {'response.completed', 'response.failed', 'response.incomplete'}]
            if not final:
                raise ValueError('missing final response')
            body = final[-1]
        elif endpoint.endswith('/chat/completions'):
            body = _chat(events)
        else:
            raise ValueError('unknown SSE protocol')
        return Assembled(body=body)
    except (ValueError, KeyError, TypeError, IndexError):
        return Assembled(raw=text[:MIB], gap='unparsable_stream')


def tokens(path, body):
    usage = body.get('usage') if isinstance(body, dict) else None
    if urlsplit(path).path.endswith('/messages/count_tokens') and isinstance(body, dict) and usage is None:
        usage = body
    if not isinstance(usage, dict):
        return None, None, None, None, None
    endpoint = urlsplit(path).path
    if endpoint.endswith(('/messages', '/messages/count_tokens')):
        value = request_tokens(usage)
        return value.input_tokens, value.output_tokens, value.cache_read_tokens, value.cache_creation_tokens, usage
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    if endpoint.endswith('/responses'):
        details = usage.get('input_tokens_details')
        details = details if isinstance(details, dict) else {}
        cached = count(details.get('cached_tokens'))
        written = count(details.get('cache_write_tokens'))
        incoming = count(usage.get('input_tokens'))
        return (max(0, incoming - cached - written) if all(value is not None for value in (incoming, cached, written)) else None,
                count(usage.get('output_tokens')), cached, written, usage)
    details = usage.get('prompt_tokens_details')
    details = details if isinstance(details, dict) else {}
    cached = count(usage.get('prompt_cache_hit_tokens', details.get('cached_tokens')))
    incoming = count(usage.get('prompt_cache_miss_tokens'))
    prompt = count(usage.get('prompt_tokens'))
    if incoming is None and prompt is not None and cached is not None:
        incoming = max(0, prompt - cached)
    return incoming, count(usage.get('completion_tokens')), cached, 0, usage


@dataclass
class Capture:
    route: RouteFile = field(default_factory=RouteFile)
    instance: str = ''
    seq: int = 0
    record_id: int | None = None
    after_record_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    method: str = 'POST'
    path: str = ''
    kind: str = 'model'
    model: str | None = None
    stream: bool | None = None
    status: int | None = None
    outcome: str = 'completed'
    error: str | None = None
    ttfb_ms: int | None = None
    duration_ms: int | None = None
    request_headers: dict = field(default_factory=dict)
    response_headers: dict | None = None
    skeleton: Any = None
    parts: dict = field(default_factory=dict)
    request_bytes: int = 0
    request_sha256: str = ''
    response_body: Any = None
    response_raw: str | None = None
    response_bytes: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    usage: dict | None = None
    record_gap: str | None = None
    body_sanitized: bool = False

    def size(self):
        return (len(canonical(self.skeleton)) + len(canonical(self.parts)) + len(canonical(self.response_body))
                + len((self.response_raw or '').encode()) + len(canonical(self.request_headers))
                + len(canonical(self.response_headers)) + 1024)


def build_capture(*, request_body, response_data, decoded_response=None, encoding_gap=False, **values):
    capture = Capture(**values)
    # Connectivity probes have no JSON body and are not model exchanges.
    if not request_body and kind_of(capture.path) == 'other':
        return None
    capture.model = capture.route.model
    capture.request_bytes = len(request_body)
    capture.request_sha256 = hashlib.sha256(request_body).hexdigest()
    headers = capture.request_headers
    decoded_request = decode(request_body, headers.get('content-encoding'))
    try:
        if decoded_request is None:
            capture.record_gap = 'body_encoding_unparsed'
        elif len(decoded_request) > 16 * MIB:
            capture.record_gap = 'request_too_large'
        else:
            body = json.loads(decoded_request)
            capture.skeleton, capture.parts = split_request(body, capture.path)
            if kind_of(capture.path) == 'other' and len(canonical(body)) > 64 * 1024:
                capture.record_gap = 'request_too_large'
            if isinstance(body, dict):
                model, stream = body.get('model'), body.get('stream')
                if isinstance(model, str):
                    capture.model = model
                capture.stream = stream if isinstance(stream, bool) else None
    except (ValueError, TypeError):
        capture.record_gap = 'unparsable_request'
    if encoding_gap:
        capture.response_raw = response_data[:MIB].decode('utf-8', errors='replace')
        capture.record_gap = capture.record_gap or 'body_encoding_unparsed'
        return capture
    if decoded_response is None:
        decoded_response = decode(response_data, (capture.response_headers or {}).get('content-encoding'))
    if decoded_response is None:
        capture.record_gap = capture.record_gap or 'body_encoding_unparsed'
        capture.response_raw = response_data[:MIB].decode('utf-8', errors='replace')
        return capture
    result = assemble(capture.path, (capture.response_headers or {}).get('content-type'), decoded_response)
    capture.response_body, capture.response_raw = result.body, result.raw
    capture.record_gap = capture.record_gap or result.gap
    (capture.input_tokens, capture.output_tokens, capture.cache_read_tokens, capture.cache_write_tokens,
     capture.usage) = tokens(capture.path, capture.response_body)
    if capture.response_body is not None and len(canonical(capture.response_body)) > 4 * MIB:
        capture.record_gap = 'response_too_large'
        capture.response_body = {key: capture.response_body[key] for key in ('usage', 'stop_reason', 'stop_sequence', 'status')
                                 if key in capture.response_body}
    return capture
