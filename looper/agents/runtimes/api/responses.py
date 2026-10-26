"""Norma-derived Responses codec; see adjacent THIRD_PARTY_NOTICES.txt."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
import logging
import time

from ... import log_redaction
from .. import ApiProtocol
from .loop import (
    CodecDone, CodecError, ErrorKind, MessageItem, ReasoningItem, ReplayState,
    RequestRecord, RequestUsage, TextDelta, ToolCall, ToolCallItem, ToolResultItem,
)

CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 120.0
REQUEST_TIMEOUT = 600.0
_UNKNOWN = RequestUsage(None, None, None, None, False)
_LOG = logging.getLogger(__name__)
_CODES = {'context_length_exceeded': ErrorKind.CONTEXT_OVERFLOW,
          'model_not_found': ErrorKind.NOT_FOUND, 'rate_limit_exceeded': ErrorKind.RATE_LIMITED,
          'server_error': ErrorKind.UNAVAILABLE, 'invalid_api_key': ErrorKind.AUTH,
          'invalid_request_error': ErrorKind.INVALID_REQUEST}
_ITEM_EVENTS = {
    'response.output_item.added': None, 'response.output_item.done': None,
    'response.output_text.delta': 'message',
    'response.function_call_arguments.delta': 'function_call',
    'response.function_call_arguments.done': 'function_call',
    'response.reasoning_summary_text.delta': 'reasoning',
    'response.reasoning_summary_text.done': 'reasoning',
    'response.reasoning_summary_part.added': 'reasoning',
    'response.reasoning_summary_part.done': 'reasoning',
}


def _error_kind(status, body, default):
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status == 408 or status is not None and status >= 500:
        return ErrorKind.UNAVAILABLE
    error = body.get('error', body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return default
    return _CODES.get(error.get('code'), _CODES.get(error.get('type'), default))


def _input(items, target):
    result = []
    for item in items:
        if isinstance(item, MessageItem):
            result.append(dict(role=item.role, content=item.text))
        elif isinstance(item, ToolCallItem):
            c = item.call
            result.append(dict(type='function_call', call_id=c.call_id, name=c.name, arguments=c.arguments))
        elif isinstance(item, ToolResultItem):
            result.append(dict(type='function_call_output', call_id=item.call_id, output=item.text))
        elif isinstance(item, ReasoningItem) and item.state is not None:
            s = item.state
            if s.protocol == target.protocol and s.model == target.model:
                result.append(dict(type='reasoning', id=s.item_id, encrypted_content=s.encrypted_content,
                    summary=[dict(type='summary_text', text=t) for t in item.summary]))
    return result


def _usage(value):
    if not isinstance(value, dict):
        return _UNKNOWN, None
    total, output = value.get('input_tokens'), value.get('output_tokens')
    details = value.get('input_tokens_details') or {}
    read, write = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    uncached = total - read - write if total is not None else None
    complete = total is not None and output is not None and uncached >= 0
    return RequestUsage(max(0, uncached) if uncached is not None else None, output, read, write, complete), total


def _required(obj, key, kind=str, *, empty=False):
    value = obj[key]
    if type(value) is not kind or kind is str and not empty and not value:
        raise ValueError('invalid required field')
    return value


class _Assembly:
    """Index/id identity survives deltas; terminal output owns final content."""
    def __init__(self):
        self.items = {}
        self.ids = {}

    def add(self, event):
        index = _required(event, 'output_index', int)
        expected = _ITEM_EVENTS[event['type']]
        item = _required(event, 'item', dict) if expected is None else None
        item_id = _required(item, 'id') if item is not None else _required(event, 'item_id')
        if index < 0 or item_id in self.ids and self.ids[item_id] != index:
            raise ValueError('inconsistent output identity')
        old = self.items.get(index, {})
        if old and old['id'] != item_id:
            raise ValueError('inconsistent item id')
        self.ids[item_id] = index
        current = dict(old or {'id': item_id})
        if item is not None:
            for key in ('type', 'call_id', 'name'):
                if key in old and key in item and old[key] != item[key]:
                    raise ValueError('inconsistent call identity')
            item_kind = _required(item, 'type')
            if item_kind == 'function_call':
                _required(item, 'call_id')
                _required(item, 'name')
                _required(item, 'arguments', empty=True)
            elif item_kind == 'message':
                _required(item, 'content', list)
            elif item_kind == 'reasoning':
                _required(item, 'summary', list)
            current.update(item)
        else:
            kind = event['type']
            if 'type' in current and current['type'] != expected:
                raise ValueError('inconsistent event type')
            current['type'] = expected
            if 'delta' in event:
                _required(event, 'delta', empty=True)
            if kind == 'response.function_call_arguments.delta':
                current['arguments'] = current.get('arguments', '') + event['delta']
            elif kind == 'response.function_call_arguments.done':
                current['arguments'] = _required(event, 'arguments', empty=True)
            elif expected == 'reasoning':
                part = _required(event, 'summary_index', int)
                if part < 0:
                    raise ValueError('invalid summary index')
                if 'delta' not in event:
                    _required(event.get('part', event), 'text', empty=True)
            elif kind == 'response.output_text.delta':
                part = _required(event, 'content_index', int)
                if part < 0:
                    raise ValueError('invalid content index')
        self.items[index] = current

    def finish(self, response, target):
        output = _required(response, 'output', list)
        items, calls, texts, summaries, ids, call_ids = [], [], [], [], set(), set()
        refusal = False
        for index, item in enumerate(output):
            item_id, kind = _required(item, 'id'), _required(item, 'type')
            if item_id in ids or item_id in self.ids and self.ids[item_id] != index:
                raise ValueError('inconsistent completed output identity')
            ids.add(item_id)
            old = self.items.get(index)
            if old and any(key in old and old[key] != item.get(key) for key in ('id', 'type', 'call_id', 'name')):
                raise ValueError('inconsistent completed item identity')
            if kind == 'function_call':
                call_id = _required(item, 'call_id')
                if call_id in call_ids:
                    raise ValueError('duplicate call id')
                call_ids.add(call_id)
                arguments = item['arguments']
                if not isinstance(arguments, str):
                    raise ValueError('invalid arguments')
                call = ToolCall(call_id, _required(item, 'name'), arguments)
                calls.append(call)
                items.append(ToolCallItem(call))
            elif kind == 'message':
                if item.get('role') != 'assistant':
                    raise ValueError('invalid output role')
                text = ''
                for block in _required(item, 'content', list):
                    key = {'output_text': 'text', 'refusal': 'refusal'}.get(block['type'])
                    if key:
                        value = block[key]
                        if not isinstance(value, str):
                            raise ValueError('invalid text')
                        text += value
                        refusal |= key == 'refusal'
                texts.append(text)
                items.append(MessageItem('assistant', text))
            elif kind == 'reasoning':
                summary = tuple(b['text'] for b in _required(item, 'summary', list) if b['type'] == 'summary_text')
                if any(not isinstance(t, str) for t in summary):
                    raise ValueError('invalid reasoning summary')
                encrypted = item.get('encrypted_content')
                if encrypted is not None and not isinstance(encrypted, str):
                    raise ValueError('invalid replay state')
                state = ReplayState(target.protocol, target.model, item_id, encrypted) if encrypted else None
                items.append(ReasoningItem(summary, state))
                summaries.extend(summary)
        if any(index >= len(output) for index in self.items) or refusal and calls:
            raise ValueError('inconsistent completed output')
        return tuple(items), ''.join(texts), tuple(calls), '\n'.join(summaries) or None


class ResponsesCodec:
    protocol = ApiProtocol.OPENAI_RESPONSES

    def __init__(self):
        self._client = None
        self._active = ContextVar('responses_request')
        self._closing = None

    async def _get_client(self, target):
        # The SDK's installed transport is httpx2, not the separate httpx dependency.
        import httpx2
        from openai import AsyncOpenAI

        if self._closing is not None:
            raise RuntimeError('Responses codec is closed')
        if self._client is None:
            for name in ('openai', 'httpx2', 'httpcore'):
                logging.getLogger(name).setLevel(logging.WARNING)

            async def request_hook(request):
                stream = self._active.get()
                stream.attempts += 1

            async def response_hook(response):
                # Keep Looper's status-based policy even if a provider suggests otherwise.
                response.headers.pop('x-should-retry', None)
                stream = self._active.get()
                stream.record = replace(stream.record, status=response.status_code,
                    upstream_id=response.headers.get('x-request-id') or stream.record.upstream_id)

            transport = httpx2.AsyncClient(trust_env=False,
                timeout=httpx2.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT),
                event_hooks={'request': [request_hook], 'response': [response_hook]})
            self._client = AsyncOpenAI(api_key=target.api_key, base_url=target.base_url, max_retries=2,
                timeout=httpx2.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT), http_client=transport)
        return self._client.with_options(api_key=target.api_key, base_url=target.base_url)

    def stream(self, **request):
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
    def __init__(self, codec, *, target, system_prompt, items, specs, session_id, index):
        self.codec, self.target = codec, target
        self.start, self.end, self.attempts = time.monotonic(), None, 0
        self.record = RequestRecord(index, None, 0, None, 0, None, _UNKNOWN, None)
        self.native, self.done, self.response_id = None, False, None
        self.assembly, self.ignored = _Assembly(), set()
        self.body = dict(model=target.model, max_output_tokens=target.max_output_tokens,
            instructions=system_prompt, input=_input(items, target), store=False,
            prompt_cache_key=session_id, parallel_tool_calls=False,
            tools=[dict(type='function', name=s.name, description=s.description, parameters=s.schema, strict=False) for s in specs])
        if target.effort is not None:
            self.body.update(reasoning={'effort': target.effort}, include=['reasoning.encrypted_content'])
        log_redaction.add_secret(target.api_key)

    def snapshot(self):
        return replace(self.record, duration_ms=int(((self.end or time.monotonic()) - self.start) * 1000),
                       retries=max(0, self.attempts - 1))

    async def aclose(self):
        self.end = self.end or time.monotonic()
        if self.ignored:
            _LOG.warning('Responses ignored event types: %s', ', '.join(sorted(self.ignored)))
            self.ignored.clear()
        if self.native is not None:
            await self.native.close()

    def _response_id(self, response):
        value = _required(response, 'id')
        if self.response_id is not None and self.response_id != value:
            raise ValueError('inconsistent response id')
        self.response_id = value
        self.record = replace(self.record, upstream_id=value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import openai

        if self.done:
            raise StopAsyncIteration
        try:
            # An absolute deadline covers SDK retries and every read, without a
            # timeout/context manager suspended across a caller's yield boundary.
            async with asyncio.timeout_at(self.start + REQUEST_TIMEOUT):
                if self.native is None:
                    token = self.codec._active.set(self)
                    try:
                        client = await self.codec._get_client(self.target)
                        self.native = await client.responses.create(**self.body, stream=True)
                    finally:
                        self.codec._active.reset(token)
                while True:
                    native = await anext(self.native)
                    e = native.model_dump(exclude_unset=True, warnings=False)
                    kind = _required(e, 'type')
                    if kind in ('response.completed', 'response.failed', 'response.incomplete'):
                        response = e['response']
                        self._response_id(response)
                        if kind != 'response.completed':
                            err = response.get('error') or {}
                            category = ErrorKind.TRUNCATED if kind == 'response.incomplete' else _error_kind(None, err, ErrorKind.UNAVAILABLE)
                            raise CodecError(category, self.record.status, str(err.get('message') or 'response incomplete'))
                        if response.get('status') != 'completed':
                            raise ValueError('inconsistent terminal status')
                        items, text, calls, summary = self.assembly.finish(response, self.target)
                        usage, prompt_tokens = _usage(response.get('usage'))
                        self.record = replace(self.record, usage=usage, prompt_tokens=prompt_tokens)
                        self.end, self.done = time.monotonic(), True
                        return CodecDone(items, self.snapshot(), text, calls, summary)
                    elif kind in ('response.created', 'response.in_progress'):
                        self._response_id(e['response'])
                    elif kind in _ITEM_EVENTS:
                        self.assembly.add(e)
                        if kind == 'response.output_text.delta':
                            text = _required(e, 'delta', empty=True)
                            if self.record.first_token_ms is None:
                                self.record = replace(self.record, first_token_ms=int((time.monotonic() - self.start) * 1000))
                            return TextDelta(text)
                    elif len(self.ignored) < 8:
                        self.ignored.add(log_redaction.redact(kind)[:60])
        except StopAsyncIteration:
            raise CodecError(ErrorKind.PROTOCOL, self.record.status, 'stream ended without terminal response') from None
        except CodecError:
            raise
        except openai.APIStatusError as error:
            raise CodecError(_error_kind(error.status_code, error.body, ErrorKind.INVALID_REQUEST),
                             error.status_code, str(error)) from None
        except (openai.APIConnectionError, TimeoutError) as error:
            raise CodecError(ErrorKind.UNAVAILABLE, self.record.status, str(error) or 'request deadline exceeded',
                             retryable_stream=self.native is not None) from None
        except (openai.APIError, ValueError, TypeError, KeyError, AttributeError):
            raise CodecError(ErrorKind.PROTOCOL, self.record.status, 'invalid Responses stream') from None
