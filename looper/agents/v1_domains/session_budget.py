"""The Manager's session budget setting (``agents.session_budget``): operator-only, and never a Manager tool."""

from .. import session_budget
from ..api_v1 import Operation, _NULL_STR, _object
from ..ops import ErrorCode, OpError

_NULL_NUMBER = {"type": ["number", "null"]}
VIEW = _object({"turn_budget_usd": _NULL_NUMBER, "recommended_usd": {"type": "number"}, "problem": _NULL_STR},
               closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("session_budget.get", "POST", "/api/v1/session_budget/get",
              "Read the Manager's per-turn budget.",
              "Read the Manager's per-turn budget. turn_budget_usd is null while it is off (the default); "
              "recommended_usd is the value to turn it on with; problem says why a stored value is not used.",
              _object({}, closed=True), VIEW, operator_only=True),
    Operation("session_budget.update", "POST", "/api/v1/session_budget/update",
              "Set the Manager's per-turn budget.",
              "Set the Manager's per-turn budget. turn_budget_usd is a positive number of US dollars; null turns "
              "it off. A field left out keeps its value. Manager wiring is separate: turns are unchanged.",
              _object({"turn_budget_usd": _NULL_NUMBER}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "session_budget.update":
        return None
    try:
        values = session_budget.checked(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    value = values.get("turn_budget_usd")
    return (f"Set {session_budget.TURN_BUDGET_KEY} to " + ("unset (off)" if value is None else f"${value:.2f}")
            + "? Manager wiring is separate; turns are unchanged.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "session_budget.get":
        return await session_budget.view()
    try:
        return await session_budget.update(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
