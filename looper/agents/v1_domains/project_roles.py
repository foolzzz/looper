"""Project role templates and statistics; task roles keep their copied versions."""

from .. import role_api
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from ..ops.service import _plain

_KIND = {"type": "string", "enum": ["work", "review"]}
_KEY = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$"}
_IDS = {"project_id": _STR, "key": _KEY}
ROLE_ITEM = _object({"id": _STR, "kind": {"type": ["string", "null"],
    "enum": ["plugin", "skill", "mcp", None]}, "name": _NULL_STR, "description": _STR},
    ["id", "kind", "name"])
ROLE_TEMPLATE = _object({
    "key": _STR, "kind": _KIND, "description": _STR, "role_prompt": _STR, "harness": _NULL_STR,
    "profile": {**_object({"profile_id": _STR, "name": _STR, "allowed": _BOOL}), "type": ["object", "null"]},
    "items": _array(ROLE_ITEM), "updated_at": _STR})
ROLE_TEMPLATES = _object({"project_id": _STR, "role_text_max_chars": _INT,
    "templates": _array(ROLE_TEMPLATE), "role_items": _array(ROLE_ITEM)})
ROLE_STATS = _object({
    **dict.fromkeys(("runs", "running", "steps", "accepted", "failed", "retries", "contract_violations"), _INT),
    "cost_usd": {"type": "number"}, "contract_violation_rate": {"type": ["number", "null"]},
    "review_pass_rate": {"type": ["number", "null"]},
    "reviews": _object({"passed": _INT, "not_passed": _INT})})
PROJECT_ROLE_STATS = _object({"project_id": _STR, "tasks": _INT, "roles": _array(_object({
    "id": _STR, "kinds": _array(_KIND), "tasks": _INT, "templates": _array(_STR), "stats": ROLE_STATS})),
    "builtin": _object({"worker": ROLE_STATS, "reviewer": ROLE_STATS})})
SCHEMAS = {"RoleItem": ROLE_ITEM, "RoleTemplate": ROLE_TEMPLATE, "RoleTemplates": ROLE_TEMPLATES,
           "RoleStats": ROLE_STATS, "ProjectRoleStats": PROJECT_ROLE_STATS}
RESULTS = {}


def _op(name, description, request, result, risk):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("project_roles.stats", "Read project role statistics, optionally since a day (YYYY-MM-DD).",
        _object({"project_id": _STR, "since": _STR}, ["project_id"], closed=True), PROJECT_ROLE_STATS, "read"),
    _op("role_templates.list", "List a project's role templates and available role items.",
        _object({"project_id": _STR}, closed=True), ROLE_TEMPLATES, "read"),
    _op("role_templates.put", "Create or replace a project role template; copied task roles stay unchanged.",
        _object({**_IDS, "kind": _KIND, "description": _STR, "role_prompt": _STR, "harness": _NULL_STR,
                 "profile_id": _NULL_STR, "items": _array(_STR)}, closed=True), ROLE_TEMPLATES, "write"),
    _op("role_templates.delete", "Delete a project role template; copied task roles keep their versions.",
        _object(_IDS, closed=True), ROLE_TEMPLATES, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "role_templates.delete":
        return None
    view = await answered(role_api.templates_view(arguments["project_id"]))
    key = arguments["key"]
    if not any(template["key"] == key for template in view["templates"]):
        raise OpError(ErrorCode.NOT_FOUND, f"project {arguments['project_id']!r} has no role template {key!r}")
    return (f"Delete role template {key!r} from project {arguments['project_id']}? "
            "Future tasks cannot copy it; task roles already copied from it keep their content and versions.")


async def run(server, operation, arguments, principal, headers):
    project_id = arguments["project_id"]
    name = operation.operation_id
    if name == "project_roles.stats":
        request = role_api.role_stats(project_id, arguments.get("since"))
    elif name == "role_templates.list":
        request = role_api.templates_view(project_id)
    elif name == "role_templates.put":
        request = role_api.put_template(project_id, arguments["key"], {
            field: arguments[field] for field in role_api.TEMPLATE_FIELDS})
    else:
        request = role_api.delete_template(project_id, arguments["key"])
    return _plain(await answered(request))
