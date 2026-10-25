"""Project setting layers and the profiles their Planners may choose."""

from ...db import projects as db_projects
from .. import agent_settings, projects
from ..api_v1 import _array, _object, _STR, _NULL_STR, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from ..ops.settings import _Place
from .global_settings import LAYER_UPDATE, PROJECT_AGENT_SETTINGS, _operation, layer_impact

PROJECT_ROLE = {"type": "string", "enum": [role.value for role in agent_settings.TASK_ROLES]}
ALLOWED_PROFILE = _object({
    "profile_id": _STR, "name": _STR, "runtime": _STR, "model": _STR, "effort": _NULL_STR,
    "roles": _array({"type": "string", "enum": ["worker", "reviewer"]}),
    "usd_per_million_tokens": _object({
        name: {"type": ["number", "null"]} for name in ("input", "output")}, closed=True),
}, closed=True)
ALLOWED_PROFILES = _object({
    "project_id": _STR, "restricted": _BOOL,
    "profiles": _array(ALLOWED_PROFILE), "choosable": _array(ALLOWED_PROFILE),
}, closed=True)

# This tool emits the saved list, without the dashboard's choosable/restricted envelope.
RESULTS = {"settings.allowed_profiles": _object({
    "project": _STR, "before": _array(_STR), "allowed_profiles": _array(ALLOWED_PROFILE), "note": _STR,
}, closed=True)}

OPERATIONS = (
    _operation("project_agent_settings.get", "Read a project's setting layers and effective role settings.",
               _object({"project_id": _STR}, closed=True), PROJECT_AGENT_SETTINGS),
    _operation("project_agent_settings.update", "Update the setting layer inherited by a project's roles.",
               _object({"project_id": _STR, "values": LAYER_UPDATE}, closed=True),
               PROJECT_AGENT_SETTINGS, "write", True),
    _operation("project_agent_settings.update_role", "Update one role's setting layer in a project.",
               _object({"project_id": _STR, "role": PROJECT_ROLE, "values": LAYER_UPDATE}, closed=True),
               PROJECT_AGENT_SETTINGS, "write", True),
    _operation("allowed_profiles.get", "Read the profiles a project's Planners may choose.",
               _object({"project_id": _STR}, closed=True), ALLOWED_PROFILES),
    _operation("allowed_profiles.replace", "Replace a project's allowed profile list; an empty list is unrestricted.",
               _object({"project_id": _STR, "profile_ids": _array(_STR)}, closed=True),
               ALLOWED_PROFILES, "write"),
)


async def impact(server, operation, arguments, principal, headers):
    if not operation.operation_id.startswith("project_agent_settings.update"):
        return None
    project = await db_projects.get_project(arguments["project_id"])
    if project is None:
        raise OpError(ErrorCode.NOT_FOUND, f"project {arguments['project_id']!r} not found")
    role = agent_settings.AgentType(arguments["role"]) if "role" in arguments else None
    place = _Place(agent_settings.Layer.ROLE_PROJECT if role else agent_settings.Layer.PROJECT, role, project)
    return await layer_impact(place, arguments["values"])


async def run(server, operation, arguments, principal, headers):
    name, project_id = operation.operation_id, arguments["project_id"]
    if name == "project_agent_settings.get":
        return await answered(agent_settings.project_settings_view(project_id))
    if name.startswith("project_agent_settings.update"):
        return await answered(agent_settings.update_project(project_id, arguments["values"],
                                                            role=arguments.get("role")))
    if name == "allowed_profiles.replace":
        # The core replaces and prunes Planner choices in one transaction.
        return await answered(projects.set_allowed_profiles(project_id, {"profile_ids": arguments["profile_ids"]}))
    return await answered(projects.allowed_profiles_view(project_id))
