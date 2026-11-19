"""The message stream's operator operations: its sources' status and runs, and the switch (message-stream RFC).

Status and runs return counts, states and times only, never a message's text. ``message_stream.configure`` is the
dashboard's switch: it creates the source, points it at an outside collector's directory and turns bridging on or off.
"""

from pathlib import Path

from ...db import message_stream as store
from .. import cron, source_service
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..ops import ErrorCode, OpError

DEFAULT_SOURCE = "lark-user"
RUNS_DEFAULT_LIMIT, RUNS_MAX_LIMIT = 20, 100
_MODES = {"type": "string", "enum": ["off", "bridge"]}
_RUN = _object({
    "run_id": _INT, "mode": _STR, "trigger": _STR, "status": _STR, "started_at": _STR, "finished_at": _NULL_STR,
    "events_new": _INT, "events_seen": _INT, "events_skipped": _INT, "files_rescanned": {"type": ["integer", "null"]},
    "error": _NULL_STR}, closed=True)
_SOURCE = _object({
    "source_id": _STR, "name": _STR, "kind": _STR, "ingest_mode": {"type": "string", "enum": ["off", "bridge", "collect"]},
    "bridge_dir": _STR, "start_from": {"type": "string", "enum": ["end", "beginning"]}, "cron": _STR,
    "timezone": _STR, "user_open_id": _NULL_STR,
    "health": {"type": "string", "enum": ["off", "healthy", "degraded", "stale"]},
    "last_run_at": _NULL_STR, "last_ok_at": _NULL_STR, "committed_seq": _INT,
    "events": _INT, "events_last_hour": _INT, "newest_sent_at": _NULL_STR, "body_bytes": _INT,
    "chats": _INT, "chats_excluded": _INT, "chats_failing": _INT,
    "last_run": {"anyOf": [_RUN, {"type": "null"}]}}, closed=True)
STATUS = _object({"sources": _array(_SOURCE)}, closed=True)
RUNS = _object({"source_id": _STR, "runs": _array(_RUN)}, closed=True)

RESULTS = {}
OPERATIONS = (
    Operation("message_stream.status", "POST", "/api/v1/message_stream/status",
              "Read the message stream's sources: mode, health, counts and the last run.",
              "Read the message stream's sources: mode, health, counts and the last run. Counts and states only; "
              "never message text.", _object({}, closed=True), STATUS, risk="read", operator_only=True),
    Operation("message_stream.runs", "POST", "/api/v1/message_stream/runs",
              "List a source's recent ingest runs, newest first.",
              "List a source's recent ingest runs, newest first. Each has its status, new, duplicate and skipped events, "
              "and the files read again.",
              _object({"source_id": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": RUNS_MAX_LIMIT}}, [],
                      closed=True), RUNS, risk="read", operator_only=True),
    Operation("message_stream.configure", "POST", "/api/v1/message_stream/configure",
              "Create the message source, point it at an outside collector's directory, or turn bridging on or off.",
              "Create the message source, point it at an outside collector's directory, or turn bridging on or off. "
              "A bridged source reads the collector's per-chat files into Looper's event store on its time rule, from "
              "the files' current end (start_from end, the default) or from their beginning; off stops reading and "
              "keeps what was stored.",
              _object({"source_id": _STR, "name": _STR, "ingest_mode": _MODES, "bridge_dir": _STR,
                       "start_from": {"type": "string", "enum": ["end", "beginning"]}, "cron": _STR,
                       "timezone": _STR, "user_open_id": _NULL_STR}, [], closed=True),
              _SOURCE, risk="write", operator_only=True, audit_operator=False),
)


def _run_view(row):
    return {"run_id": row["id"], **{key: row[key] for key in (
        "mode", "trigger", "status", "started_at", "finished_at", "events_new", "events_seen", "events_skipped",
        "files_rescanned", "error")}}


async def _source_view(source):
    stats = await store.status_row(source["id"])
    runs = await store.list_runs(source["id"], limit=1)
    last = runs[0] if runs else None
    return {
        "source_id": source["id"], "name": source["name"], "kind": source["kind"],
        "ingest_mode": source["ingest_mode"], "bridge_dir": source["bridge_dir"],
        "start_from": source["bridge_start_from"], "cron": source["cron"],
        "timezone": source["timezone"], "user_open_id": source["user_open_id"],
        "health": source_service.health(source, last["status"] if last else None, stats["now"]),
        "last_run_at": source["last_run_at"], "last_ok_at": source["last_ok_at"],
        "committed_seq": source["committed_seq"], "events": stats["events"],
        "events_last_hour": stats["events_last_hour"], "newest_sent_at": stats["newest_sent_at"],
        "body_bytes": stats["body_bytes"], "chats": stats["chats"], "chats_excluded": stats["chats_excluded"],
        "chats_failing": stats["chats_failing"], "last_run": _run_view(last) if last else None}


def _check_configuration(arguments, existing):
    """The fields to store, or an OpError that says what to fix."""
    fields = {key: arguments[key] for key in ("name", "ingest_mode", "bridge_dir", "cron", "timezone", "user_open_id")
              if key in arguments}
    if "start_from" in arguments:
        fields["bridge_start_from"] = arguments["start_from"]
    if "name" in fields and not fields["name"].strip():
        raise OpError(ErrorCode.INVALID, "name must not be empty")
    for key in ("cron", "timezone"):
        try:
            if key == "cron" and key in fields:
                cron.parse(fields[key])
            if key == "timezone" and key in fields:
                cron.zone(fields[key])
        except ValueError as error:
            raise OpError(ErrorCode.INVALID, str(error)) from None
    open_id = fields.get("user_open_id")
    if open_id is not None and (not open_id.strip() or len(open_id) > 100 or any(c.isspace() for c in open_id)):
        raise OpError(ErrorCode.INVALID, "user_open_id is the Lark open id of the user the messages are read as")
    mode = fields.get("ingest_mode", existing["ingest_mode"] if existing else "off")
    directory = fields.get("bridge_dir", existing["bridge_dir"] if existing else "")
    if directory:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise OpError(ErrorCode.INVALID, "bridge_dir must be an absolute path")
        fields["bridge_dir"] = str(path)
    if mode == "bridge" and not (directory and (Path(fields["bridge_dir"]) / "chats").is_dir()):
        raise OpError(ErrorCode.INVALID,
                      "bridge needs a bridge_dir that is an absolute path to a directory holding a chats/ directory "
                      "of per-chat .ndjson files")
    if existing is None and mode == "bridge" and "cron" not in fields:
        fields["cron"] = source_service.BRIDGE_CRON
    return fields


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "message_stream.status":
        return {"sources": [await _source_view(source) for source in await store.list_sources()]}
    sources = await store.list_sources()
    if operation.operation_id == "message_stream.runs":
        source_id = arguments.get("source_id") or (sources[0]["id"] if len(sources) == 1 else None)
        if source_id is None or not any(source["id"] == source_id for source in sources):
            raise OpError(ErrorCode.NOT_FOUND, "no such message source")
        limit = arguments.get("limit", RUNS_DEFAULT_LIMIT)
        return {"source_id": source_id,
                "runs": [_run_view(row) for row in await store.list_runs(source_id, limit=limit)]}
    source_id = arguments.get("source_id") or (sources[0]["id"] if len(sources) == 1 else DEFAULT_SOURCE)
    existing = next((source for source in sources if source["id"] == source_id), None)
    if existing is None and "source_id" in arguments and arguments["source_id"] != DEFAULT_SOURCE and sources:
        raise OpError(ErrorCode.NOT_FOUND, "no such message source")
    fields = _check_configuration(arguments, existing)
    fields.setdefault("name", existing["name"] if existing else "Lark messages")
    return await _source_view(await store.save_source(source_id, fields))
