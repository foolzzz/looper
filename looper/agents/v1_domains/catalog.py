"""Full dashboard catalog projections and the existing compact Tool results."""

from .. import catalog
from ..api_v1 import Operation, STATUS, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from ..projects import RequestError

ORDER = 4


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


INSTALL_ROLE = _enum("manager", "planner", "worker", "reviewer", "task_role")
PROJECT_ROLE = _enum("planner", "worker", "reviewer", "task_role")
INSTALL_SCOPE = _enum("global", "project")
LIBRARY_KIND = _enum("plugin", "skill", "mcp")
MCP_TRANSPORT = _enum("stdio", "http", "sse")
_STRINGS = _array(_STR)
_VARIABLES = {"type": "object", "additionalProperties": _STR}
MCP_CONFIG = _object({"transport": MCP_TRANSPORT, "command": _STR, "args": _STRINGS,
                      "env": _VARIABLES, "url": _STR, "headers": _VARIABLES})
INSTALL = _object({
    "install_id": _STR, "item_id": _STR, "scope": INSTALL_SCOPE,
    "project_id": _NULL_STR, "project_name": _NULL_STR, "roles": _array(INSTALL_ROLE),
    "enabled": _BOOL, "created_at": _STR, "updated_at": _STR})
CATALOG_ITEM = _object({
    "item_id": _STR, "kind": LIBRARY_KIND, "name": _STR, "qualified_name": _STR,
    "origin": _enum("machine", "upload", "git", "ui", "project"),
    "owner_project_id": _NULL_STR, "owner_project_name": _NULL_STR, "description": _STR,
    "source": _STR, "version": _STR, "sha256": _STR, "size_bytes": _INT, "file_count": _INT,
    "modified": _BOOL, "imported_at": _STR, "updated_at": _STR,
    "mcp": _nullable(MCP_CONFIG), "installs": _array(INSTALL)})
_SKIPPED = {"item": _STR, "path": _STR, "reason": _STR}
LIBRARY_SKIPPED = _object({**_SKIPPED, "size_bytes": {"type": ["integer", "null"]}})
_IMPORT = {"imported_at": _STR, "source": _STR, "added": _STRINGS, "updated": _STRINGS,
           "removed": _STRINGS, "unchanged": _INT, "skipped_count": _INT}
LIBRARY_IMPORT = _object({**_IMPORT, "plugins": _INT, "skills": _INT,
                          "skipped": _array(LIBRARY_SKIPPED)})
MCP_IMPORT = _object({**_IMPORT, "servers": _INT, "skipped": _array(_object(_SKIPPED))})
CATALOG_CAPS = _object(dict.fromkeys(("file_max_bytes", "item_max_bytes", "item_max_files",
                                     "library_max_bytes", "upload_max_bytes"), _INT))
MCP_CAPS = _object({**dict.fromkeys(("servers_max", "variables_max", "value_max", "args_max"), _INT),
                    "mask": _STR})
SKILL_CAPS = _object(dict.fromkeys(("name_max_chars", "description_max_chars", "content_max_bytes",
                                   "project_max_skills"), _INT))
CATALOG = _object({
    "items": _array(CATALOG_ITEM), "installed_count": _INT, "total_bytes": _INT,
    "last_import": _nullable(LIBRARY_IMPORT), "last_mcp_import": _nullable(MCP_IMPORT),
    "roles": _array(INSTALL_ROLE), "task_roles": _array(PROJECT_ROLE),
    "caps": CATALOG_CAPS, "mcp_caps": MCP_CAPS})
CATALOG_ITEM_DETAIL = _object({
    "item": CATALOG_ITEM, "files": _array(_object({"path": _STR, "size_bytes": _INT, "executable": _BOOL})),
    "skill_md": _NULL_STR, "content": _NULL_STR})
INSTALL_CHANGE = _object({"install": INSTALL, "item": CATALOG_ITEM})
PROJECT_INSTALL = _object({**INSTALL["properties"], "inherited": _BOOL, "item": CATALOG_ITEM})
PROJECT_INSTALLS = _object({"project_id": _STR, "installs": _array(PROJECT_INSTALL), "owned_count": _INT,
                            "task_roles": _array(PROJECT_ROLE), "skill_caps": SKILL_CAPS})
SKILL_PATCH = {**_object(dict.fromkeys(("name", "description", "content"), _STR), [], closed=True),
               "minProperties": 1}
MCP_PATCH = {**_object({"name": _STR, "description": _STR, "transport": MCP_TRANSPORT, "command": _STR,
                        "url": _STR, "args": _STRINGS, "env": _VARIABLES, "headers": _VARIABLES}, [], closed=True),
             "minProperties": 1}

COMPACT_ITEM = _object({"item_id": _STR, "kind": LIBRARY_KIND, "name": _STR,
                        "description": _NULL_STR, "project": _NULL_STR})
COMPACT_INSTALL = _object({"install_id": _STR, "scope": _STR, "project": _NULL_STR,
                           "roles": _STRINGS, "enabled": _BOOL})
_COMPACT_CHANGE = _object({"install": COMPACT_INSTALL, "item": COMPACT_ITEM})
RESULTS = {
    "catalog.list": {"oneOf": [
        _object({"items": _array(_object({**COMPACT_ITEM["properties"], "installs": _array(COMPACT_INSTALL)}))}),
        _object({"project": _STR, "installs": _array(_object({**COMPACT_INSTALL["properties"],
                                                             "inherited": _BOOL, "item": COMPACT_ITEM}))})]},
    "catalog.install": _object({**_COMPACT_CHANGE["properties"], "note": _STR}),
    "catalog.update": _COMPACT_CHANGE,
    "catalog.uninstall": _object({"deleted": _STR, "item": COMPACT_ITEM}),
}


def _op(name, request, result, *, risk="read", credential=False):
    description = f"{name.replace('_', ' ').replace('.', ': ')}."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high",
                     operator_only=credential, credential_write=credential)


_ITEM_ID = {"item_id": _STR}
_INSTALL_ID = {"install_id": _STR}
_ROLES = {**_array(INSTALL_ROLE), "minItems": 1}
OPERATIONS = (
    _op("catalog_items.list", _object({}, closed=True), CATALOG),
    _op("catalog_items.get", _object(_ITEM_ID, closed=True), CATALOG_ITEM_DETAIL),
    # MCP patches can contain secrets: neither arguments nor failures enter the audit trail.
    _op("catalog_items.update", _object({**_ITEM_ID, "fields": {"anyOf": [SKILL_PATCH, MCP_PATCH]}}, closed=True),
        _object({"item": CATALOG_ITEM}), risk="write", credential=True),
    _op("catalog_items.delete", _object(_ITEM_ID, closed=True), _object({"deleted": _STR}), risk="high"),
    _op("installs.create", _object({**_ITEM_ID, "scope": INSTALL_SCOPE, "project_id": _STR,
                                    "roles": _ROLES, "enabled": _BOOL}, ["item_id", "scope"], closed=True),
        INSTALL_CHANGE, risk="write"),
    _op("installs.update", _object({**_INSTALL_ID, "roles": _ROLES, "enabled": _BOOL}, ["install_id"], closed=True),
        INSTALL_CHANGE, risk="write"),
    _op("installs.delete", _object(_INSTALL_ID, closed=True), _object({"deleted": _STR, "item": CATALOG_ITEM}),
        risk="write"),
    _op("installs.project", _object({"project_id": _STR}, closed=True), PROJECT_INSTALLS),
)


async def _deletable(item_id):
    item = await catalog._item(item_id)
    if item["origin"] == "project":
        raise catalog.CatalogError("a project's own skill is deleted from its project's Skills tab")
    return item


async def _answer(request, *, credential=False):
    try:
        return _plain(await request)
    except (catalog.CatalogNotFound, catalog.CatalogConflict, catalog.CatalogError) as exc:
        code = (ErrorCode.NOT_FOUND if isinstance(exc, catalog.CatalogNotFound) else
                ErrorCode.CONFLICT if isinstance(exc, catalog.CatalogConflict) else ErrorCode.INVALID)
        if credential:
            # The transport redacts this message while preserving the service's HTTP error code.
            raise RequestError(STATUS[code], "invalid credential request") from None
        raise OpError(code, str(exc)) from None


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "catalog_items.delete":
        return None
    item = await _answer(_deletable(arguments["item_id"]))
    return (f"Delete catalog item {item['qualified_name']!r} and all {len(item['installs'])} installations? "
            "Its stored files and installations will be removed; other catalog items are unchanged.")


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "catalog_items.list":
        return await _answer(catalog.catalog_view())
    if name == "catalog_items.get":
        return await _answer(catalog.item_detail(arguments["item_id"]))
    if name == "catalog_items.update":
        return await _answer(catalog.update_item(arguments["item_id"], arguments["fields"]), credential=True)
    if name == "catalog_items.delete":
        # Confirmation is not a reservation: re-read existence and ownership before deleting.
        await _answer(_deletable(arguments["item_id"]))
        return await _answer(catalog.delete_item(arguments["item_id"]))
    if name == "installs.create":
        return await _answer(catalog.install(arguments))
    if name == "installs.update":
        return await _answer(catalog.update_install(arguments["install_id"],
                            {key: value for key, value in arguments.items() if key != "install_id"}))
    if name == "installs.delete":
        return await _answer(catalog.uninstall(arguments["install_id"]))
    return await _answer(catalog.project_view(arguments["project_id"]))
