"""The message stream's operator operations: its sources' status and runs, and the switch (message-stream RFC).

Status and runs return counts, states and times only, never a message's text. ``message_stream.configure`` is the
dashboard's switch: it creates the source, points it at an outside collector's directory and turns bridging on or off.
"""

import asyncio
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from ...db import message_stream as store
from .. import cron, source_service
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError

DEFAULT_SOURCE = "lark-user"
RUNS_DEFAULT_LIMIT, RUNS_MAX_LIMIT = 20, 100
_MODES = {"type": "string", "enum": ["off", "bridge"]}
_RUN = _object({
    "run_id": _INT, "mode": _STR, "trigger": _STR, "status": _STR, "started_at": _STR, "finished_at": _NULL_STR,
    "events_new": _INT, "events_seen": _INT, "events_skipped": _INT, "skipped_by_deletion": _INT, "files_rescanned": {"type": ["integer", "null"]},
    "error": _NULL_STR}, closed=True)
_SOURCE = _object({
    "source_id": _STR, "name": _STR, "kind": _STR, "ingest_mode": {"type": "string", "enum": ["off", "bridge", "collect"]},
    "bridge_dir": _STR, "start_from": {"type": "string", "enum": ["end", "beginning"]}, "cron": _STR,
    "timezone": _STR, "user_open_id": _NULL_STR,
    "health": {"type": "string", "enum": ["off", "healthy", "degraded", "stale"]},
    "last_run_at": _NULL_STR, "last_ok_at": _NULL_STR, "committed_seq": _INT,
    "events": _INT, "events_last_hour": _INT, "newest_sent_at": _NULL_STR, "body_bytes": _INT,
    "chats": _INT, "chats_excluded": _INT, "chats_failing": _INT,
    "body_retention_days": {"type": ["integer", "null"]},
    "app_body_retention_days": {"type": ["integer", "null"]}, "retention_last_at": _NULL_STR,
    "events_last_day": _INT, "body_bytes_last_day": _INT, "bodies_dropped": _INT,
    "last_run": {"anyOf": [_RUN, {"type": "null"}]}}, closed=True)
STATUS = _object({"sources": _array(_SOURCE)}, closed=True)
RUNS = _object({"source_id": _STR, "runs": _array(_RUN)}, closed=True)

_CHAT = _object({"chat_id": _STR, "kind": _STR, "name": _STR, "inclusion": _STR, "state": _STR,
                 "events_7d": _INT, "purged_at": _NULL_STR}, closed=True)
_TARGET = {"source_id": _STR, "scope": {"type": "string", "enum": ["message", "range", "chat"]},
           "message_id": _STR, "chat_id": _STR, "start": _STR, "end": _STR}
_BACKUPS = _object({"count": _INT, "earliest_at": _NULL_STR}, closed=True)
_PREVIEW = _object({"source_id": _STR, "scope": _STR, "events": _INT, "confirmation_token": _STR,
                    "backups": _BACKUPS, "limits": _array(_STR)}, closed=True)
LIMITS = (
    "Outputs already pushed to chats cannot be recalled by this deletion.",
    "Processor run outputs already stored are not deleted and may summarize these messages.",
    "Schema-change backups exclude source_events rows; older upgrade backups retain their contents until rotated out.",
    "When enabled, daily message backups keep the newest 7 copies; older copies age out through rotation. "
    "When disabled, no new message backup is created.",
    "The lark-chat archive and the upstream collector files are outside Looper and are not deleted.",
)
CONFIRMATION_SECONDS = 600

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
OPERATIONS += (
    Operation("message_stream.chats", "POST", "/api/v1/message_stream/chats",
              "List chats and their recent event counts.",
              "List chats and their recent event counts. List chat names, inclusion, state and seven-day counts; never bodies.",
              _object({"source_id": _STR, "search": _STR}, [], closed=True),
              _object({"source_id": _STR, "chats": _array(_CHAT)}, closed=True), risk="read", operator_only=True),
    Operation("message_stream.chat_update", "POST", "/api/v1/message_stream/chat_update",
              "Include or exclude a chat from ingestion and all future subscription windows.",
              "Include or exclude a chat from ingestion and all future subscription windows. Stored messages remain.",
              _object({"source_id": _STR, "chat_id": _STR, "included": _BOOL}, ["chat_id", "included"], closed=True),
              _CHAT, risk="write", operator_only=True, audit_operator=False),
    Operation("message_stream.delete_preview", "POST", "/api/v1/message_stream/delete_preview",
              "Preview deletion and its limits, then request a short-lived confirmation token.",
              "Preview deletion and its limits, then request a short-lived confirmation token. "
              "Preview one message, a half-open sent-time range or a whole chat, without returning message bodies.",
              _object(_TARGET, ["scope"], closed=True), _PREVIEW, risk="read", operator_only=True),
    Operation("message_stream.delete", "POST", "/api/v1/message_stream/delete",
              "Confirm a previewed deletion and keep a tombstone against reimport.",
              "Confirm a previewed deletion and keep a tombstone against reimport. "
              "Permanently delete the previewed scope using its single-use confirmation token. Chat deletion also excludes it.",
              _object({**_TARGET, "confirmation_token": _STR}, ["scope", "confirmation_token"], closed=True),
              _object({"source_id": _STR, "deleted": _INT, "deletion_id": _INT}, closed=True),
              risk="high", operator_only=True, audit_operator=False),
)



def _run_view(row):
    return {"run_id": row["id"], **{key: row[key] for key in (
        "mode", "trigger", "status", "started_at", "finished_at", "events_new", "events_seen", "events_skipped", "skipped_by_deletion",
        "files_rescanned", "error")}}


async def _source_view(source):
    if source["ingest_mode"] == "collect":
        from ...db.message_collection import collector_source
        source = await collector_source(source["id"])
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
        "chats_failing": stats["chats_failing"],
        "body_retention_days": source["body_retention_days"],
        "app_body_retention_days": source["app_body_retention_days"], "retention_last_at": source["retention_last_at"],
        **{key: stats[key] for key in ("events_last_day", "body_bytes_last_day", "bodies_dropped")}, "last_run": _run_view(last) if last else None}


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


def deletion_target(arguments):
    """Reject mixed scopes and timezone-ambiguous ranges before previewing or changing anything."""
    scope = arguments["scope"]
    keys = {key for key in ("message_id", "chat_id", "start", "end") if key in arguments}
    required, allowed = {
        "message": ({"message_id"}, {"message_id"}), "chat": ({"chat_id"}, {"chat_id"}),
        "range": ({"start", "end"}, {"start", "end", "chat_id"}),
    }[scope]
    if not required <= keys or not keys <= allowed or any(not str(arguments[key]).strip() for key in keys):
        raise OpError(ErrorCode.INVALID, "provide only the fields for the selected deletion scope")
    result = {key: arguments[key] for key in keys}
    if scope == "range":
        try:
            for key in ("start", "end"):
                value = datetime.fromisoformat(result[key].replace("Z", "+00:00"))
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError()
                result[key] = value.astimezone(timezone.utc)
            if result["start"] >= result["end"]:
                raise ValueError()
        except ValueError:
            raise OpError(ErrorCode.INVALID, "range needs timezone-aware start and end, with start before end") from None
        if result["end"] > datetime.now(timezone.utc):
            raise OpError(ErrorCode.INVALID, "range end must not be in the future")
    return {"scope": scope, **result}


def backup_summary(runtime_root, dbname):
    """Only this database's completed upgrade archives; file names encode their UTC creation time."""
    from .. import db_backup

    if runtime_root is None:
        raise OpError(ErrorCode.UNAVAILABLE, "backup inventory is unavailable without a runtime root")
    directory = Path(runtime_root) / db_backup.BACKUP_DIRNAME
    stem = "looper-" + re.sub(r"[^A-Za-z0-9_.-]", "_", dbname)
    pattern = re.compile(re.escape(stem) + r"-(\d{8}T\d{6}\.\d{6}Z)\.dump")
    stamps = []
    try:
        for path in directory.iterdir():
            if path.is_file() and (match := pattern.fullmatch(path.name)):
                stamps.append(datetime.strptime(match[1], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc))
    except FileNotFoundError:
        return {"count": 0, "earliest_at": None}
    except (OSError, ValueError):
        raise OpError(ErrorCode.UNAVAILABLE, "cannot inventory upgrade backups; deletion is unavailable") from None
    return {"count": len(stamps), "earliest_at": min(stamps).isoformat() if stamps else None}


async def preview_deletion(source_id, target, *, runtime_root, confirmations, owner, db=None):
    from ...db.connection import get_pool

    database = db if db is not None else get_pool()
    dbname = await database.fetchval("SELECT current_database()")
    backups = await asyncio.to_thread(backup_summary, runtime_root, dbname)
    preview = await store.deletion_preview(source_id, **target, db=db)
    now = time.monotonic()
    for key in list(confirmations):
        if confirmations[key][0] <= now:
            del confirmations[key]
    if len(confirmations) >= 100:
        raise OpError(ErrorCode.RATE_LIMITED, "too many pending deletion previews; wait for one to expire")
    token = secrets.token_urlsafe(32)
    confirmations[token] = (now + CONFIRMATION_SECONDS, owner, source_id, target, backups)
    return {"source_id": source_id, "scope": target["scope"], "events": preview["events"],
            "confirmation_token": token, "backups": backups, "limits": list(LIMITS)}


async def confirm_deletion(source_id, target, token, *, runtime_root, confirmations, owner, db=None):
    from ...db.connection import get_pool

    pending = confirmations.get(token)
    if pending is None or pending[0] <= time.monotonic() or pending[1:4] != (owner, source_id, target):
        raise OpError(ErrorCode.CONFLICT, "deletion confirmation is missing, expired or for a different scope; preview again")
    database = db if db is not None else get_pool()
    dbname = await database.fetchval("SELECT current_database()")
    if await asyncio.to_thread(backup_summary, runtime_root, dbname) != pending[4]:
        del confirmations[token]
        raise OpError(ErrorCode.CONFLICT, "upgrade backups changed; preview deletion again")
    # Consume before the first awaited mutation, so concurrent requests cannot reuse a confirmation.
    if confirmations.pop(token, None) is not pending:
        raise OpError(ErrorCode.CONFLICT, "deletion confirmation has already been used; preview again")
    return {"source_id": source_id, **await store.delete_events(source_id, **target, db=db)}


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
    action = operation.operation_id.removeprefix("message_stream.")
    if action in ("chats", "chat_update", "delete_preview", "delete"):
        if "source_id" not in arguments and len(sources) > 1:
            raise OpError(ErrorCode.INVALID, "more than one message source exists; specify source_id")
        if existing is None:
            raise OpError(ErrorCode.NOT_FOUND, "no such message source")
        if action == "chats":
            return {"source_id": source_id, "chats": await store.list_chats(source_id, search=arguments.get("search", ""))}
        if action == "chat_update":
            row = await store.set_chat_inclusion(source_id, arguments["chat_id"], arguments["included"])
            if row is None:
                from ...db.connection import get_pool
                chat = await store.chat_row(get_pool(), source_id, arguments["chat_id"])
                if chat is not None and chat["purged_at"] is not None:
                    raise OpError(ErrorCode.CONFLICT, "a deleted chat cannot be included again; its tombstone is permanent")
                raise OpError(ErrorCode.NOT_FOUND, "no such source chat")
            return {key: row[key] for key in _CHAT["properties"]}
        target = deletion_target(arguments)
        if target["scope"] == "chat":
            from ...db.connection import get_pool
            if await store.chat_row(get_pool(), source_id, target["chat_id"]) is None:
                raise OpError(ErrorCode.NOT_FOUND, "no such source chat")
        if not hasattr(server, "_message_deletion_confirmations"):
            server._message_deletion_confirmations = {}
        kwargs = {"runtime_root": server._runtime_root, "confirmations": server._message_deletion_confirmations,
                  "owner": (principal.key, headers.get("x-looper-session", ""))}
        if action == "delete_preview":
            return await preview_deletion(source_id, target, **kwargs)
        return await confirm_deletion(source_id, target, arguments["confirmation_token"], **kwargs)
    fields = _check_configuration(arguments, existing)
    fields.setdefault("name", existing["name"] if existing else "Lark messages")
    from ...db.message_collection import CollectionConflict
    try:
        return await _source_view(await store.save_source(source_id, fields))
    except CollectionConflict as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None


async def impact(server, operation, arguments, principal, headers):
    # Dashboard deletion has its own preview token; retain a concrete impact for the API's high-risk registry.
    return "Permanently delete the previewed message-stream scope and retain a reimport tombstone. " + " ".join(LIMITS)
