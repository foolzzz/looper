"""Read run records without rewriting historical metadata or transcript events.

Database failures return unavailable, including task/list reads that used to hide failures as empty lists.
"""

from datetime import datetime, timedelta, timezone

from looper.db import llm_records
from .. import failures
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_NULL_INT = {"type": ["integer", "null"]}
_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_OBJECT = {"type": ["object", "null"]}
_COMPACTIONS = {"type": ["array", "null"], "items": {"type": "object"}}
_STATUS = {"type": "string", "enum": [status.value for status in llm_records.RecordStatus]}
TASK_RUN_ROW = _object({
    "id": _INT, "role": {"type": "string", "enum": ["planner", "worker", "reviewer", "explorer"]},
    "agent_id": _STR, "step_id": _NULL_STR, "status": _STATUS, "created_at": _STR,
    "runtime": _object({"type": _NULL_STR, "name": _NULL_STR}, closed=True),
    "model": _NULL_STR, "effort": _NULL_STR,
    "profile": _object({"name": _NULL_STR, "source": _NULL_STR}, closed=True),
    "task_role": _NULL_OBJECT, "kind": _NULL_STR, "parent_record_id": {"type": ["integer", "null"]}}, closed=True)
_RECORD_FIELDS = {
    "id": _INT, "agent_id": _STR, "status": _STATUS,
    **dict.fromkeys(("task_id", "session_id", "input_summary", "output_summary", "input_text", "output_text",
                     "model", "model_requested", "model_reported", "error", "finished_at", "last_output_at"), _NULL_STR),
    **dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                     "context_tokens", "duration_ms"), _NULL_INT),
    "cost_usd": _NULL_NUMBER, "cost_source": {"type": ["string", "null"], "enum": ["runtime", "provider_price", None]},
    "compactions": _COMPACTIONS, "metadata": _NULL_OBJECT, "created_at": _STR,
    "task_role": {"type": "object"},
    # Why a failed run failed (``failures``), while engine.failure_typing is on; null otherwise.
    "failure_class": {"type": ["string", "null"], "enum": [*(cls.value for cls in failures.FailureClass), None]},
    "failure_cause": {"type": ["string", "null"], "enum": [*(cause.value for cause in failures.FailureCause), None]}}
_OPTIONAL = ("cost_source", "task_role", "failure_class", "failure_cause")
LLM_RECORD = _object(_RECORD_FIELDS, [key for key in _RECORD_FIELDS if key not in _OPTIONAL], closed=True)
_SUMMARY_FIELDS = {key: value for key, value in _RECORD_FIELDS.items() if key not in ("input_text", "output_text")}
_SUMMARY_FIELDS.update(input_chars=_INT, output_chars=_INT)
LLM_RECORD_SUMMARY = _object(_SUMMARY_FIELDS, [key for key in _SUMMARY_FIELDS if key not in _OPTIONAL], closed=True)
LLM_RECORD_EVENT = _object({"seq": _INT, "event": {"type": "object"}, "created_at": _STR}, closed=True)
AGENT_TURN = _object({"id": _INT, "created_at": _STR, "status": _STATUS, "session_id": _NULL_STR,
                      "cost_usd": _NULL_NUMBER, "context_tokens": _NULL_INT, "compactions": _COMPACTIONS}, closed=True)
AGENT_USAGE = _object({
    "agent_id": _STR,
    "total": _object({"cost_usd": {"type": "number"}, **dict.fromkeys(
        ("runs", "running", "unpriced_runs", "compactions"), _INT)}, closed=True),
    "by_session": _array(_object({"session_id": _NULL_STR, "cost_usd": {"type": "number"}, "runs": _INT,
                                   "first_at": _STR, "last_at": _STR}, closed=True)),
    "by_day": _array(_object({"day": _STR, "cost_usd": {"type": "number"}, "runs": _INT}, closed=True)),
    "turns": _array(AGENT_TURN), "latest_context_tokens": _NULL_INT}, closed=True)
_CLASS = {"type": "string", "enum": [cls.value for cls in failures.FailureClass]}
FAILURE_STATS = _object({
    "failure_typing": {"type": "string", "enum": [mode.value for mode in failures.FailureTyping]},
    "since": _STR, "runs": _INT,
    "by_class": _object({cls.value: _INT for cls in failures.FailureClass}, closed=True),
    "steps": _object({"failed": _INT, "not_judgment": _INT}, closed=True),
    "rows": _array(_object({"day": _STR, "run_kind": _NULL_STR, "runtime": _NULL_STR, "failure_class": _CLASS,
                            "failure_cause": {"type": "string", "enum": [cause.value for cause in failures.FailureCause]},
                            "runs": _INT}, closed=True))}, closed=True)
SCHEMAS = {"TaskRunRow": TASK_RUN_ROW, "LlmRecord": LLM_RECORD, "LlmRecordSummary": LLM_RECORD_SUMMARY,
           "RunFailureStats": FAILURE_STATS,
           "LlmRecordEvent": LLM_RECORD_EVENT,
           "AgentTurn": AGENT_TURN, "AgentUsage": AGENT_USAGE}
RESULTS = {}


def _op(action, fields, result, required=None):
    description = f"Read run records: {action.replace('_', ' ')}. Database failures return unavailable."
    return Operation(f"runs.{action}", "POST", f"/api/v1/runs/{action}", description, description,
                     _object(fields, required, closed=True), result, risk="read", operator_only=True,
                     level=ActionLevel.INSIDE)  # only reads run records and failure statistics stored by Looper


_ID = {"record_id": {"type": "integer", "minimum": 1}}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}
OPERATIONS = (
    _op("for_task", {"task_id": _STR}, _object({"runs": _array(TASK_RUN_ROW)})),
    _op("list", {"agent_id": _STR, "task_id": _STR, "role": _STR, "limit": _LIMIT,
                 "offset": {"type": "integer", "minimum": 0}}, _object({"records": _array(LLM_RECORD_SUMMARY)}), []),
    _op("get", _ID, _object({"record": LLM_RECORD})),
    _op("events", {**_ID, "after": {"type": "integer", "minimum": 0}, "limit": {**_LIMIT, "maximum": 5000}},
        _object({"events": _array(LLM_RECORD_EVENT)}), ["record_id"]),
    _op("usage", {"agent_id": _STR, "turns": {**_LIMIT, "maximum": 1000}}, AGENT_USAGE, ["agent_id"]),
    _op("failures", {"days": {"type": "integer", "minimum": 1, "maximum": 90}}, FAILURE_STATS, []),
)


def _model_fields(record):
    metadata = record.get("metadata") or {}
    profile = metadata.get("profile") or metadata.get("agent") or {}
    runtime = (profile.get("runtime") or {}).get("type")
    reported = (metadata["routed_model"] if runtime == "cursor-agent" and metadata.get("routed_model") else
                metadata["model_reported"] if "model_reported" in metadata else
                metadata.get("model_display") if runtime == "cursor-agent" else
                None if runtime in ("coco", "codex", "grok", "pi", "omp", "dsh", "api") else record.get("model"))
    return {**record, "model_requested": (metadata.get("model_choice") or {}).get("model") or profile.get("model"),
            "model_reported": reported}


async def run(server, operation, arguments, principal, headers):
    # Retain the shared historical role projection, including the absence of a built-in task_role.
    from ..http_server import _with_run_role

    action = operation.operation_id.removeprefix("runs.")
    try:
        if action == "for_task":
            result = {"runs": await llm_records.task_run_rows(arguments["task_id"])}
        elif action == "list":
            result = {"records": [_model_fields(_with_run_role(row)) for row in await llm_records.list_records(**arguments)]}
        elif action == "get":
            row = await llm_records.get_record(arguments["record_id"])
            if row is None:
                raise OpError(ErrorCode.NOT_FOUND, "llm record not found")
            result = {"record": _model_fields(_with_run_role(row))}
        elif action == "failures":
            since = datetime.now(timezone.utc) - timedelta(days=arguments.get("days", 7))
            result = {"failure_typing": (await failures.typing_mode()).value, "since": since.isoformat(),
                      **await llm_records.failure_stats(since)}
        elif action == "events":
            result = {"events": await llm_records.list_events(arguments["record_id"],
                after_seq=arguments.get("after", 0), limit=arguments.get("limit", 1000))}
        else:
            result = await llm_records.agent_usage(**arguments)
    except OpError:
        raise
    except Exception:
        raise OpError(ErrorCode.UNAVAILABLE, "run records database unavailable") from None
    return _plain(result)
