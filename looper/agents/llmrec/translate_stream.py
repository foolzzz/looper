"""Incremental Chat Completions to Anthropic SSE conversion.

Tool arguments are held until completion: interleaved OpenAI tool deltas cannot
be represented by reopening an already closed Anthropic content block.
"""
from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
import hashlib
import json
import re
from typing import Any

from .dialects import Dialect
from .translate import (Rejection, RejectionCode, anthropic_usage, cache_fields_present, error_response,
                        from_chat, thinking_signature)


def _event(kind: str, **payload: Any) -> bytes:
    body = {'type': kind, **payload}
    return ('event: ' + kind + '\ndata: ' +
            json.dumps(body, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode()


def _stream_container(value, kind):
    if not isinstance(value, kind):
        raise Rejection(502, 'api_error', 'upstream returned invalid stream data')
    return value


def _optional_text(value):
    return None if value is None else _stream_container(value, str)


def _validate_chunk(event, dialect):
    # Validate before producing deltas: an exception must not leave a block open
    # whose start frame was never returned to the caller.
    event = _stream_container(event, dict)
    _optional_text(event.get('id'))
    for choice in _stream_container(event.get('choices', []), list):
        choice = _stream_container(choice, dict)
        if choice.get('index', 0) != 0:
            continue
        _optional_text(choice.get('finish_reason'))
        delta = choice.get('delta')
        delta = _stream_container({} if delta is None else delta, dict)
        _optional_text(delta.get('content'))
        _optional_text(delta.get('refusal'))
        if dialect.reasoning_field:
            _optional_text(delta.get(dialect.reasoning_field))
        calls = delta.get('tool_calls')
        for call in _stream_container([] if calls is None else calls, list):
            call = _stream_container(call, dict)
            index = call.get('index', 0)
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise Rejection(502, 'api_error', 'upstream returned invalid stream data')
            _optional_text(call.get('id'))
            function = call.get('function')
            function = _stream_container({} if function is None else function, dict)
            for key in ('name', 'arguments'):
                _optional_text(function.get(key))
    return event


class StreamTranslator:
    def __init__(self, dialect: Dialect, client_model: str, message_id: str, *, start_input_tokens: int = 0):
        self.dialect = dialect
        self.client_model = client_model
        self.message_id = message_id
        self.start_input_tokens = start_input_tokens
        self.index = 0
        self.open: str | None = None
        self.tools: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.done = False
        self.error_type: str | None = None
        self.error_status: int | None = None
        self.tool_args_invalid = False
        self._started = False
        self._thinking = ''
        self._upstream_id = ''

    @property
    def usage_missing(self) -> bool:
        return not self.usage

    @property
    def cache_fields_missing(self) -> bool:
        return bool(self.usage and 'prompt_tokens' in self.usage and
                    not cache_fields_present(self.usage, self.dialect))

    def start(self) -> list[bytes]:
        if self._started or self.done:
            return []
        self._started = True
        return [_event('message_start', message={
            'id': self.message_id, 'type': 'message', 'role': 'assistant',
            'model': self.client_model, 'content': [], 'stop_reason': None,
            'stop_sequence': None, 'usage': {**anthropic_usage(None, self.dialect)[0],
                                            'input_tokens': self.start_input_tokens},
        }), self.ping()]

    def ping(self) -> bytes:
        return _event('ping')

    def _close(self) -> list[bytes]:
        if self.open is None:
            return []
        frames = []
        if self.open == 'thinking':
            frames.append(_event('content_block_delta', index=self.index,
                delta={'type': 'signature_delta', 'signature': thinking_signature(self._thinking)}))
        frames.append(_event('content_block_stop', index=self.index))
        self.index += 1
        self.open = None
        self._thinking = ''
        return frames

    def _delta(self, kind: str, text: str) -> list[bytes]:
        _stream_container(text, str)
        frames = []
        if self.open != kind:
            frames.extend(self._close())
            self.open = kind
            block = {'type': kind, kind: ''}
            if kind == 'thinking':
                block['signature'] = ''
            frames.append(_event('content_block_start', index=self.index, content_block=block))
        if kind == 'thinking':
            self._thinking += text
        frames.append(_event('content_block_delta', index=self.index,
            delta={'type': kind + '_delta', kind: text}))
        return frames

    def feed(self, event: dict) -> list[bytes]:
        if self.done:
            return []
        event = _validate_chunk(event, self.dialect)
        if isinstance(event.get('error'), dict):
            error = event['error']
            status = event.get('status') or error.get('status') or 500
            status, _, body = error_response(status, json.dumps(event).encode(), {}, self.dialect)
            mapped = json.loads(body)['error']
            self.error_status = status
            return self.fail(mapped['type'], mapped['message'])
        if not self._upstream_id and event.get('id'):
            self._upstream_id = event['id']
        if isinstance(event.get('usage'), dict) and event['usage']:
            self.usage = event['usage']
        frames = []
        for choice in event.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            reasoning = delta.get(self.dialect.reasoning_field) if self.dialect.reasoning_field else None
            if reasoning:
                frames.extend(self._delta('thinking', reasoning))
            if delta.get('content'):
                frames.extend(self._delta('text', delta['content']))
            if delta.get('refusal'):
                frames.extend(self._delta('text', delta['refusal']))
            for call in delta.get('tool_calls') or []:
                index = call.get('index', 0)
                if index not in self.tools:
                    frames.extend(self._close())
                    self.tools[index] = {'type': 'function', 'function': {'arguments': ''}}
                target = self.tools[index]
                function = call.get('function') or {}
                if call.get('id') and not target.get('id'):
                    target['id'] = call['id']
                if function.get('name') and not target['function'].get('name'):
                    target['function']['name'] = function['name']
                target['function']['arguments'] += function.get('arguments') or ''
            if choice.get('finish_reason'):
                self.finish_reason = choice['finish_reason']
                if self.finish_reason == 'insufficient_system_resource':
                    self.error_status = 529
                    return frames + self.fail('overloaded_error', 'upstream has insufficient system resources')
        return frames

    def finish(self) -> list[bytes]:
        if self.done:
            return []
        if self.finish_reason is None:
            return self.fail('api_error', 'upstream stream ended without finish_reason')
        calls = []
        for index in sorted(self.tools):
            call = self.tools[index]
            if not call.get('id'):
                digest = hashlib.sha256((self._upstream_id + ':' + str(index)).encode()).hexdigest()[:24]
                call['id'] = 'call_' + digest
            calls.append(call)
        # Use the same parser, stop mapping and id encoding as non-streaming.
        try:
            message = from_chat({'id': self._upstream_id, 'choices': [{
                'message': {'tool_calls': calls}, 'finish_reason': self.finish_reason}],
                'usage': self.usage}, client_model=self.client_model, dialect=self.dialect)
        except Rejection as rejection:
            self.tool_args_invalid = rejection.code is RejectionCode.TOOL_ARGS_INVALID
            self.error_status = rejection.status
            return self.fail(rejection.error_type, rejection.message)
        frames = self._close()
        for block in message['content']:
            if block['type'] != 'tool_use':
                continue
            arguments = json.dumps(block['input'], ensure_ascii=False)
            frames.append(_event('content_block_start', index=self.index,
                content_block={**block, 'input': {}}))
            frames.append(_event('content_block_delta', index=self.index,
                delta={'type': 'input_json_delta', 'partial_json': arguments}))
            frames.append(_event('content_block_stop', index=self.index))
            self.index += 1
        frames.append(_event('message_delta',
            delta={'stop_reason': message['stop_reason'], 'stop_sequence': None},
            usage=message['usage']))
        frames.append(_event('message_stop'))
        self.done = True
        return frames

    def fail(self, error_type: str, message: str) -> list[bytes]:
        if self.done:
            return []
        self.done = True
        self.error_type = error_type
        return self._close() + [_event('error', error={'type': error_type, 'message': message})]


_LINE_END = re.compile(b'\r\n|\r|\n')


async def sse_events(byte_iter: AsyncIterable[bytes]) -> AsyncIterator[dict | None]:
    """Parse decompressed SSE bytes; comments yield None, DONE stops reading.

    A pending ``__anext__`` should be kept alive by the caller during idle pings;
    cancelling that read on a ping timeout would also cancel the upstream read.
    """
    buffer = b''
    lines: list[str] = []
    first = True

    def consume(line: bytes):
        nonlocal first
        text = line.decode('utf-8')
        if first:
            text = text.removeprefix('\ufeff')
            first = False
        if text.startswith(':'):
            return 'comment', None
        if not text and lines:
            payload = '\n'.join(lines)
            lines.clear()
            if payload == '[DONE]':
                return 'done', None
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError('upstream SSE data must be a JSON object')
            return 'data', value
        if text.startswith('data:'):
            value = text[5:]
            lines.append(value[1:] if value.startswith(' ') else value)
        return 'skip', None

    async for chunk in byte_iter:
        buffer += chunk
        while match := _LINE_END.search(buffer):
            if match.group() == b'\r' and match.end() == len(buffer):
                break
            line, buffer = buffer[:match.start()], buffer[match.end():]
            kind, value = consume(line)
            if kind == 'done':
                return
            if kind in ('data', 'comment'):
                yield value
    # Some upstreams omit the last empty line; accept their final complete JSON.
    while match := _LINE_END.search(buffer):
        line, buffer = buffer[:match.start()], buffer[match.end():]
        kind, value = consume(line)
        if kind == 'done':
            return
        if kind in ('data', 'comment'):
            yield value
    if buffer:
        kind, value = consume(buffer)
        if kind in ('data', 'comment'):
            yield value
    if lines:
        kind, value = consume(b'')
        if kind == 'data':
            yield value
