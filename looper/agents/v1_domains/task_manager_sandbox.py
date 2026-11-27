"""Operator-only per-project network allowlist of the task Manager's sandbox."""

from ...db import projects as db_projects
from ...db import task_manager_sandbox_policy as store
from .. import task_manager_network
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _INT, _STR, _array, _object
from ..ops import ErrorCode, OpError
from ..ops.config import project_named

_FIELDS = {"project": _STR, "network_allowed_domains": _array(_STR), "revision": _INT}
VIEW = _object(_FIELDS, closed=True)
UPDATED = _object({**_FIELDS, "added": _array(_STR), "removed": _array(_STR)}, closed=True)
_PROJECT = {"type": "string", "minLength": 1}
RESULTS = {}
OPERATIONS = (
    Operation(
        "projects.task_manager_sandbox_get", "POST", "/api/v1/projects/task_manager_sandbox_get",
        "Read a project's task Manager sandbox network allowlist and its revision.",
        "Read a project's task Manager sandbox network allowlist and its revision. The default is an empty list "
        "(revision 0). " + task_manager_network.SCOPE_TEXT,
        _object({"project": _PROJECT}, closed=True), VIEW, risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's own project policy row
    Operation(
        "projects.task_manager_sandbox_update", "POST", "/api/v1/projects/task_manager_sandbox_update",
        "Replace a project's task Manager sandbox network allowlist.",
        "Replace a project's task Manager sandbox network allowlist. The whole list is replaced; only external "
        "domains are accepted (at most 50, unique, no IP literals, ports or bare wildcards), else "
        "network_policy_invalid and nothing is written. expected_revision, when given, must match or the call "
        "returns conflict. Each change writes an audit row, and the next task Manager turn restarts with the new "
        "list. " + task_manager_network.SCOPE_TEXT,
        _object({"project": _PROJECT, "network_allowed_domains": _array(_STR),
                 "expected_revision": {"type": "integer", "minimum": 0}},
                ["project", "network_allowed_domains"], closed=True),
        UPDATED, risk="write", operator_only=True,
        level=ActionLevel.INSIDE),  # writes only Looper's own policy and audit rows; the sandbox applies it later
)


async def _project_id(raw: str) -> str:
    row = await db_projects.get_project(raw)
    return row["id"] if row is not None else (await project_named(raw))["id"]


async def run(server, operation, arguments, principal, headers):
    project_id = await _project_id(arguments["project"])
    if operation.operation_id == "projects.task_manager_sandbox_get":
        domains, revision = await store.get(project_id)
        return {"project": project_id, "network_allowed_domains": domains, "revision": revision}
    try:
        domains = list(task_manager_network.validate_domains(arguments["network_allowed_domains"]))
    except task_manager_network.PolicyInvalid as exc:
        raise OpError(ErrorCode.NETWORK_POLICY_INVALID, str(exc), field=exc.field) from None
    try:
        old, _, revision = await store.update(project_id, domains, actor="operator",
                                              expected_revision=arguments.get("expected_revision"))
    except store.RevisionConflict as exc:
        raise OpError(ErrorCode.CONFLICT, f"expected_revision does not match the stored revision {exc.revision}",
                      details={"current_version": exc.revision}) from None
    stored, revision = await store.get(project_id)
    return {"project": project_id, "network_allowed_domains": stored, "revision": revision,
            "added": [d for d in stored if d not in old], "removed": [d for d in old if d not in stored]}
