"""Project-owned skills; installation switches belong to the catalog domain."""

from .. import catalog, project_skills, skill_lifecycle, skill_versions
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from .catalog import PROJECT_INSTALLS, _answer as catalog_answer, _enum

ORDER = 6


PROJECT_SKILL = _object({
    "skill_id": _STR, "name": _STR, "qualified_name": _STR, "description": _STR,
    "created_at": _STR, "updated_at": _STR, "size_bytes": _INT,
    "lifecycle": _enum(*(member.value for member in skill_lifecycle.Lifecycle))})
_FIELDS = dict.fromkeys(("name", "description", "content"), _STR)
_IDS = {"project_id": _STR, "skill_id": _STR}
_CHANGE = _object({**PROJECT_INSTALLS["properties"], "skill": PROJECT_SKILL})


def _op(action, request, result, risk):
    name = f"project_skills.{action}"
    description = f"{action.capitalize()} a project-owned skill."
    if action == "delete":
        description += " Removes its installations in every project; client tokens require session confirmation."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high", level=ActionLevel.INSIDE)


_VERSIONS_SUMMARY = "List one project skill's versions, and the difference between two of them."
_ROLLBACK_SUMMARY = "Write a new version of a project skill from an older version's content."
_DEPRECATE_SUMMARY = "Mark a project skill deprecated so new runs skip it."
_RESTORE_SUMMARY = "Mark a deprecated project skill active so new runs load it again."
_BUDGET_SUMMARY = "Count the skill name and description characters a run of each role would list."
_AUTHOR = {"type": "string", "enum": ["operator", "proposal", "rollback"]}
_VERSION = _object({
    "version": _INT, "name": _STR, "description": _STR, "sha256": _STR, "author_kind": _AUTHOR,
    "author_ref": {"type": ["string", "null"]}, "reason": _STR, "created_at": _STR,
})
_AFFECTED = _object({"project_id": _STR, "project_name": _STR})
_DIFF = _object({
    "from_version": _INT, "to_version": _INT,
    "name": _object({"from": _STR, "to": _STR}),
    "description": _object({"from": _STR, "to": _STR}),
    "content": _STR,
})
_VERSIONS = _object({
    "skill_id": _STR, "current_version": {"type": ["integer", "null"]}, "versions": _array(_VERSION),
    "affected_projects": _array(_AFFECTED), "diff": {**_DIFF, "type": ["object", "null"]},
})
_ROLLBACK = _object({**_CHANGE["properties"], "version": _INT, "affected_projects": _array(_AFFECTED)})
_LIFECYCLE = _object({**_CHANGE["properties"], "affected_projects": _array(_AFFECTED)})
_BUDGET_ROLE = _object({"chars": _INT, "skills": _INT, "over": _BOOL})
_BUDGET = _object({
    "limit": _INT,
    "roles": _object({"planner": _BUDGET_ROLE, "worker": _BUDGET_ROLE, "reviewer": _BUDGET_ROLE}),
    "counts_plugin_skills": _BOOL,
})


OPERATIONS = (
    _op("add", _object({"project_id": _STR, "skill": _object({**_FIELDS, "enabled": _BOOL},
                                                          list(_FIELDS), closed=True)}, closed=True),
        _CHANGE, "write"),
    _op("get", _object(_IDS, closed=True), _object({"skill": PROJECT_SKILL, "content": _STR}), "read"),
    _op("update", _object({**_IDS, "fields": {**_object(_FIELDS, [], closed=True), "minProperties": 1}},
                           closed=True), _CHANGE, "write"),
    _op("delete", _object(_IDS, closed=True),
        _object({**PROJECT_INSTALLS["properties"], "deleted": _STR}), "high"),
    Operation(
        "project_skills.rollback", "POST", "/api/v1/project_skills/rollback", _ROLLBACK_SUMMARY,
        _ROLLBACK_SUMMARY + " The new version keeps that content and hash. Returns the projects that install the skill.",
        _object({**_IDS, "version": _INT}, closed=True), _ROLLBACK, risk="write", level=ActionLevel.INSIDE,
        operator_only=True,
    ),
    Operation(
        "project_skills.versions", "POST", "/api/v1/project_skills/versions", _VERSIONS_SUMMARY,
        _VERSIONS_SUMMARY + " Each version records the content hash, author kind, reason and time.",
        _object({**_IDS, "from_version": _INT, "to_version": _INT}, ["project_id", "skill_id"], closed=True),
        _VERSIONS, risk="read", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.deprecate", "POST", "/api/v1/project_skills/deprecate", _DEPRECATE_SUMMARY,
        _DEPRECATE_SUMMARY + " Installs, versions and files stay. An optional reason is not stored on the skill; it is kept only in the operator action record.",
        _object({**_IDS, "reason": _STR}, ["project_id", "skill_id"], closed=True),
        _LIFECYCLE, risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.restore", "POST", "/api/v1/project_skills/restore", _RESTORE_SUMMARY,
        _RESTORE_SUMMARY + " Installs, versions and files stay.",
        _object(_IDS, closed=True), _LIFECYCLE, risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.context_budget", "POST", "/api/v1/project_skills/context_budget", _BUDGET_SUMMARY,
        _BUDGET_SUMMARY + " The 8000-character line is a display threshold. Plugin-bundled skills are not counted.",
        _object({"project_id": _STR}, closed=True), _BUDGET, risk="read", level=ActionLevel.INSIDE,
        operator_only=True,
    ),
)
RESULTS = {}


async def _answer(request):
    try:
        return _plain(await request)
    except (project_skills.ProjectSkillError, project_skills.ProjectSkillNotFound,
            project_skills.ProjectSkillConflict) as exc:
        code = (ErrorCode.NOT_FOUND if isinstance(exc, project_skills.ProjectSkillNotFound) else
                ErrorCode.CONFLICT if isinstance(exc, project_skills.ProjectSkillConflict) else ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None


async def _references(arguments):
    # Ownership is checked before reading the catalog's full installation projection.
    await _answer(project_skills.get_skill(arguments["project_id"], arguments["skill_id"]))
    return (await catalog_answer(catalog.item_detail(arguments["skill_id"])))["item"]


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "project_skills.delete":
        return None
    item = await _references(arguments)
    places = sorted({f"{install['project_name']} ({install['project_id']})" for install in item["installs"]})
    return (f"Delete project skill {item['qualified_name']!r} and all {len(item['installs'])} installations "
            f"across projects: {', '.join(places) or 'none'}? "
            "Its stored content and installations will be removed; other catalog items stay unchanged.")


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    project_id = arguments["project_id"]
    if name == "project_skills.add":
        return await _answer(project_skills.add_skill(project_id, arguments["skill"]))
    if name == "project_skills.context_budget":
        return await _answer(skill_lifecycle.context_budget(project_id))
    skill_id = arguments["skill_id"]
    if name == "project_skills.get":
        return await _answer(project_skills.get_skill(project_id, skill_id))
    if name == "project_skills.update":
        return await _answer(project_skills.update_skill(project_id, skill_id, arguments["fields"]))
    if name == "project_skills.versions":
        return await _answer(skill_versions.versions(
            project_id, skill_id, from_version=arguments.get("from_version"),
            to_version=arguments.get("to_version")))
    if name == "project_skills.rollback":
        return await _answer(skill_versions.rollback(project_id, skill_id, arguments["version"]))
    if name == "project_skills.deprecate":
        return await _answer(skill_lifecycle.deprecate(project_id, skill_id, reason=arguments.get("reason", "")))
    if name == "project_skills.restore":
        return await _answer(skill_lifecycle.restore(project_id, skill_id))
    # A pending confirmation reserves nothing: read current references and ownership again.
    # The core deletes the item and all current installations in one transaction.
    await _references(arguments)
    return await _answer(project_skills.delete_skill(project_id, skill_id))
