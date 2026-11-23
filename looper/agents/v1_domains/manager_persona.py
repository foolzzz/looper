"""Operator read and update of the default Manager's persona. The Manager has no write tool for it."""

from .. import persona
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _STR, _object
from ..ops import ErrorCode, OpError

_RESULT = _object({name: _STR for name in persona.FIELDS})


def _nullable(limit: int) -> dict:
    return {"type": ["string", "null"], "maxLength": limit}


def _op(name: str, summary: str, description: str, request: dict, risk: str) -> Operation:
    # inside: the call only reads or stores persona text in Looper; it sends nothing.
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, description,
                     request, _RESULT, risk=risk, confirms=False, operator_only=True,
                     level=ActionLevel.INSIDE)


OPERATIONS = (
    _op("manager_persona.get", "Read the default Manager's persona.",
        "Read the default Manager's persona. Unset fields are empty strings.",
        _object({}, closed=True), "read"),
    _op("manager_persona.update", "Change the default Manager's persona.",
        "Change the default Manager's persona. A field left out keeps its value; null clears it. "
        "Name and address are at most 20 characters, tone 200, notes 400, and the four together at most 600.",
        _object({name: _nullable(persona.LIMITS[name]) for name in persona.FIELDS}, [], closed=True), "write"),
)
RESULTS = {}


async def run(server, operation, arguments, principal, headers):
    try:
        if operation.operation_id == "manager_persona.get":
            await persona.load()
            return persona.view()
        return await persona.update(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
