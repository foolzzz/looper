"""Runner 0.1 wire contract. Owners enforce initialize-once/before-session,
active traces, matching response ids and settled-last ordering; corruption is fatal.
"""
from __future__ import annotations

import asyncio
import json
import re
import types
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Annotated, Any, Literal, NotRequired, TypedDict, Union, cast, get_args, get_origin, get_type_hints

PROTOCOL_VERSION = "0.1"
MAX_FRAME_BYTES = 4 * 1024 * 1024
Count = Annotated[int, "nonnegative"]
Name = Annotated[str, "nonempty"]
RpcId = Annotated[str, r"[lr]-[0-9]+"]
Version = Annotated[str, r"[0-9]+\.[0-9]+"]
RequestKind = Literal["turn", "retry", "compaction", "subagent", "tool", "title", "other"]
HostCapabilities = TypedDict("HostCapabilities", {
    "tools_call": Literal[True], "budget_authorize": Literal[True], "bus": Literal[False], "exec_slots": Literal[False]})
InitializeParams = TypedDict("InitializeParams", {
    "protocol_versions": list[Version], "looper_version": Name, "host_capabilities": HostCapabilities})
SteerCapability = TypedDict("SteerCapability", {
    "in_trace": bool, "boundary": str, "idle_start": Literal[False]}, total=False)
UsageCapability = TypedDict("UsageCapability", {
    "per_request": bool, "buckets": bool, "reasoning_in_output": bool}, total=False)
Capabilities = TypedDict("Capabilities", {
    "per_session_isolation": bool, "per_session_credentials": bool, "max_sessions": Count,
    "cancel": bool, "steer": SteerCapability, "usage": UsageCapability, "authorize_covers": list[RequestKind]}, total=False)
RunnerIdentity = TypedDict("RunnerIdentity", {"kind": Name, "version": Name})
SdkIdentity = TypedDict("SdkIdentity", {"name": Name, "version": Name})
InitializeResult = TypedDict("InitializeResult", {
    "protocol_version": Version, "runner": RunnerIdentity, "sdk": SdkIdentity, "capabilities": NotRequired[Capabilities]})
Provider = TypedDict("Provider", {
    "account_id": Name, "reasoning": bool,
    "protocol": Name, "base_url": Name, "model": Name, "api_key": str, "effort": str | None, "context_window": Count | None})
SystemPrompt = TypedDict("SystemPrompt", {"mode": Name, "text": str})
PlatformTool = TypedDict("PlatformTool", {"name": Name, "description": str, "inputSchema": dict[str, Any]})
Tools = TypedDict("Tools", {"builtin": list[Name], "read_only": bool, "platform": list[PlatformTool]})
Limits = TypedDict("Limits", {
    "max_requests_per_turn": Count, "tool_timeout_s": Literal[120], "tool_output_bytes": Literal[102400]})
NewSessionParams = TypedDict("NewSessionParams", {
    "session_key": Annotated[str, r"[A-Za-z0-9_-]+"], "role": Literal["manager", "planner"], "cwd": Name,
    "add_dirs": list[str], "env": dict[str, str], "home": Name, "state_dir": Name, "provider": Provider,
    "system_prompt": SystemPrompt, "tools": Tools, "limits": Limits})
Session = TypedDict("Session", {"session_id": Name})
LoadSessionParams = TypedDict("LoadSessionParams", {**NewSessionParams.__annotations__, "session_id": Name})
Loaded = TypedDict("Loaded", {"session_id": Name, "resumed": Literal[True]})
Turn = TypedDict("Turn", {"session_id": Name, "trace_id": Name})
CloseParams = TypedDict("CloseParams", {"session_id": Name, "trace_id": NotRequired[Name]})
PromptParams = TypedDict("PromptParams", {"session_id": Name, "trace_id": Name, "text": str, "tools": list[PlatformTool]})
SteerParams = TypedDict("SteerParams", {
    "session_id": Name, "trace_id": Name, "message_id": Name, "text": str, "if_idle": Literal["reject"]})
ToolCallParams = TypedDict("ToolCallParams", {
    "session_id": Name, "trace_id": Name, "call_id": Name, "name": Name, "arguments": Any})
AuthorizeParams = TypedDict("AuthorizeParams", {
    "session_id": Name, "trace_id": Name, "request_id": Name, "kind": RequestKind, "provider": Name,
    "model": Name, "estimated_input_tokens": Count, "max_output_tokens": Count})
TextBlock = TypedDict("TextBlock", {"type": Literal["text"], "text": str})
ToolUseBlock = TypedDict("ToolUseBlock", {"type": Literal["tool_use"], "id": Name, "name": Name, "input": dict[str, Any]})
TurnError = TypedDict("TurnError", {
    "kind": Literal["PARSE_ERROR", "INVALID_REQUEST", "METHOD_NOT_FOUND", "INVALID_PARAMS", "INTERNAL_ERROR",
                    "SESSION_NOT_FOUND", "SESSION_OWNED", "SESSION_BUSY", "NOT_RUNNING", "TRACE_MISMATCH", "CAPACITY",
                    "UNSUPPORTED", "ISOLATION_REFUSED", "VERSION_MISMATCH", "PROVIDER_AUTH", "PROVIDER_UNAVAILABLE",
                    "CONTEXT_OVERFLOW", "OUTPUT_TRUNCATED"], "message": str})
TextDelta = TypedDict("TextDelta", {"session_id": Name, "trace_id": Name, "kind": Literal["text_delta"], "text": str})
MessageUpdate = TypedDict("MessageUpdate", {
    "session_id": Name, "trace_id": Name, "kind": Literal["message"], "model": Name, "content": list[TextBlock | ToolUseBlock]})
ToolResultUpdate = TypedDict("ToolResultUpdate", {
    "session_id": Name, "trace_id": Name, "kind": Literal["tool_result"], "call_id": Name, "text": str, "is_error": bool})
RequestEnd = TypedDict("RequestEnd", {
    "session_id": Name, "trace_id": Name, "kind": Literal["request_end"], "request_id": Name, "request_kind": RequestKind,
    "model": Name, "status": Name, "input_uncached": Count, "output": Count, "cache_read": Count, "cache_write": Count,
    "reasoning": Count | None, "complete": bool})
SteerApplied = TypedDict("SteerApplied", {
    "session_id": Name, "trace_id": Name, "kind": Literal["steer_applied"], "message_id": Name,
    "after_request_id": Name | None, "after_tool_batch": bool, "new_turn": Literal[False]})
SteerDropped = TypedDict("SteerDropped", {
    "session_id": Name, "trace_id": Name, "kind": Literal["steer_dropped"], "message_id": Name,
    "reason": Literal["cancelled", "turn_ended", "session_closed"]})
Settled = TypedDict("Settled", {
    "session_id": Name, "trace_id": Name, "kind": Literal["settled"],
    "outcome": Literal["completed", "cancelled", "capped", "max_requests", "failed"], "final_text": str,
    "requests": Count, "usage_complete": bool, "error": NotRequired[TurnError]})
Update = TextDelta | MessageUpdate | ToolResultUpdate | RequestEnd | SteerApplied | SteerDropped | Settled
Accepted = TypedDict("Accepted", {"session_id": Name, "trace_id": Name, "accepted": Literal[True]})
Queued = TypedDict("Queued", {"session_id": Name, "trace_id": Name, "status": Literal["queued"]})
ToolResult = TypedDict("ToolResult", {"session_id": Name, "trace_id": Name, "text": str, "is_error": bool, "ran": bool})
Allowed = TypedDict("Allowed", {"session_id": Name, "trace_id": Name, "allow": Literal[True]})
Denied = TypedDict("Denied", {
    "session_id": Name, "trace_id": Name, "allow": Literal[False],
    "reason": Literal["run_cap", "turn_cap", "task_cap", "max_requests", "stopped", "budget_unverifiable"]})
RpcError = TypedDict("RpcError", {"code": int, "message": str, "data": NotRequired[dict[str, Any]]})
Empty = TypedDict("Empty", {})
Health = TypedDict("Health", {
    "sessions": Count, "running": Count, "inflight_requests": Count, "tool_processes": Count,
    "rss_bytes": Count, "event_loop_lag_ms": Count})
Params = InitializeParams | NewSessionParams | LoadSessionParams | PromptParams | SteerParams | Turn | CloseParams | ToolCallParams | AuthorizeParams | Update | Empty
Result = InitializeResult | Session | Loaded | Accepted | Queued | Turn | Empty | ToolResult | Allowed | Denied | Health
PARAMS = {"initialize": InitializeParams, "runner/health": Empty, "session/new": NewSessionParams,
          "session/load": LoadSessionParams, "session/prompt": PromptParams,
          "session/steer": SteerParams, "session/cancel": Turn, "session/close": CloseParams,
          "session/update": Update, "tools/call": ToolCallParams, "budget/authorize": AuthorizeParams}
RESULTS = {"initialize": InitializeResult, "runner/health": Health, "session/new": Session, "session/load": Loaded,
           "session/prompt": Accepted,
           "session/steer": Queued, "session/cancel": Turn, "session/close": Empty,
           "tools/call": ToolResult, "budget/authorize": Allowed | Denied}


class ErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SESSION_NOT_FOUND = -32001
    SESSION_OWNED = -32002
    SESSION_BUSY = -32003
    NOT_RUNNING = -32004
    TRACE_MISMATCH = -32005
    CAPACITY = -32006
    UNSUPPORTED = -32007
    ISOLATION_REFUSED = -32008
    VERSION_MISMATCH = -32009
    PROVIDER_AUTH = -32010
    PROVIDER_UNAVAILABLE = -32011
    CONTEXT_OVERFLOW = -32012
    OUTPUT_TRUNCATED = -32013


class ProtocolError(ValueError):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code.name)  # Never retain the offending payload.


class Payload(dict):
    """Decoded payloads may hold credentials, environment or tool input."""
    def __repr__(self):
        return "<runner payload>"


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    params: Params = field(repr=False)


@dataclass(frozen=True)
class Notification:
    method: str
    params: Params = field(repr=False)


@dataclass(frozen=True)
class Response:
    id: str | None
    result: dict[str, Any] | None = field(default=None, repr=False)
    error: RpcError | None = field(default=None, repr=False)


Message = Request | Notification | Response


def _decode(shape, value):
    origin, args = get_origin(shape), get_args(shape)
    if origin in (Annotated, NotRequired):
        decoded = _decode(args[0], value)
        rule = args[1] if origin is Annotated else None
        if ((rule == "nonnegative" and decoded < 0) or (rule == "nonempty" and not decoded)
                or (rule == "empty" and decoded) or
                (rule not in {None, "nonnegative", "nonempty", "empty"} and not re.fullmatch(rule, decoded))):
            raise ValueError
        return decoded
    if origin in (types.UnionType, Union):
        for option in args:
            try:
                return _decode(option, value)
            except (ValueError, TypeError):
                pass
        raise ValueError
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in args):
            raise ValueError
    elif hasattr(shape, "__required_keys__"):
        if type(value) not in (dict, Payload) or not shape.__required_keys__ <= value.keys():
            raise ValueError
        return Payload({key: _decode(item, value[key]) for key, item in get_type_hints(shape, include_extras=True).items()
                        if key in value})
    elif origin in (list, dict):
        if not isinstance(value, origin):
            raise ValueError
        return ([_decode(args[0], item) for item in value] if origin is list else
                Payload({_decode(args[0], key): _decode(args[1], item) for key, item in value.items()}))
    elif shape is not Any and type(value) is not shape:
        raise ValueError
    return value


def decode_params(method: str, params: object) -> Params:
    if method not in PARAMS:
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND)
    try:
        return cast(Params, _decode(PARAMS[method], params))
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError(ErrorCode.INVALID_PARAMS) from None


def _verify_correlation(value: dict, params: Params) -> None:
    if "trace_id" in params and any(value.get(key) != params[key] for key in ("session_id", "trace_id")):
        raise ProtocolError(ErrorCode.TRACE_MISMATCH)


def decode_result(method: str, result: object, params: Params) -> Result:
    """Caller matches the response id first; only turn responses echo correlation."""
    try:
        shape = Turn if method == "session/close" and "trace_id" in params else RESULTS[method]
        decoded = _decode(shape, result)
        _verify_correlation(decoded, params)
        return cast(Result, decoded)
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError(ErrorCode.INVALID_REQUEST) from None


def verify_error(error: RpcError, params: Params) -> None:
    _verify_correlation(error.get("data", {}), params)


def select_version(offered: list[str]) -> str:
    if PROTOCOL_VERSION not in offered:
        raise ProtocolError(ErrorCode.VERSION_MISMATCH)
    return PROTOCOL_VERSION


def validate_initialize(result: InitializeResult, offered: list[str], kind: str,
                        permitted: tuple[RequestKind, ...] = get_args(RequestKind)) -> InitializeResult:
    if result["protocol_version"] != select_version(offered) or result["runner"]["kind"] != kind:
        raise ProtocolError(ErrorCode.VERSION_MISMATCH)
    caps = result.get("capabilities", {})  # Absent capabilities are false.
    if not caps.get("cancel", False) or not caps.get("usage", {}).get("per_request", False) or not set(
            permitted) <= set(caps.get("authorize_covers", [])):
        raise ProtocolError(ErrorCode.UNSUPPORTED)
    return result


def decode_message(value: object) -> Message:
    try:
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ValueError
        if "method" in value:
            method = _decode(Name, value["method"])
            if "params" not in value or "result" in value or "error" in value:
                raise ValueError
            if (method == "session/update") != ("id" not in value) and method in PARAMS:
                raise ValueError
            rpc_id = _decode(RpcId, value["id"]) if "id" in value else None
            if rpc_id and method in PARAMS and not rpc_id.startswith(
                    "r-" if method in {"tools/call", "budget/authorize"} else "l-"):
                raise ValueError
            params = decode_params(method, value["params"])
            return Request(rpc_id, method, params) if rpc_id else Notification(method, params)
        if "params" in value or "id" not in value or ("result" in value) == ("error" in value):
            raise ValueError
        rpc_id = _decode(RpcId | None, value["id"])
        if "error" in value:
            return Response(rpc_id, error=_decode(RpcError, value["error"]))
        if rpc_id is None or not isinstance(value["result"], dict):
            raise ValueError
        return Response(rpc_id, result=Payload(value["result"]))
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError(ErrorCode.INVALID_REQUEST) from None


def encode_frame(message: Message | dict) -> bytes:
    if isinstance(message, (Request, Notification)):
        value = {"jsonrpc": "2.0", "method": message.method, "params": message.params}
        if isinstance(message, Request):
            value["id"] = message.id
    elif isinstance(message, Response):
        if message.result is not None and message.error is not None:
            raise ProtocolError(ErrorCode.INVALID_REQUEST)
        value = {"jsonrpc": "2.0", "id": message.id}
        value.update({"result": message.result} if message.error is None else {"error": message.error})
    else:
        value = message
    try:
        frame = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError(ErrorCode.INVALID_REQUEST) from None
    decode_message(value)
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError(ErrorCode.INVALID_REQUEST)
    return frame + b"\n"


async def read_frame(reader: asyncio.StreamReader) -> Message:
    """Only LF delimits frames, including with the default StreamReader limit.

    EOF between frames raises EOFError. Owners close on any ProtocolError;
    never skip a corrupt stdout line and resume the connection.
    """
    chunks, size = [], 0
    while True:
        # StreamReader has no per-read limit argument. Temporarily narrow its
        # limit so readuntil rejects the first byte beyond the remaining budget.
        limit = reader._limit
        reader._limit = min(limit, MAX_FRAME_BYTES - size)
        try:
            try:
                chunk = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as error:
                chunk = await reader.readexactly(min(error.consumed, MAX_FRAME_BYTES - size + 1))
            except asyncio.IncompleteReadError as error:
                if not size and not error.partial:
                    raise EOFError from None
                raise ProtocolError(ErrorCode.INVALID_REQUEST) from None
        finally:
            reader._limit = limit
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FRAME_BYTES + (1 if chunk.endswith(b"\n") else 0):
            raise ProtocolError(ErrorCode.INVALID_REQUEST)
        if chunk.endswith(b"\n"):
            break
    try:
        value = json.loads(b"".join(chunks)[:-1].decode("utf-8"),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError(ErrorCode.PARSE_ERROR) from None
    return decode_message(value)
