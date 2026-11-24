"""Messages wire boundary for the neutral loop; see THIRD_PARTY_NOTICES.txt."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
import json
import logging
import os
import time

from ... import log_redaction, model_names
from .. import ApiProtocol, RuntimeUnsupported
from .loop import (
    CodecDone, CodecError, ErrorKind, MessageItem, ReasoningItem, ReplayState,
    RequestLimitReached, RequestRecord, RequestUsage, TextDelta, ToolCall, ToolCallItem, ToolResultItem,
)

CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 120.0
REQUEST_TIMEOUT = 600.0
_UNKNOWN = RequestUsage(None, None, None, None, False)


class MessagesConfigurationError(RuntimeUnsupported):
    """Ambient configuration must not override the resolved Profile."""


def _check_environment():
    from .session import FORBIDDEN_ENV

    names = [name for name in FORBIDDEN_ENV if name in os.environ]
    if names:
        raise MessagesConfigurationError('Unsupported Messages environment variables: ' + ', '.join(names))


def _required(obj, key, kind=str, *, empty=False):
    value = obj[key]
    if type(value) is not kind or kind is str and not empty and not value:
        raise ValueError('invalid required field')
    return value


def _object(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate argument key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('non-JSON number')

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('tool input must be an object')
    return value


def _input(items, target):
    messages = []

    def add(role, block):
        if not messages or messages[-1]['role'] != role:
            messages.append(dict(role=role, content=[]))
        messages[-1]['content'].append(block)

    for item in items:
        if isinstance(item, MessageItem):
            add(item.role, dict(type='text', text=item.text))
        elif isinstance(item, ToolCallItem):
            call = item.call
            try:
                value = _object(call.arguments)
            except (ValueError, TypeError):
                raise CodecError(ErrorKind.INVALID_REQUEST, None, 'stored tool input is not a JSON object') from None
            add('assistant', dict(type='tool_use', id=call.call_id, name=call.name, input=value))
        elif isinstance(item, ToolResultItem):
            add('user', dict(type='tool_result', tool_use_id=item.call_id, content=item.text, is_error=item.is_error))
        elif isinstance(item, ReasoningItem) and item.state is not None:
            state = item.state
            if state.protocol != target.protocol or state.model != target.model:
                continue
            try:
                payload = json.loads(state.encrypted_content)
                block = payload['block']
                if payload['version'] != 1 or block['type'] not in ('thinking', 'redacted_thinking'):
                    raise ValueError
                if block['type'] == 'thinking':
                    block = dict(type='thinking', thinking=_required(block, 'thinking', empty=True),
                                 signature=_required(block, 'signature'))
                else:
                    block = dict(type='redacted_thinking', data=_required(block, 'data'))
            except (ValueError, TypeError, KeyError):
                raise CodecError(ErrorKind.PROTOCOL, None, 'invalid Messages reasoning replay') from None
            add('assistant', block)
    if messages and messages[-1]['content']:
        messages[-1]['content'][-1]['cache_control'] = {'type': 'ephemeral'}
    return messages


def _error_kind(status, body):
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (408, 409) or status is not None and status >= 500:
        return ErrorKind.UNAVAILABLE
    error = body.get('error', body) if isinstance(body, dict) else {}
    kinds = {'authentication_error': ErrorKind.AUTH, 'permission_error': ErrorKind.AUTH,
             'not_found_error': ErrorKind.NOT_FOUND, 'rate_limit_error': ErrorKind.RATE_LIMITED,
             'overloaded_error': ErrorKind.UNAVAILABLE, 'api_error': ErrorKind.UNAVAILABLE,
             'context_length_exceeded': ErrorKind.CONTEXT_OVERFLOW,
             'invalid_request_error': ErrorKind.INVALID_REQUEST}
    return kinds.get(error.get('type'), ErrorKind.INVALID_REQUEST) if isinstance(error, dict) else ErrorKind.INVALID_REQUEST


class MessagesCodec:
    protocol = ApiProtocol.ANTHROPIC_MESSAGES

    def __init__(self):
        self._client = None
        self._closing = None
        self._active = ContextVar('messages_request')

    async def _get_client(self, target):
        _check_environment()
        import httpx2
        from anthropic import AsyncAnthropic

        if self._closing is not None:
            raise RuntimeError('Messages codec is closed')
        if self._client is None:
            for name in ('anthropic', 'httpx2', 'httpcore2', 'httpcore'):
                logging.getLogger(name).setLevel(logging.WARNING)

            async def request_hook(request):
                stream = self._active.get()
                if stream.attempts >= stream.max_attempts:
                    raise RequestLimitReached()
                # The SDK hardcodes /v1/messages; Profile endpoints already
                # own their version prefix (including compatibility proxies).
                request.url = httpx2.URL(stream.target.base_url.rstrip('/') + '/messages')
                stream.attempts += 1

            async def response_hook(response):
                response.headers.pop('x-should-retry', None)
                stream = self._active.get()
                stream.record = replace(stream.record, status=response.status_code,
                    upstream_id=response.headers.get('request-id') or response.headers.get('x-request-id')
                    or stream.record.upstream_id)

            transport = httpx2.AsyncClient(trust_env=False,
                timeout=httpx2.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT),
                event_hooks={'request': [request_hook], 'response': [response_hook]})
            self._client = AsyncAnthropic(auth_token=target.api_key, base_url=target.base_url, max_retries=2,
                timeout=httpx2.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT), http_client=transport)
        return self._client.with_options(auth_token=target.api_key, base_url=target.base_url)

    def stream(self, **request):
        _check_environment()
        return _Stream(self, **request)

    async def aclose(self):
        if self._closing is None:
            async def close():
                if self._client is not None:
                    await self._client.close()
            self._closing = asyncio.create_task(close())
        cancelled = False
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError:
                cancelled = True
        self._closing.result()
        if cancelled:
            raise asyncio.CancelledError()


class _Stream:
    def __init__(self, codec, *, target, system_prompt, items, specs, session_id, index, max_attempts):
        self.codec, self.target, self.max_attempts = codec, target, max_attempts
        self.start, self.end, self.attempts = time.monotonic(), None, 0
        self.record = RequestRecord(index, None, 0, None, 0, None, _UNKNOWN, None)
        self.native, self.done, self.started, self.terminal = None, False, False, False
        self.blocks, self.stopped, self.ids, self.usage = {}, set(), set(), {}
        self.stop_reason = None
        self.final_output_known = False
        self.items = items
        system = [dict(type='text', text=system_prompt, cache_control={'type': 'ephemeral'})] if system_prompt else []
        self.body = dict(model=model_names.parse_model_name(target.model).base, max_tokens=target.max_output_tokens,
            system=system,
            tools=[dict(name=s.name, description=s.description, input_schema=s.schema) for s in specs])
        if target.effort is not None:
            self.body.update(output_config={'effort': target.effort}, thinking={'type': 'adaptive'})
        log_redaction.add_secret(target.api_key)

    def snapshot(self):
        return replace(self.record, duration_ms=int(((self.end or time.monotonic()) - self.start) * 1000),
                       retries=max(0, self.attempts - 1))

    async def aclose(self):
        self.end = self.end or time.monotonic()
        if self.native is not None:
            await self.native.close()

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
            if key in usage and usage[key] is not None:
                value = usage[key]
                if type(value) is not int or value < 0:
                    raise ValueError('invalid usage')
                self.usage[key] = value
        values = [self.usage.get(key) for key in
                  ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
        complete = all(value is not None for value in values)
        prompt = sum(values[i] for i in (0, 2, 3)) if all(values[i] is not None for i in (0, 2, 3)) else None
        self.record = replace(self.record, usage=RequestUsage(*values, complete), prompt_tokens=prompt)

    def _event(self, event):
        kind = _required(event, 'type')
        if kind == 'ping':
            return
        if kind == 'message_start':
            if self.started:
                raise ValueError('duplicate message start')
            message = _required(event, 'message', dict)
            if message.get('role') != 'assistant' or message.get('type') != 'message' or message.get('content') != []:
                raise ValueError('invalid message start')
            _required(message, 'id')
            self.started = True
            self.record = replace(self.record, upstream_id=self.record.upstream_id or message['id'])
            self._usage(message.get('usage'))
            return
        if not self.started or self.terminal:
            raise ValueError('event outside message')
        if kind == 'message_delta':
            if len(self.blocks) != len(self.stopped):
                raise ValueError('unclosed content block')
            delta = _required(event, 'delta', dict)
            self._usage(event.get('usage'))
            if delta.get('stop_reason') is not None:
                self.stop_reason = _required(delta, 'stop_reason')
                self.final_output_known = isinstance(event.get('usage'), dict) and type(event['usage'].get('output_tokens')) is int
                self.terminal = True
            return
        index = _required(event, 'index', int)
        if index < 0:
            raise ValueError('invalid block index')
        if kind == 'content_block_start':
            if index != len(self.blocks) or len(self.blocks) != len(self.stopped):
                raise ValueError('inconsistent block index')
            block = dict(_required(event, 'content_block', dict))
            block_type = _required(block, 'type')
            if block_type == 'text':
                _required(block, 'text', empty=True)
            elif block_type == 'tool_use':
                identity = _required(block, 'id')
                if identity in self.ids:
                    raise ValueError('duplicate tool id')
                self.ids.add(identity)
                _required(block, 'name')
                _required(block, 'input', dict)
                block['arguments'] = ''
            elif block_type == 'thinking':
                _required(block, 'thinking', empty=True)
                block['signature'] = block.get('signature', '')
                _required(block, 'signature', empty=True)
            elif block_type == 'redacted_thinking':
                _required(block, 'data')
            else:
                raise ValueError('unknown block type')
            self.blocks[index] = block
            if block_type == 'text' and block['text']:
                return TextDelta(block['text'])
            return
        if index not in self.blocks or index in self.stopped:
            raise ValueError('event outside content block')
        if kind == 'content_block_stop':
            self.stopped.add(index)
            return
        if kind != 'content_block_delta':
            raise ValueError('unknown Messages event')
        block, delta = self.blocks[index], _required(event, 'delta', dict)
        delta_type = _required(delta, 'type')
        fields = {'text_delta': ('text', 'text'), 'input_json_delta': ('tool_use', 'partial_json'),
                  'thinking_delta': ('thinking', 'thinking'), 'signature_delta': ('thinking', 'signature')}
        expected, field = fields[delta_type]
        if block['type'] != expected:
            raise ValueError('inconsistent delta type')
        value = _required(delta, field, empty=True)
        key = 'arguments' if delta_type == 'input_json_delta' else field
        block[key] += value
        if delta_type == 'text_delta':
            return TextDelta(value)

    def _finish(self):
        if not self.started or not self.terminal or len(self.blocks) != len(self.stopped):
            raise ValueError('incomplete message')
        if self.stop_reason == 'max_tokens':
            raise CodecError(ErrorKind.TRUNCATED, self.record.status, 'Messages output reached max_tokens')
        if self.stop_reason not in ('end_turn', 'stop_sequence', 'tool_use'):
            raise CodecError(ErrorKind.PROTOCOL, self.record.status, 'unsupported Messages stop reason')
        self.record = replace(self.record, usage=replace(self.record.usage,
            complete=self.record.usage.complete and self.final_output_known))
        items, calls, texts, summaries = [], [], [], []
        for index, block in self.blocks.items():
            kind = block['type']
            if kind == 'text':
                text = block['text']
                texts.append(text)
                items.append(MessageItem('assistant', text))
            elif kind == 'tool_use':
                # Keep malformed JSON untouched: TurnTools returns the existing
                # invalid-arguments result, without executing a platform action.
                arguments = block['arguments'] or json.dumps(block['input'])
                try:
                    _object(arguments)
                except (ValueError, TypeError):
                    # The shared tool validator accepts duplicate keys/NaN.
                    # Retain rejected input as a string, never guess an object;
                    # this takes its existing invalid-arguments result path.
                    arguments = json.dumps(arguments)
                call = ToolCall(block['id'], block['name'], arguments)
                calls.append(call)
                items.append(ToolCallItem(call))
            else:
                if kind == 'thinking':
                    block = dict(type=kind, thinking=block['thinking'], signature=_required(block, 'signature'))
                    summary = (block['thinking'],) if block['thinking'] else ()
                    summaries.extend(summary)
                else:
                    block, summary = dict(type=kind, data=block['data']), ()
                state = ReplayState(self.target.protocol, self.target.model, str(index),
                                    json.dumps({'version': 1, 'block': block}))
                items.append(ReasoningItem(summary, state))
        if bool(calls) != (self.stop_reason == 'tool_use'):
            raise ValueError('tool calls and stop reason disagree')
        return tuple(items), ''.join(texts), tuple(calls), '\n'.join(summaries) or None

    def __aiter__(self):
        return self

    async def __anext__(self):
        import anthropic
        import httpx2

        if self.done:
            raise StopAsyncIteration
        try:
            async with asyncio.timeout_at(self.start + REQUEST_TIMEOUT):
                if self.native is None:
                    self.body['messages'] = _input(self.items, self.target)
                    token = self.codec._active.set(self)
                    try:
                        client = await self.codec._get_client(self.target)
                        self.native = await client.messages.create(**self.body, stream=True)
                    finally:
                        self.codec._active.reset(token)
                while True:
                    native = await anext(self.native)
                    event = native.model_dump(exclude_unset=True, warnings=False)
                    if event.get('type') == 'message_stop':
                        items, text, calls, summary = self._finish()
                        self.end, self.done = time.monotonic(), True
                        return CodecDone(items, self.snapshot(), text, calls, summary)
                    output = self._event(event)
                    if output is not None:
                        if self.record.first_token_ms is None:
                            self.record = replace(self.record, first_token_ms=int((time.monotonic() - self.start) * 1000))
                        return output
        except StopAsyncIteration:
            raise CodecError(ErrorKind.PROTOCOL, self.record.status, 'stream ended without message_stop',
                             retryable_stream=self.native is not None) from None
        except (CodecError, MessagesConfigurationError):
            raise
        except anthropic.APIStatusError as error:
            kind = _error_kind(error.status_code, error.body)
            raise CodecError(kind, error.status_code, 'Messages API request failed (' + kind.value + ')') from None
        except anthropic.APIConnectionError as error:
            # The SDK wraps request-hook exceptions; retain the neutral loop's
            # request-budget result instead of reporting a connection failure.
            if isinstance(error.__cause__, RequestLimitReached):
                raise error.__cause__ from None
            raise CodecError(ErrorKind.UNAVAILABLE, self.record.status, 'Messages connection failed',
                             retryable_stream=self.native is not None) from None
        except (httpx2.TransportError, TimeoutError):
            raise CodecError(ErrorKind.UNAVAILABLE, self.record.status, 'Messages connection or deadline failed',
                             retryable_stream=self.native is not None) from None
        except (anthropic.APIError, ValueError, TypeError, KeyError, AttributeError):
            raise CodecError(ErrorKind.PROTOCOL, self.record.status, 'invalid Messages stream') from None
