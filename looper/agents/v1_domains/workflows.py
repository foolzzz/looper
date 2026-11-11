"""Operator-owned workflow presets; tasks retain their creation-time snapshots."""

import asyncpg

from ...db import workflows as db_workflows
from .. import workflows
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_KEY = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,31}$"}
_ROLE = _object({"key": _STR, "kind": {"type": "string", "enum": ["work", "review"]},
    "role_prompt": _STR, "harness": _NULL_STR, "profile_id": _NULL_STR},
    ["key", "kind", "role_prompt"], closed=True)
_PANEL = {**_object({"members": _array(_STR), "accept_when": {
    "anyOf": [_STR, {"type": "object"}]}}, closed=True), "type": ["object", "null"]}
_VERIFICATION = {**_object({"role": _STR, "max_rounds": {"type": "integer", "minimum": 1, "maximum": 5}},
                          closed=True), "type": ["object", "null"]}
_FIELDS = {
    "key": _KEY, "name": {"type": "string", "minLength": 1, "maxLength": 60},
    "description": {"type": "string", "maxLength": 300},
    "planner_instructions": {"type": "string", "maxLength": 8000},
    "profiles": _object(dict.fromkeys(("planner", "worker", "reviewer"), _STR), [], closed=True),
    "roles": {**_array(_ROLE), "maxItems": 8}, "work_role": _NULL_STR, "review_panel": _PANEL,
    "max_step_retries": {"type": ["integer", "null"], "minimum": 0, "maximum": 20},
    "budget_usd": {"type": ["number", "null"], "exclusiveMinimum": 0}, "verification": _VERIFICATION,
    **dict.fromkeys(("review_all", "dependency_conditions", "distinct_reviewer_runtime", "adjudication",
                     "escalate_on_exhaustion", "phase_notices"), _BOOL),
}
_READ_ONLY = {"updated_at": _STR}
WORKFLOW = _object({**_FIELDS, **_READ_ONLY})
SCHEMAS = {"Workflow": WORKFLOW}
RESULTS = {}


def _op(action, description, request, result, risk="read"):
    return Operation(f"workflows.{action}", "POST", f"/api/v1/workflows/{action}", description, description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=True)


OPERATIONS = (
    _op("list", "List workflow presets.", _object({}, closed=True),
        _object({"workflows": _array(WORKFLOW)}, closed=True)),
    _op("get", "Read a workflow preset.", _object({"key": _KEY}, closed=True),
        _object({"workflow": WORKFLOW}, closed=True)),
    _op("put", "Create or replace a workflow preset; existing task snapshots stay unchanged.",
        _object({**_FIELDS, **_READ_ONLY}, ["key", "name"], closed=True),
        _object({"workflow": WORKFLOW}, closed=True), "write"),
    _op("delete", "Delete a workflow preset and clear project defaults; task snapshots stay unchanged.",
        _object({"key": _KEY}, closed=True), _object({"deleted": _STR}, closed=True), "high"),
)


def _view(row):
    return _plain({**workflows.Workflow.from_row(row).snapshot(), "updated_at": row["updated_at"]})


async def _get(key):
    row = await db_workflows.get(key)
    if row is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no workflow {key!r}")
    return row


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "workflows.delete":
        await _get(arguments["key"])
        return f"Delete workflow {arguments['key']!r} and clear its project defaults? Existing task snapshots stay unchanged."


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    if action == "list":
        return {"workflows": [_view(row) for row in await db_workflows.list_workflows()]}
    if action == "get":
        return {"workflow": _view(await _get(arguments["key"]))}
    if action == "put":
        try:
            workflow = workflows.Workflow.from_dict({k: v for k, v in arguments.items() if k not in _READ_ONLY})
            await workflows.check_profiles(workflow)
            row = await db_workflows.put(workflow)
        except workflows.WorkflowError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
        except asyncpg.UniqueViolationError:
            raise OpError(ErrorCode.INVALID, "a workflow with that name already exists") from None
        except asyncpg.ForeignKeyViolationError:
            raise OpError(ErrorCode.CONFLICT, "a workflow's Agent Profile was removed meanwhile; reload the profiles") from None
        return {"workflow": _view(row)}
    if not await db_workflows.delete(arguments["key"]):
        raise OpError(ErrorCode.NOT_FOUND, f"no workflow {arguments['key']!r}")
    return {"deleted": arguments["key"]}
