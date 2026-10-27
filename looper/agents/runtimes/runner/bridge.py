"""One runner stdio connection. Process ownership and turn policy live elsewhere."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
import inspect
import json
import logging
import re
import time
from typing import TYPE_CHECKING
from typing import get_args

from looper import __version__
from ...claude_cli import BUDGET_SUBTYPE, user_stop_result
from ...log_redaction import redact
from . import protocol as p

if TYPE_CHECKING:
    from .turn import RunnerTurn, TurnSummary

logger = logging.getLogger(__name__)
MAX_ABANDONED_CONTROLS = 128
MAX_TOMBSTONES = 128
TOMBSTONE_MAX_AGE = 600.0
_LOCAL_CONTROLS = frozenset({"session/cancel", "session/close"})


class State(StrEnum):
    """Turn activity, independent of session admission and close ownership."""
    OPENING = "opening"
    IDLE = "idle"
    RUNNING = "running"


class CloseState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    CLOSE_FAILED = "close_failed"
    POISONED = "poisoned"


class ConnectionLost(ConnectionError):
    """Safe, bounded connection diagnostic; never includes a frame or provider."""


class RpcFailure(Exception):
    def __init__(self, error):
        self.code = error["code"]
        super().__init__(redact(error["message"])[:512])


@dataclass
class SessionEntry:
    session_key: str
    role: str
    session_id: str = ""
    state: State = State.OPENING
    close_state: CloseState = CloseState.OPEN
    close_task: asyncio.Task | None = field(default=None, repr=False)
    local_error: str | None = None
    trace_id: str | None = None
    turn: RunnerTurn | None = field(default=None, repr=False)
    on_event: Callable | None = field(default=None, repr=False)
    prompt_sent: bool = False
    session_started: bool = False
    stopped: bool = False
    initialized: bool = False
    accepted: bool = False
    settled_received: bool = False
    requests_ended: int = 0
    authorized: dict[str, str] = field(default_factory=dict, repr=False)
    invalid_usage: bool = False
    summary: TurnSummary | None = None
    result_event: dict | None = field(default=None, repr=False)
    settled: asyncio.Future | None = field(default=None, repr=False)
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    updates: deque = field(default_factory=deque, repr=False)
    worker: asyncio.Task | None = field(default=None, repr=False)
    traces: set[str] = field(default_factory=set, repr=False)


@dataclass
class Pending:
    method: str
    params: dict = field(repr=False)
    future: asyncio.Future = field(repr=False)
    entry: SessionEntry | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Tombstone:
    session_id: str
    trace_id: str | None
    outcome: str | None
    closed_at: float


async def _value(value):
    return await value if inspect.isawaitable(value) else value


class _FrameReader:
    """Retain only the current bounded frame to answer unknown reverse methods.

    The codec rejects unknown methods before returning their id. All known
    envelopes, framing limits and malformed stdout still use its decoder.
    """
    def __init__(self, reader):
        self.reader, self.parts = reader, []

    @property
    def _limit(self):
        return self.reader._limit

    @_limit.setter
    def _limit(self, value):
        self.reader._limit = value

    async def readuntil(self, separator):
        chunk = await self.reader.readuntil(separator)
        self.parts.append(chunk)
        return chunk

    async def readexactly(self, size):
        chunk = await self.reader.readexactly(size)
        self.parts.append(chunk)
        return chunk

    async def read(self):
        self.parts.clear()
        try:
            return await p.read_frame(self)
        except p.ProtocolError:
            # Unknown methods have no schema; validate the envelope, not its payload.
            if (self.parts and self.parts[-1].endswith(b"\n")
                    and sum(map(len, self.parts)) <= p.MAX_FRAME_BYTES + 1):
                try:
                    raw = json.loads(b"".join(self.parts),
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if (isinstance(raw, dict) and raw.get("jsonrpc") == "2.0"
                            and isinstance(raw.get("method"), str) and raw["method"]
                            and raw["method"] not in p.PARAMS and isinstance(raw.get("params"), dict)
                            and isinstance(raw.get("id"), str) and re.fullmatch(r"r-[0-9]+", raw["id"])
                            and not {"result", "error"} & raw.keys()):
                        return p.Request(raw["id"], raw["method"], raw["params"])
                except (ValueError, UnicodeError, RecursionError):
                    pass
            raise
        finally:
            self.parts.clear()


class RunnerBridge:
    def __init__(self, reader, writer, kind: str, *, control_timeout: float = 10,
                 max_updates: int = 128, on_failure: Callable | None = None):
        if control_timeout <= 0 or max_updates < 1:
            raise ValueError("runner bounds must be positive")
        self.writer, self.kind = writer, kind
        self.control_timeout, self.max_updates = control_timeout, max_updates
        self.on_failure = on_failure
        self.sessions: dict[str, SessionEntry] = {}
        self.tombstones: dict[str, Tombstone] = {}
        self._pending: dict[str, Pending] = {}
        self._abandoned_controls: dict[str, tuple[str, dict]] = {}
        self._reverse_tasks: set[asyncio.Task] = set()
        self._reverse_ids: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._counter = 0
        self._initialized = self._initialize_attempted = False
        self.capabilities: dict = {}
        self._error: ConnectionLost | None = None
        self._close_task: asyncio.Task | None = None
        self._reader = _FrameReader(reader)
        self._reader_task = asyncio.create_task(self._read())

    async def initialize(self, *, permitted=get_args(p.RequestKind)):
        if self._initialize_attempted:
            raise RuntimeError("runner initialize may only be called once")
        self._initialize_attempted = True
        result = await self.request("initialize", {"protocol_versions": [p.PROTOCOL_VERSION],
            "looper_version": __version__, "host_capabilities": {
                "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}})
        try:
            p.validate_initialize(result, [p.PROTOCOL_VERSION], self.kind, permitted)
        except p.ProtocolError:
            self._fail(ConnectionLost("runner protocol-error: initialize rejected"))
            raise
        self.capabilities = result.get("capabilities", {})
        self._initialized = True
        return result

    async def _write(self, message, entry=None):
        frame = p.encode_frame(message)
        async with self._write_lock:
            if self._error:
                raise self._error
            try:
                self.writer.write(frame)
                if entry is not None:
                    entry.prompt_sent = True
                await self.writer.drain()
            except (OSError, RuntimeError):
                self._fail(ConnectionLost("runner-exited: stdin unavailable"))
                raise self._error from None

    async def request(self, method, params, *, _entry=None):
        if self._error:
            raise self._error
        if method != "initialize" and not self._initialized:
            raise RuntimeError("runner is not initialized")
        if method in _LOCAL_CONTROLS and (len(self._abandoned_controls)
                + sum(pending.method in _LOCAL_CONTROLS for pending in self._pending.values())
                >= MAX_ABANDONED_CONTROLS):
            raise p.ProtocolError(p.ErrorCode.CAPACITY)
        params = p.decode_params(method, params)  # Strip host-only objects before encoding.
        self._counter += 1
        rpc_id = f"l-{self._counter}"
        future = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = Pending(method, params, future, _entry)
        try:
            async with asyncio.timeout(self.control_timeout):
                await self._write(p.Request(rpc_id, method, params), _entry)
                return await asyncio.shield(future)
        except (TimeoutError, asyncio.CancelledError):
            if method in _LOCAL_CONTROLS:
                # Cancel and close cannot admit new work. Keep bounded correlation
                # state so its late response cannot take down another session.
                if not future.done():
                    self._abandoned_controls[rpc_id] = (method, params)
            else:
                # A late acceptance cannot make an abandoned prompt runnable again.
                self._fail(ConnectionLost("runner protocol-error: control abandoned or timed out"))
            raise
        finally:
            self._pending.pop(rpc_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Also consume a failure raised while writing.

    async def open_session(self, params):
        entry = SessionEntry(params["session_key"], params["role"])
        result = await self.request("session/new", params)
        sid = result["session_id"]
        self._prune_tombstones()
        if sid in self.sessions or sid in self.tombstones:
            self._fail(ConnectionLost("runner protocol-error: duplicate session id"))
            raise self._error
        entry.session_id, entry.state = sid, State.IDLE
        self.sessions[sid] = entry
        return sid

    def _entry(self, session_id, trace_id=None):
        entry = self.sessions[session_id]
        if trace_id is not None and entry.trace_id != trace_id:
            raise p.ProtocolError(p.ErrorCode.TRACE_MISMATCH)
        return entry

    async def begin_turn(self, session_id, trace_id, on_event, turn, *, text: str, tools=None):
        entry = self._entry(session_id)
        if self._error:
            raise self._error
        if entry.close_state != CloseState.OPEN or entry.state != State.IDLE or trace_id in entry.traces:
            raise p.ProtocolError(p.ErrorCode.SESSION_BUSY)
        # No await until the active trace, policy and consumer have all been installed.
        entry.state, entry.trace_id, entry.turn, entry.on_event = State.RUNNING, trace_id, turn, on_event
        entry.traces.add(trace_id)
        entry.prompt_sent = entry.session_started = entry.accepted = entry.stopped = entry.settled_received = False
        entry.summary = entry.result_event = None
        entry.requests_ended = 0
        entry.authorized.clear()
        entry.invalid_usage = False
        entry.updates.clear()
        entry.ready, entry.wake = asyncio.Event(), asyncio.Event()
        entry.settled = asyncio.get_running_loop().create_future()
        entry.worker = asyncio.create_task(self._updates(entry))
        try:
            return await self.request("session/prompt", {"session_id": session_id, "trace_id": trace_id,
                "text": text, "tools": tools or []}, _entry=entry)
        except (RpcFailure, p.ProtocolError) as error:
            if not isinstance(error, RpcFailure) and entry.prompt_sent:
                self._fail(ConnectionLost("runner protocol-error: prompt rejected"))
                raise
            # A correlated rejection abandons only this prompt, not the connection.
            # Local schema/encoding failures have not written anything to the pipe.
            entry.stopped = True
            entry.worker.cancel()
            await asyncio.gather(entry.worker, return_exceptions=True)
            await _value(entry.turn.stop())
            entry.updates.clear()
            entry.settled.cancel()
            entry.settled = None
            entry.state = State.IDLE
            raise
        except BaseException:
            entry.stopped = True
            entry.ready.set()
            self._fail(ConnectionLost("runner protocol-error: prompt rejected"))
            raise

    async def wait_settled(self, session_id, trace_id):
        entry = self._entry(session_id, trace_id)
        if entry.settled is None:
            raise p.ProtocolError(p.ErrorCode.NOT_RUNNING)
        return await asyncio.shield(entry.settled)

    async def cancel_turn(self, session_id, trace_id):
        entry = self._entry(session_id, trace_id)
        if entry.close_state != CloseState.OPEN or entry.state != State.RUNNING:
            raise p.ProtocolError(p.ErrorCode.NOT_RUNNING)
        entry.stopped = True
        await _value(entry.turn.stop())
        return await self.request("session/cancel", {"session_id": session_id, "trace_id": trace_id})

    async def steer(self, session_id, trace_id, message_id, text):
        entry = self._entry(session_id, trace_id)
        if entry.close_state != CloseState.OPEN or entry.state != State.RUNNING or entry.stopped:
            raise p.ProtocolError(p.ErrorCode.NOT_RUNNING)
        if not self.capabilities.get("steer", {}).get("in_trace", False):
            raise p.ProtocolError(p.ErrorCode.UNSUPPORTED)
        return await self.request("session/steer", {"session_id": session_id, "trace_id": trace_id,
            "message_id": message_id, "text": text, "if_idle": "reject"})

    def _transition_close(self, entry: SessionEntry, state: CloseState, reason: str) -> None:
        previous = entry.close_state
        # Poison is irreversible, including while a cancel is awaiting its reply.
        if previous == CloseState.POISONED or previous == state:
            return
        allowed = {
            CloseState.OPEN: {CloseState.CLOSING, CloseState.POISONED},
            CloseState.CLOSING: {CloseState.CLOSED, CloseState.CLOSE_FAILED, CloseState.POISONED},
            CloseState.CLOSE_FAILED: {CloseState.CLOSING, CloseState.POISONED},
            CloseState.CLOSED: set(),
        }
        if state not in allowed[previous]:
            raise RuntimeError(f"invalid runner close transition: {previous} -> {state}")
        entry.close_state = state
        logger.debug("Runner session %s close %s -> %s: %s", entry.session_id, previous, state, reason)

    def _prune_tombstones(self) -> None:
        now = time.monotonic()
        while self.tombstones:
            sid, tombstone = next(iter(self.tombstones.items()))
            if len(self.tombstones) <= MAX_TOMBSTONES and now - tombstone.closed_at < TOMBSTONE_MAX_AGE:
                break
            del self.tombstones[sid]

    async def close_session(self, session_id) -> CloseState:
        entry = self.sessions.get(session_id)
        if entry is None:
            return CloseState.CLOSED
        if entry.close_task is None or entry.close_task.done():
            self._transition_close(entry, CloseState.CLOSING, "close requested")
            entry.stopped = True
            entry.close_task = asyncio.create_task(self._close_session(entry))
            # The bridge owns the attempt even if its caller stops waiting.
            entry.close_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.shield(entry.close_task)

    async def _close_session(self, entry: SessionEntry) -> CloseState:
        params = {"session_id": entry.session_id}
        if entry.settled is not None and not entry.settled.done():
            params["trace_id"] = entry.trace_id
        try:
            if entry.turn is not None:
                await _value(entry.turn.stop())
            await self.request("session/close", params)
            if entry.settled is not None and not entry.settled.done():
                # Settlement drains admitted tools, including user confirmations. A
                # slow tool is not evidence that the shared control pipe is dead.
                await asyncio.shield(entry.settled)
        except (Exception, asyncio.CancelledError) as error:
            if not self._error:
                self._transition_close(entry, CloseState.CLOSE_FAILED, "close failed; retry retains ownership")
                entry.ready.set()
                if entry.turn is not None and entry.summary is None:
                    # The single consumer finishes any admitted callback before
                    # local settlement; never cancel or replay a tool write.
                    entry.local_error = "runner session close failed"
                    entry.updates.clear()
                    # A rejected in-flight prompt cancels both its consumer and
                    # settlement future. Restore them for local failure cleanup.
                    if entry.settled is None or entry.settled.cancelled():
                        entry.settled = asyncio.get_running_loop().create_future()
                    if entry.worker is None or entry.worker.done():
                        entry.worker = asyncio.create_task(self._updates(entry))
                    entry.wake.set()
                if entry.settled is not None and not entry.settled.done():
                    await asyncio.shield(entry.settled)
                self.tombstones[entry.session_id] = Tombstone(entry.session_id, entry.trace_id,
                    entry.summary.outcome if entry.summary else None, time.monotonic())
                self._prune_tombstones()
                if isinstance(error, asyncio.CancelledError) and not asyncio.current_task().cancelling():
                    # Only the internal settlement was cancelled, not this task.
                    return entry.close_state
            raise
        self._transition_close(entry, CloseState.CLOSED, "close acknowledged and settlement drained")
        self.tombstones[entry.session_id] = Tombstone(entry.session_id, entry.trace_id,
            entry.summary.outcome if entry.summary else None, time.monotonic())
        self._prune_tombstones()
        self.sessions.pop(entry.session_id, None)
        return CloseState.CLOSED

    async def _read(self):
        try:
            while True:
                message = await self._reader.read()
                if isinstance(message, p.Response):
                    abandoned = self._abandoned_controls.pop(message.id, None)
                    if abandoned is not None:
                        method, params = abandoned
                        if message.error is not None:
                            p.verify_error(message.error, params)
                        else:
                            p.decode_result(method, message.result, params)
                        logger.debug("Dropped late %s response", method)
                        continue
                    pending = self._pending.get(message.id)
                    if not pending or not message.id.startswith("l-") or pending.future.done():
                        raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
                    if message.error is not None:
                        p.verify_error(message.error, pending.params)
                        pending.future.set_exception(RpcFailure(message.error))
                    else:
                        result = p.decode_result(pending.method, message.result, pending.params)
                        if pending.entry is not None:
                            pending.entry.accepted = pending.entry.session_started = True
                            pending.entry.ready.set()
                        pending.future.set_result(result)
                elif isinstance(message, p.Request):
                    if not message.id.startswith("r-") or message.id in self._reverse_ids:
                        raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
                    self._reverse_ids.add(message.id)
                    task = asyncio.create_task(self._reverse(message))
                    self._reverse_tasks.add(task)
                    task.add_done_callback(self._reverse_tasks.discard)
                elif message.method == "session/update":
                    self._enqueue(message.params)
                else:
                    raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
        except EOFError:
            self._fail(ConnectionLost("runner-exited: stdout EOF"))
        except ConnectionLost as error:
            self._fail(error)
        except p.ProtocolError as error:
            self._fail(ConnectionLost(f"runner protocol-error: {error.code.name}"))
        except Exception:
            self._fail(ConnectionLost("runner protocol-error: invalid stdout or correlation"))

    def _enqueue(self, update):
        self._prune_tombstones()
        if update["session_id"] in self.tombstones:
            logger.debug("Dropped late %s for a closed session", update["kind"])
            return
        entry = self.sessions.get(update["session_id"])
        if entry is None:
            logger.debug("Dropped %s for a missing session", update["kind"])
            return
        if entry.close_state in (CloseState.POISONED, CloseState.CLOSE_FAILED) or entry.summary is not None:
            logger.debug("Dropped update for a poisoned or summarized session")
            return
        if entry.state != State.RUNNING:
            return  # Rejected prompts cannot enqueue work for the next turn.
        if entry.trace_id != update["trace_id"] or entry.settled_received:
            if update["kind"] == "text_delta":
                return  # A stale display delta cannot create effects or reopen a trace.
            raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
        if update["kind"] == "settled":
            entry.settled_received = entry.stopped = True
        if (update["kind"] == "text_delta" and entry.updates
                and entry.updates[-1]["kind"] == "text_delta"):
            text = entry.updates[-1]["text"] + update["text"]
            if len(text.encode("utf-8")) > p.MAX_FRAME_BYTES:
                raise ConnectionLost("runner protocol-error: merged text exceeds byte bound")
            entry.updates[-1] = dict(update, text=text)
        elif len(entry.updates) >= self.max_updates:
            raise ConnectionLost("runner protocol-error: session update queue exceeds bound")
        else:
            entry.updates.append(update)
        entry.wake.set()

    async def _reverse(self, message):
        params = message.params
        data = {k: params[k] for k in ("session_id", "trace_id") if k in params}
        try:
            if message.method not in {"tools/call", "budget/authorize"}:
                raise p.ProtocolError(p.ErrorCode.METHOD_NOT_FOUND)
            entry = self.sessions.get(params["session_id"])
            if entry is None or entry.trace_id != params["trace_id"]:
                raise p.ProtocolError(p.ErrorCode.TRACE_MISMATCH)
            if entry.stopped or entry.close_state != CloseState.OPEN or entry.state != State.RUNNING or self._error:
                result = ({"allow": False, "reason": "stopped"} if message.method == "budget/authorize" else
                          {"text": "stopped", "is_error": True, "ran": False})
            else:
                if (message.method == "budget/authorize"
                        and params["kind"] not in self.capabilities.get("authorize_covers", [])):
                    raise p.ProtocolError(p.ErrorCode.UNSUPPORTED)
                handler = entry.turn.call_tool if message.method == "tools/call" else entry.turn.authorize
                result = await _value(handler(params))
                if message.method == "budget/authorize" and result.get("allow") is True:
                    # The policy already owns this reservation. A concurrent stop
                    # cancels the turn; it must not rewrite a committed allowance.
                    entry.authorized[params["request_id"]] = params["kind"]
            response = p.Response(message.id, result={**result, **data})
        except p.ProtocolError as error:
            response = p.Response(message.id, error={"code": int(error.code), "message": error.code.name, "data": data})
        except Exception:
            # Host exception strings can contain unregistered secrets or arbitrary tool input.
            response = p.Response(message.id, error={"code": int(p.ErrorCode.INTERNAL_ERROR),
                "message": "runner host callback failed", "data": data})
        try:
            await self._write(response)
        except ConnectionLost:
            pass
        except Exception:
            self._fail(ConnectionLost("runner protocol-error: invalid host response"))
        finally:
            self._reverse_ids.discard(message.id)

    async def _emit(self, entry, event):
        await _value(entry.on_event({"session_id": entry.session_id, "trace_id": entry.trace_id, **event}))

    async def _updates(self, entry):
        try:
            await entry.ready.wait()
            if entry.accepted and not entry.initialized:
                entry.initialized = True
                await self._emit(entry, {"type": "system", "subtype": "init"})
            while entry.summary is None:
                if entry.local_error is not None:
                    await self._settle(entry, {"session_id": entry.session_id, "trace_id": entry.trace_id,
                        "outcome": "failed", "final_text": "", "usage_complete": False,
                        "requests": entry.requests_ended,
                        "error": {"kind": "INTERNAL_ERROR", "message": entry.local_error}})
                    return
                while entry.updates:
                    update = entry.updates.popleft()
                    kind = update["kind"]
                    if kind == "settled":
                        await self._settle(entry, update)
                        return
                    if kind == "text_delta":
                        event = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": update["text"]}}}
                    elif kind == "message":
                        event = {"type": "assistant", "message": {"role": "assistant", "model": update["model"],
                            "content": update["content"]}}
                    elif kind == "tool_result":
                        event = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result",
                            "tool_use_id": update["call_id"], "content": update["text"], "is_error": update["is_error"]}]}}
                    elif kind == "request_end":
                        authorized = entry.authorized.pop(update["request_id"], None)
                        approved = (authorized == update["request_kind"]
                                    and update["request_kind"] in self.capabilities.get("authorize_covers", []))
                        try:
                            if approved:
                                event = await _value(entry.turn.request_end(update))
                                entry.requests_ended += 1
                            else:
                                entry.invalid_usage = True
                                if entry.state == State.RUNNING and not entry.stopped:
                                    await self.cancel_turn(entry.session_id, entry.trace_id)
                                continue
                        except (Exception, asyncio.CancelledError):
                            if self._error:
                                raise self._error
                            # A callback can record usage before settings or its
                            # cancel callback fails. Finish still reconciles it.
                            if approved:
                                entry.requests_ended += 1
                            cancel_needed = not entry.stopped
                            self._transition_close(entry, CloseState.POISONED, "request_end processing failed")
                            entry.updates.clear()
                            if cancel_needed:
                                # Quarantine updates before awaiting cancellation.
                                # Do not repeat an earlier stop or cancel attempt.
                                entry.stopped = True
                                try:
                                    await _value(entry.turn.stop())
                                    await self.request("session/cancel", {"session_id": entry.session_id,
                                        "trace_id": entry.trace_id})
                                except (Exception, asyncio.CancelledError):
                                    if self._error:
                                        raise self._error
                            await self._settle(entry, {"session_id": entry.session_id, "trace_id": entry.trace_id,
                                "outcome": "failed", "final_text": "",
                                "usage_complete": approved, "requests": entry.requests_ended,
                                "error": {"kind": "INTERNAL_ERROR", "message": "runner request_end processing failed"}})
                            return
                    else:
                        event = {"type": "system", "subtype": kind, "runner": update}
                    await self._emit(entry, event)
                if self._error:
                    break
                if entry.local_error is not None:
                    continue
                entry.wake.clear()
                await entry.wake.wait()
        except Exception:
            self._fail(ConnectionLost("runner protocol-error: update processing failed"))
        if entry.summary is None:
            try:
                await self._settle(entry, {"outcome": "lost", "final_text": "", "usage_complete": False,
                    "requests": entry.requests_ended,
                    "session_id": entry.session_id, "trace_id": entry.trace_id,
                    "error": {"kind": "INTERNAL_ERROR", "message": str(self._error)}})
            except Exception:
                if not entry.settled.done():
                    entry.settled.set_exception(self._error)

    async def _settle(self, entry, settled):
        entry.stopped = True
        await _value(entry.turn.stop())
        if entry.invalid_usage:
            settled = {**settled, "usage_complete": False, "requests": entry.requests_ended,
                       "outcome": "lost" if settled["outcome"] == "lost" else "failed",
                       "error": {"kind": "INVALID_REQUEST", "message": "runner usage lacks matching authorization"}}
        summary = await _value(entry.turn.finish(settled))
        entry.summary = summary
        outcome = summary.outcome
        event = {"type": "result", "subtype": "success", "is_error": False, "result": settled["final_text"]}
        if outcome == "cancelled":
            event.update(user_stop_result())
        elif outcome != "completed":
            subtype = BUDGET_SUBTYPE if outcome == "capped" else (
                "error_max_turns" if outcome == "max_requests" else "error_during_execution")
            reason = (f"runner authorization stopped: {summary.cap_reason}" if outcome == "capped" else
                      "runner reached its request limit" if outcome == "max_requests" else
                      redact(settled.get("error", {}).get("message", "runner failed"))[:512])
            event.update(subtype=subtype, is_error=True, result="", errors=[reason])
        u = summary.usage
        event.update(session_id=entry.session_id, trace_id=entry.trace_id,
            modelUsage=summary.model_usage, total_cost_usd=summary.totals.cost_usd,
            usage={"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                   "cache_read_input_tokens": u.cache_read_tokens, "cache_creation_input_tokens": u.cache_creation_tokens},
            usage_complete=summary.usage_complete, num_turns=settled.get("requests", 0),
            runner={"outcome": outcome, "cap_reason": summary.cap_reason, "error_kind": settled.get("error", {}).get("kind")})
        entry.result_event = event
        try:
            # Settlement (including loss) remains terminal if acceptance never arrived.
            await self._emit(entry, event)
        finally:
            if self._error or outcome == "lost":
                self._transition_close(entry, CloseState.POISONED, "trace lost")
            entry.state = State.IDLE
            entry.settled.set_result(summary)

    def _fail(self, error):
        if self._close_task is not None:
            return
        self._error = error
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(error)
        for entry in self.sessions.values():
            entry.stopped = True
            entry.ready.set()
            entry.wake.set()
            self._transition_close(entry, CloseState.POISONED, "connection lost")
        self._close_task = asyncio.create_task(self._shutdown())

    async def _shutdown(self):
        if self.on_failure:
            try:
                await _value(self.on_failure(self._error))
            except Exception:
                pass
        self._reader_task.cancel()
        self.writer.close()  # Close this connection only; never terminate the process.
        await asyncio.gather(self._reader_task, return_exceptions=True)
        tasks = [task for entry in self.sessions.values() for task in (entry.worker, entry.close_task)
                 if task is not None]
        # Started tool writes belong to the turn adapter; join them rather than cancel/retry.
        await asyncio.gather(*tasks, *self._reverse_tasks, return_exceptions=True)
        try:
            await self.writer.wait_closed()
        except (OSError, RuntimeError):
            pass

    async def close(self, error=None):
        self._fail(ConnectionLost("runner-exited: connection closed" if error is None else
                                  "runner protocol-error: connection closed"))
        await asyncio.shield(self._close_task)
