"""Norma-derived turn loop; see THIRD_PARTY_NOTICES.txt for origin and MIT notice."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Literal, Protocol

from ... import log_redaction
from .. import ApiProtocol, RuntimeUnsupported
from ..cancel import TurnCancel


class ErrorKind(str, Enum):
    AUTH = 'auth'
    NOT_FOUND = 'not_found'
    RATE_LIMITED = 'rate_limited'
    UNAVAILABLE = 'unavailable'
    CONTEXT_OVERFLOW = 'context_overflow'
    TRUNCATED = 'truncated'
    INVALID_REQUEST = 'invalid_request'
    PROTOCOL = 'protocol'


class Outcome(str, Enum):
    ANSWERED = 'answered'
    MAX_REQUESTS = 'max_requests'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    CAPPED = 'capped'


@dataclass(frozen=True)
class ModelTarget:
    protocol: ApiProtocol
    base_url: str
    model: str
    effort: str | None
    context_window: int | None
    api_key: str = field(repr=False)
    max_output_tokens: int = 16_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    is_error: bool
    ran: bool


class TurnTools(Protocol):
    async def specs(self) -> list[ToolSpec]: ...
    async def call(self, call: ToolCall) -> ToolOutcome: ...


@dataclass(frozen=True)
class RequestUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    complete: bool


@dataclass(frozen=True)
class RequestRecord:
    index: int
    status: int | None
    duration_ms: int
    first_token_ms: int | None
    retries: int
    upstream_id: str | None
    usage: RequestUsage
    prompt_tokens: int | None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class RequestDone:
    record: RequestRecord
    text: str
    tool_calls: tuple[ToolCall, ...]
    reasoning_summary: str | None


@dataclass(frozen=True)
class ToolDone:
    call: ToolCall
    outcome: ToolOutcome


LoopEvent = TextDelta | RequestDone | ToolDone


@dataclass(frozen=True)
class LoopResult:
    outcome: Outcome
    final_text: str
    requests: tuple[RequestRecord, ...]
    error_kind: ErrorKind | None
    error_status: int | None
    error_message: str | None
    first_request_failed: bool


@dataclass(frozen=True)
class MessageItem:
    role: Literal['user', 'assistant']
    text: str


@dataclass(frozen=True)
class ToolCallItem:
    call: ToolCall


@dataclass(frozen=True)
class ToolResultItem:
    call_id: str
    text: str
    is_error: bool
    ran: bool


@dataclass(frozen=True)
class ReplayState:
    protocol: ApiProtocol
    model: str
    item_id: str
    encrypted_content: str = field(repr=False)


@dataclass(frozen=True)
class ReasoningItem:
    summary: tuple[str, ...]
    state: ReplayState | None


SessionItem = MessageItem | ToolCallItem | ToolResultItem | ReasoningItem


class Transcript(Protocol):
    protocol: ApiProtocol
    async def items(self) -> list[SessionItem]: ...
    async def append(self, items: list[SessionItem]) -> None: ...


class AgentLoop(Protocol):
    async def run(self, *, target: ModelTarget, system_prompt: str, user_text: str, session_id: str,
                  transcript: Transcript, tools: TurnTools, cancel: TurnCancel,
                  emit: Callable[[LoopEvent], Awaitable[None]], over_cap: Callable[[], bool],
                  max_requests: int = 30) -> LoopResult: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class CodecDone:
    items: tuple[SessionItem, ...]
    record: RequestRecord
    text: str
    tool_calls: tuple[ToolCall, ...]
    reasoning_summary: str | None


class CodecError(Exception):
    def __init__(self, kind: ErrorKind, status: int | None, message: str, *, retryable_stream: bool = False):
        self.kind, self.status = kind, status
        self.message = log_redaction.redact(message)[:300]
        self.retryable_stream = retryable_stream
        super().__init__(self.message)


class CodecStream(Protocol):
    def __aiter__(self) -> AsyncIterator[TextDelta | CodecDone]: ...
    def snapshot(self) -> RequestRecord: ...
    async def aclose(self) -> None: ...


class ProtocolCodec(Protocol):
    protocol: ApiProtocol
    def stream(self, *, target: ModelTarget, system_prompt: str,
               items: Sequence[SessionItem], specs: Sequence[ToolSpec],
               session_id: str, index: int) -> CodecStream: ...
    async def aclose(self) -> None: ...


class TranscriptFormatError(ValueError):
    """Stored neutral items do not match the versioned transcript format."""


def item_to_json(item: SessionItem) -> dict[str, Any]:
    kinds = {MessageItem: 'message', ToolCallItem: 'tool_call',
             ToolResultItem: 'tool_result', ReasoningItem: 'reasoning'}
    try:
        value = {'version': 1, 'kind': kinds[type(item)], **asdict(item)}
    except KeyError:
        raise TranscriptFormatError('unknown session item') from None
    # Validate both directions at the persistence boundary.
    item_from_json(value)
    if isinstance(item, ReasoningItem):
        value['summary'] = list(item.summary)
    return value


def item_from_json(value: Any) -> SessionItem:
    def required(obj, key, kind):
        field = obj[key]
        if type(field) is not kind:
            raise ValueError
        return field

    def text(obj, key):
        return required(obj, key, str)

    try:
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError
        kind = text(value, 'kind')
        if kind == 'message':
            role = text(value, 'role')
            if role not in ('user', 'assistant'):
                raise ValueError
            return MessageItem(role, text(value, 'text'))
        if kind == 'tool_call':
            call = required(value, 'call', dict)
            return ToolCallItem(ToolCall(*(text(call, k) for k in ('call_id', 'name', 'arguments'))))
        if kind == 'tool_result':
            return ToolResultItem(text(value, 'call_id'), text(value, 'text'),
                                  required(value, 'is_error', bool), required(value, 'ran', bool))
        if kind == 'reasoning':
            summary = value['summary']
            if not isinstance(summary, (list, tuple)) or any(type(s) is not str for s in summary):
                raise ValueError
            state = value['state']
            if state is not None:
                if not isinstance(state, dict):
                    raise ValueError
                state = ReplayState(ApiProtocol(state['protocol']),
                                    *(text(state, k) for k in ('model', 'item_id', 'encrypted_content')))
            return ReasoningItem(tuple(summary), state)
        raise ValueError
    except (KeyError, TypeError, ValueError):
        raise TranscriptFormatError('invalid versioned session item') from None


class NormaAgentLoop:
    def __init__(self, codec: ProtocolCodec):
        self.codec = codec

    async def run(self, *, target: ModelTarget, system_prompt: str, user_text: str, session_id: str,
                  transcript: Transcript, tools: TurnTools, cancel: TurnCancel,
                  emit: Callable[[LoopEvent], Awaitable[None]], over_cap: Callable[[], bool],
                  max_requests: int = 30) -> LoopResult:
        if target.protocol != transcript.protocol or target.protocol != self.codec.protocol:
            raise RuntimeUnsupported('target, transcript and codec protocols must match')
        if max_requests <= 0:
            raise ValueError('max_requests must be positive')
        log_redaction.add_secret(target.api_key)
        working = [*await transcript.items(), MessageItem('user', user_text)]
        specs = await tools.specs()
        records: list[RequestRecord] = []
        completed = 0
        pending_user = True
        resent = False
        prefix = ''

        def finish(outcome: Outcome, text: str = '', error: CodecError | None = None) -> LoopResult:
            message = log_redaction.redact(error.message) if error else None
            if message is not None and target.api_key:
                message = message.replace(target.api_key, log_redaction.MASK)
            return LoopResult(outcome, text, tuple(records), error.kind if error else None,
                              error.status if error else None, message[:300] if message else message,
                              completed == 0)

        while True:
            if cancel.requested:
                return finish(Outcome.CANCELLED)
            if len(records) >= max_requests:
                return finish(Outcome.MAX_REQUESTS)
            if records and over_cap():
                return finish(Outcome.CAPPED)
            stream = self.codec.stream(target=target, system_prompt=system_prompt, items=list(working),
                                       specs=specs, session_id=session_id, index=len(records))
            visible = ''

            async def consume() -> CodecDone:
                nonlocal visible
                received = ''
                async for event in stream:
                    if isinstance(event, TextDelta):
                        received += event.text
                        overlap = min(len(received), len(prefix))
                        if received[:overlap] != prefix[:overlap]:
                            raise CodecError(ErrorKind.PROTOCOL, None, 'resend text diverged from displayed prefix')
                        suffix = received[max(len(prefix), len(visible)):]
                        if suffix and not cancel.requested:
                            await emit(TextDelta(suffix))
                            visible = received
                    else:
                        if not event.text.startswith(prefix):
                            raise CodecError(ErrorKind.PROTOCOL, None, 'resend answer did not retain displayed prefix')
                        return event
                raise CodecError(ErrorKind.PROTOCOL, None, 'stream ended without completion', retryable_stream=True)

            request_task = asyncio.create_task(consume())
            stopped = asyncio.get_running_loop().create_future()
            unregister = cancel.on_request(request_task.cancel)
            unwake = cancel.on_request(lambda: stopped.set_result(None))
            try:
                try:
                    await asyncio.wait((request_task, stopped), return_when=asyncio.FIRST_COMPLETED)
                    if not request_task.done():
                        raise asyncio.CancelledError()
                    response = request_task.result()
                finally:
                    unregister()
                    unwake()
                    stopped.cancel()
                    await _close_request(stream, request_task)
            except asyncio.CancelledError:
                records.append(_unfinished(stream.snapshot()))
                # A service shutdown must propagate even if the turn was also stopped.
                if asyncio.current_task().cancelling() or not cancel.requested:
                    raise
                return finish(Outcome.CANCELLED)
            except CodecError as error:
                records.append(_unfinished(stream.snapshot()))
                if cancel.requested:
                    return finish(Outcome.CANCELLED)
                if error.retryable_stream and not resent:
                    resent = True
                    prefix = visible
                    continue
                return finish(Outcome.FAILED, error=error)

            records.append(response.record)
            completed += 1
            prefix = ''  # Earlier completed requests are never compared with a resend.
            if not cancel.requested:
                await emit(RequestDone(response.record, response.text, response.tool_calls, response.reasoning_summary))
            batch: list[SessionItem] = [MessageItem('user', user_text)] if pending_user else []
            batch.extend(response.items)
            for call in response.tool_calls:
                outcome = _STOPPED if cancel.requested else await tools.call(call)
                batch.append(ToolResultItem(call.call_id, outcome.text, outcome.is_error, outcome.ran))
                if not cancel.requested:
                    await emit(ToolDone(call, outcome))
            if not response.tool_calls and not any(isinstance(i, MessageItem) and i.role == 'assistant'
                                                   for i in response.items):
                batch.append(MessageItem('assistant', response.text))
            # Never retry after dispatch: persistence failures belong to the caller.
            await transcript.append(batch)
            working.extend(batch[1:] if pending_user else batch)
            pending_user = False
            if cancel.requested:
                return finish(Outcome.CANCELLED)
            if not response.tool_calls:
                return finish(Outcome.ANSWERED, response.text)

    async def aclose(self) -> None:
        await self.codec.aclose()


async def _close_request(stream: CodecStream, task: asyncio.Task[CodecDone]) -> None:
    async def cleanup() -> None:
        if not task.done() and not task.cancelling():
            task.cancel()
        try:
            await stream.aclose()  # Close before waiting for the request's cancellation cleanup.
        finally:
            await asyncio.gather(task, return_exceptions=True)

    closing = asyncio.create_task(cleanup())
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
    closing.result()
    if cancelled:
        raise asyncio.CancelledError()


def _unfinished(record: RequestRecord) -> RequestRecord:
    return replace(record, usage=replace(record.usage, complete=False))


_STOPPED = ToolOutcome(json.dumps({'status': 'error', 'error': {'code': 'stopped',
    'message': 'the user stopped this turn: this call did not run'}}, ensure_ascii=False), True, False)


def loop_for(protocol: ApiProtocol) -> AgentLoop:
    raise RuntimeUnsupported(f'no API codec implemented for {protocol.value}')
