"""Project-owned skills; installation switches belong to the catalog domain."""

from .. import catalog, project_skills
from ..api_v1 import Operation, _object, _STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from .catalog import PROJECT_INSTALLS, _answer as catalog_answer

ORDER = 6


PROJECT_SKILL = _object({
    "skill_id": _STR, "name": _STR, "qualified_name": _STR, "description": _STR,
    "created_at": _STR, "updated_at": _STR, "size_bytes": _INT})
_FIELDS = dict.fromkeys(("name", "description", "content"), _STR)
_IDS = {"project_id": _STR, "skill_id": _STR}
_CHANGE = _object({**PROJECT_INSTALLS["properties"], "skill": PROJECT_SKILL})


def _op(action, request, result, risk):
    name = f"project_skills.{action}"
    description = f"{action.capitalize()} a project-owned skill."
    if action == "delete":
        description += " Removes its installations in every project; client tokens require session confirmation."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("add", _object({"project_id": _STR, "skill": _object({**_FIELDS, "enabled": _BOOL},
                                                          list(_FIELDS), closed=True)}, closed=True),
        _CHANGE, "write"),
    _op("get", _object(_IDS, closed=True), _object({"skill": PROJECT_SKILL, "content": _STR}), "read"),
    _op("update", _object({**_IDS, "fields": {**_object(_FIELDS, [], closed=True), "minProperties": 1}},
                           closed=True), _CHANGE, "write"),
    _op("delete", _object(_IDS, closed=True),
        _object({**PROJECT_INSTALLS["properties"], "deleted": _STR}), "high"),
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
    skill_id = arguments["skill_id"]
    if name == "project_skills.get":
        return await _answer(project_skills.get_skill(project_id, skill_id))
    if name == "project_skills.update":
        return await _answer(project_skills.update_skill(project_id, skill_id, arguments["fields"]))
    # A pending confirmation reserves nothing: read current references and ownership again.
    # The core deletes the item and all current installations in one transaction.
    await _references(arguments)
    return await _answer(project_skills.delete_skill(project_id, skill_id))
