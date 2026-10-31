"""Operator-only restart admission controls in the system domain."""

from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..drain import DrainScope

_RUN = _object({"worker_id": _STR, "task_id": _STR, "role": _STR, "since": _STR, "keepable": _BOOL}, closed=True)
DRAIN = _object({"phase": {"type": "string", "enum": ["serving", "draining", "drained"]},
                 "scope": {"type": ["string", "null"], "enum": ["turns", "runs", None]},
                 "since": _NULL_STR, "deadline": _NULL_STR, "deadline_passed": _BOOL,
                 "waiting": _object({"runs": _array(_RUN), "manager_turns": _array(_STR),
                                     "planner_turns": _array(_STR), "gates": _INT, "waits": _INT,
                                     "consults": _INT, "starting": _INT}, closed=True)}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("system.drain", "POST", "/api/v1/system/drain", "Drain before a planned restart.",
              "Drain before a planned restart. Hold new turns and runs until restart or cancellation; deadlines do not abort work.",
              _object({"scope": {"type": "string", "enum": ["turns", "runs"]},
                       "timeout_seconds": {"type": "number", "exclusiveMinimum": 0}},
                      ["scope"], closed=True), DRAIN, risk="write", operator_only=True,
              audit_operator=False),
    Operation("system.drain_status", "POST", "/api/v1/system/drain_status", "Read drain status and blockers.",
              "Read drain status and blockers.", _object({}, closed=True), DRAIN, risk="read", operator_only=True),
    Operation("system.drain_cancel", "POST", "/api/v1/system/drain_cancel", "Cancel drain and resume admission.",
              "Cancel drain and resume admission. Resume held messages and runs.",
              _object({}, closed=True), DRAIN, risk="write",
              operator_only=True, audit_operator=False),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "system.drain":
        return server._drain.begin(DrainScope(arguments["scope"]), arguments.get("timeout_seconds"))
    if operation.operation_id == "system.drain_cancel":
        return server._drain.cancel()
    return server._drain.snapshot()
