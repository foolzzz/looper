"""Operator-only switch: an ended task proposes memories instead of writing vault notes."""
from ...db import projects
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object, _STR
from ..ops import ErrorCode, OpError

MODE = {"type": "string", "enum": ["off", "on"]}
VIEW = _object({"memory_closeout": MODE}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("memory_closeout.get", "POST", "/api/v1/memory_closeout/get",
              "Read a project's memory closeout switch.",
              "Read a project's memory closeout switch. Off is the default.",
              _object({"project_id": _STR}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's project configuration
    Operation("memory_closeout.update", "POST", "/api/v1/memory_closeout/update",
              "Set whether a project's ended tasks propose memories instead of writing vault notes.",
              "Set whether a project's ended tasks propose memories instead of writing vault notes. "
              "Off is the default. Candidates still need the operator's confirmation.",
              _object({"project_id": _STR, "memory_closeout": MODE}, closed=True), VIEW,
              risk="write", confirms=False, operator_only=True,
              level=ActionLevel.INSIDE),  # stores a project flag; candidates still need the operator's confirmation
)


def _stored(value: str) -> str | None:
    if value == "on":
        return "on"
    if value == "off":
        return None
    raise OpError(ErrorCode.INVALID, "memory_closeout must be off or on")


def _shown(value: str | None) -> str:
    return "on" if value == "on" else "off"


async def run(server, operation, arguments, principal, headers):
    project_id = arguments["project_id"]
    if operation.operation_id == "memory_closeout.update":
        if not await projects.set_memory_closeout(project_id, _stored(arguments["memory_closeout"])):
            raise OpError(ErrorCode.NOT_FOUND, "project not found")
        stored = arguments["memory_closeout"]
    else:
        project = await projects.get_project(project_id)
        if project is None:
            raise OpError(ErrorCode.NOT_FOUND, "project not found")
        stored = _shown(project.get("memory_closeout"))
    return {"memory_closeout": stored}
