"""Operator API and generated CLI for additive Manager capabilities."""

from .. import manager_installs
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL
from ..ops.config import answered
from ..ops.service import _plain

ORDER = 10
_ID = {"type": "string", "minLength": 1}
_IDS = {"manager_id": _ID, "item_id": _ID}
_RESULT = _object({"manager_id": _STR, "installs": _array(_object({
    "item_id": _STR, "kind": {"type": "string", "enum": ["plugin", "skill"]}, "name": _STR,
    "enabled": _BOOL, "inherited": _BOOL, "updated_at": _NULL_STR}))})


def _op(name, request, risk):
    description = f"{name.split('.')[-1].capitalize()} a Manager's additive plugin and skill installs."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, _RESULT, risk=risk, operator_only=True)


OPERATIONS = (
    _op("manager_installs.list", _object({"manager_id": _ID}, closed=True), "read"),
    _op("manager_installs.put", _object({**_IDS, "enabled": _BOOL}, list(_IDS), closed=True), "write"),
    _op("manager_installs.delete", _object(_IDS, closed=True), "write"),
)
RESULTS = {}


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.split(".")[-1]
    function = getattr(manager_installs, action + "_install" if action != "list" else "list_installs")
    return _plain(await answered(function(**arguments)))
