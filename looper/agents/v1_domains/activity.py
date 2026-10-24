"""Task activity reads and a durable, cursor-based activity stream."""

import asyncio
import json

from looper.db import activity, tasks
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _summary
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

ORDER = 8

_SEQ = {"type": "integer", "minimum": 1}
_CURSOR = {"type": "integer", "minimum": 0}
_NULL_INT = {"type": ["integer", "null"]}
_CONVERSATION = _object({"seq": _SEQ, "worker": _NULL_STR, "kind": _STR,
                         "summary": _NULL_STR, "created_at": _STR}, closed=True)
_ACTIVITY = _object({**_CONVERSATION["properties"], "task_id": _STR, "detail": _NULL_STR,
                     "metadata": {"type": ["object", "null"]},
                     "input_tokens": _NULL_INT, "output_tokens": _NULL_INT},
                    [*_CONVERSATION["properties"], "task_id", "metadata", "input_tokens", "output_tokens"], closed=True)
SCHEMAS = {"ActivityEvent": _ACTIVITY, "ConversationEvent": _CONVERSATION,
           "ActivityDone": _object({}, closed=True)}
_EVENT = {"$ref": "#/components/schemas/ActivityEvent"}
_WINDOW = {"has_more": _BOOL, "oldest_seq": _NULL_INT, "latest_seq": _NULL_INT}
RESULTS = {}


def _op(action, properties, result, description, **kwargs):
    return Operation("tasks." + action, "POST", "/api/v1/tasks/" + action, _summary(description), description,
                     _object({"task_id": _STR, **properties}, ["task_id", *(["seq"] if action == "activity_detail" else [])],
                             closed=True), result, risk="read", **kwargs)


OPERATIONS = (
    _op("activity", {"after": _CURSOR, "since": _CURSOR, "before": _SEQ, "limit": _INT},
        _object({"activity": _array(_EVENT), **_WINDOW}, ["activity"]),
        "Read ordered task activity. after takes precedence over since; either selects forward reads and ignores "
        "before (default limit 100, min(requested, 500)). Otherwise read a backward window, oldest first, "
        "with limit clamped to 1..1000 (default 1000). Database failures return unavailable."),
    _op("activity_detail", {"seq": _SEQ}, _EVENT, "Read one activity event belonging to this task, including detail."),
    _op("conversation", {"before": _SEQ, "limit": _INT},
        _object({"rows": _array({"$ref": "#/components/schemas/ConversationEvent"}), **_WINDOW}),
        "Read a conversation window, oldest first. Only text, phase, error and user_message kinds; "
        "limit clamps to 1..1000 (default 1000). Database failures return unavailable."),
    _op("activity_stream", {"since": _CURSOR}, _EVENT,
        "Follow task activity after since (default 0), replay then live, until done or disconnected.", stream=True,
        stream_description="activity carries ActivityEvent with SSE id equal to seq; done carries {}; "
                           "idle streams send heartbeat comments. Failures before headers use OpError.",
        stream_events={"type": "string", "x-looper-events": {
            "activity": {"data": _EVENT, "id": {"type": "integer", "description": "ActivityEvent.seq"}},
            "done": {"data": {"$ref": "#/components/schemas/ActivityDone"}},
            "heartbeat": {"comment": "ping"}}}),
)


def _event(row):
    row = _plain(row)
    if isinstance(row.get("metadata"), str):
        row["metadata"] = json.loads(row["metadata"])
    return row


async def _task(task_id):
    task = await tasks.get_task(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, "task not found")
    return task


async def run(server, operation, arguments, principal, headers):
    task_id = arguments["task_id"]
    await _task(task_id)
    name = operation.operation_id
    if name == "tasks.activity_detail":
        row = await activity.get_activity_detail(arguments["seq"])
        if row is None or row["task_id"] != task_id:
            raise OpError(ErrorCode.NOT_FOUND, "activity not found")
        return _event(row)
    if name == "tasks.conversation":
        return _plain(await activity.conversation_window(
            task_id, before=arguments.get("before"), limit=arguments.get("limit", activity.WINDOW_LIMIT)))
    forward = arguments.get("after", arguments.get("since"))
    if forward is not None:
        return {"activity": [_event(row) for row in await activity.list_activity(
            task_id, since_seq=forward, limit=min(arguments.get("limit", 100), 500))]}
    window = await activity.activity_window(
        task_id, before=arguments.get("before"), limit=arguments.get("limit", activity.WINDOW_LIMIT))
    return {"activity": [_event(row) for row in window.pop("rows")], **window}


async def stream(server, operation, arguments, principal, headers, writer):
    task_id, cursor = arguments["task_id"], arguments.get("since", 0)
    await _task(task_id)
    sub = server._broadcaster.subscribe(task_id)
    started = False
    try:
        # Subscribe before reading: notifications during replay remain queued. Use
        # the database for live payloads too: bus notifications can be thin rows.
        rows = await activity.list_activity(task_id, since_seq=cursor, limit=500)
        terminal = (await _task(task_id))["status"] in {"completed", "failed", "stopped", "cancelled"}
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\n")
        started = True
        while True:
            while rows:
                for row in rows:
                    cursor = row["seq"]
                    writer.write(f"id: {cursor}\nevent: activity\ndata: {json.dumps(_event(row))}\n\n".encode())
                    await writer.drain()
                rows = await activity.list_activity(task_id, since_seq=cursor, limit=500)
            if terminal:
                writer.write(b"event: done\ndata: {}\n\n")
                await writer.drain()
                return
            try:
                event = await asyncio.wait_for(sub.get(), timeout=20)
                terminal = event is None
            except asyncio.TimeoutError:
                writer.write(b": ping\n\n")
                await writer.drain()
            rows = await activity.list_activity(task_id, since_seq=cursor, limit=500)
            terminal = terminal or (await _task(task_id))["status"] in {"completed", "failed", "stopped", "cancelled"}
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception:
        if not started:
            raise OpError(ErrorCode.UNAVAILABLE, "activity is unavailable") from None
        # After headers, close the stream; reconnect replays from its last seq.
    finally:
        server._broadcaster.unsubscribe(task_id, sub)
