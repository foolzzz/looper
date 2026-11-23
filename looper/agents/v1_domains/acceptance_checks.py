"""Operator-only permission for a project's executable acceptance definitions."""
from ...db import projects
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object, _STR
from ..ops import ErrorCode, OpError
from ..project_execution import AcceptanceChecksMode, acceptance_checks_mode

MODE = {"type": "string", "enum": [mode.value for mode in AcceptanceChecksMode]}
VIEW = _object({"acceptance_checks": MODE}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("acceptance_checks.get", "POST", "/api/v1/acceptance_checks/get",
              "Read a project's acceptance check permission.",
              "Read a project's acceptance check permission. Off is the default.",
              _object({"project_id": _STR}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's project configuration
    Operation("acceptance_checks.update", "POST", "/api/v1/acceptance_checks/update",
              "Set a project's acceptance check permission.",
              "Set a project's acceptance check permission. Runs checks after Worker success and stores evidence; "
              "evidence does not affect decisions yet. Commands run as the server user and can read its files; local denies network, "
              "network permits it only when a check requests it. A private checkout is not a sandbox.",
              _object({"project_id": _STR, "acceptance_checks": MODE}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes stored permission; checks execute separately after Worker success
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "acceptance_checks.update":
        return None
    return (f"Set project {arguments['project_id']}'s acceptance_checks to {arguments['acceptance_checks']}? "
            "This permits checks after Worker success; evidence does not affect decisions yet. Commands run as the "
            "server user and can read its files; network mode permits network only when a check requests it.")


async def run(server, operation, arguments, principal, headers):
    project_id = arguments["project_id"]
    if operation.operation_id == "acceptance_checks.update":
        mode = acceptance_checks_mode(arguments["acceptance_checks"])
        if not await projects.set_acceptance_checks(project_id, None if mode is AcceptanceChecksMode.OFF else mode.value):
            raise OpError(ErrorCode.NOT_FOUND, "project not found")
    else:
        project = await projects.get_project(project_id)
        if project is None:
            raise OpError(ErrorCode.NOT_FOUND, "project not found")
        mode = acceptance_checks_mode(project.get("acceptance_checks"))
    return {"acceptance_checks": mode.value}
