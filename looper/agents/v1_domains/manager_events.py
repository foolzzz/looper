"""The completion events' counts for Settings → Chat (manager completion ledger RFC, C1)."""

from ...db import manager_events as store
from ..api_v1 import Operation, _object, _BOOL, _INT

_COUNTS = {"type": "object", "additionalProperties": _INT}
SUMMARY = _object({"window_hours": _INT, "total": _INT, "by_state": _COUNTS, "by_reason": _COUNTS,
                   "turn_cost_usd": {"type": "number"}, "pending": _INT, "unpriced_runs": _INT,
                   "cost_status": {"type": "string", "enum": ["known", "unknown"]},
                   "oldest_pending_seconds": {"type": ["number", "null"]}, "failing": _BOOL}, closed=True)

RESULTS = {}
OPERATIONS = (
    Operation("manager_events.summary", "POST", "/api/v1/manager_events/summary",
              "Count the task-end completion events of the last 24 hours, by state and reason.",
              "Count the task-end completion events of the last 24 hours, by state and reason. With what their "
              "completion turns cost, the events waiting now and whether the last turns all fell back. Counts "
              "only; never a task's text.", _object({}, closed=True), SUMMARY, risk="read", operator_only=True),
)


async def run(server, operation, arguments, principal, headers):
    return await store.summary()
