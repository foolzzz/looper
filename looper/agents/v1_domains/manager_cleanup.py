"""Manual operator-only cleanup of deleted Manager history; identity tombstones remain reserved."""

from ...db import manager_actions
from .. import manager_cleanup
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import API_CHANNEL
from ..ops.clients import DEFAULT_SESSION


REQUEST = _object({
    "manager_id": {"type": "string", "minLength": 1},
    "retention_days": {"type": "integer", "minimum": 30, "maximum": 3650, "default": 30},
}, ["manager_id"], closed=True)
COUNTS = _object({
    "threads": {**_INT, "description": "Retained ownership tombstones; these threads are never deleted."},
    "chat_messages": _INT, "llm_records": _INT, "llm_record_events": _INT, "manager_actions": _INT,
}, closed=True)
RESULT = _object({
    "manager_id": _STR, "deleted_at": _NULL_STR, "retention_days": _INT,
    "eligible": _BOOL, "blockers": _array(_STR), "counts": COUNTS, "purged": _BOOL,
}, closed=True)
OPERATIONS = tuple(
    Operation(f"manager_cleanup.{action}", "POST", f"/api/v1/manager_cleanup/{action}", summary, summary,
              REQUEST, RESULT, risk=risk, confirms=risk == "high", operator_only=True)
    for action, risk, summary in (
        ("preview", "read", "Preview retention eligibility and exact-owner history counts; thread identities remain."),
        ("purge", "high", "Permanently remove an eligible deleted Manager's history, retaining identity tombstones."),
    )
)
RESULTS = {}


async def impact(server, operation, arguments, principal, headers):
    result = await manager_cleanup.preview(**arguments, fleet=server._managers)
    counts = result["counts"]
    return (f"Permanently purge history for {result['manager_id']} after {result['retention_days']} days retention? "
            f"Remove {counts['chat_messages']} chat message bodies, {counts['llm_records']} run records, "
            f"{counts['llm_record_events']} run events and {counts['manager_actions']} actions. "
            f"Retain {counts['threads']} thread ownership tombstones and the Manager identity; its key stays reserved. "
            "This loss of bodies, run and action history is irreversible. Eligibility is checked again on execution. "
            + ("Currently eligible." if result["eligible"] else "Blocked: " + "; ".join(result["blockers"])))


async def run(server, operation, arguments, principal, headers):
    callback = manager_cleanup.preview if operation.operation_id == "manager_cleanup.preview" else manager_cleanup.purge
    result = await callback(**arguments, fleet=server._managers)
    if operation.operation_id == "manager_cleanup.preview":
        # Read domains bypass ClientOps. Audit only metadata outside the target; the DB preview stays read-only.
        await manager_actions.record(channel=API_CHANNEL, chat=headers.get("x-looper-session") or DEFAULT_SESSION,
            message_id=None, tool=operation.operation_id, risk="read", status="ok",
            arguments={"manager_id": result["manager_id"], "retention_days": result["retention_days"]})
    return result
