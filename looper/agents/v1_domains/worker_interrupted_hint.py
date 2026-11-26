"""Operator-only control of the fixed Worker recovery instruction."""

from .. import worker_interruption
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
# These operations only read/store Looper's setting; they do not launch a run or deliver a message.
OPERATIONS = (
    Operation("worker_interrupted_hint.get", "POST", "/api/v1/worker_interrupted_hint/get",
              "Read the Worker interruption hint setting.",
              "Read the Worker interruption hint setting. It is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True, level=ActionLevel.INSIDE),
    Operation("worker_interrupted_hint.update", "POST", "/api/v1/worker_interrupted_hint/update",
              "Set the Worker interruption hint setting.",
              "Set the Worker interruption hint setting. When enabled, a re-dispatched interrupted attempt receives "
              "fixed recovery guidance. Null restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "worker_interrupted_hint.update":
        return (f"Set {worker_interruption.KEY} to {str(bool(arguments['enabled'])).lower()}? "
                "This changes the first task message only for re-dispatched interrupted Worker attempts.")
    return None


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "worker_interrupted_hint.get":
        return await worker_interruption.view()
    return await worker_interruption.update(arguments["enabled"])
