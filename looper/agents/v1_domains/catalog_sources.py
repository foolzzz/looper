"""Acquire catalog content without installing it or exposing local credentials."""

from .. import catalog, catalog_sources as sources, library, mcp, project_skills
from ..api_v1 import Operation, STATUS, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from ..projects import RequestError
from .catalog import CATALOG, CATALOG_ITEM, MCP_CONFIG, LIBRARY_SKIPPED, _enum


_GIT = {"url": _STR, "ref": _STR, "subpath": _STR}
_SOURCE = {"url": _STR, "ref": _NULL_STR, "subpath": _STR, "commit": _STR}
_NAME = {"type": "string", "maxLength": sources.NAME_MAX_CHARS}
_SELECTED = _object({"path": _STR, "name": _NAME}, ["path"], closed=True)
GIT_PREVIEW_ITEM = _object({
    "path": _STR, "kind": _enum("skill", "plugin"), "name": _STR, "description": _STR,
    "version": _STR, "file_count": _INT, "size_bytes": _INT,
    "status": _enum("new", "update", "unchanged", "conflict"),
    "reason": _STR, "item_id": _STR, "modified": _BOOL},
    ["path", "kind", "name", "description", "version", "file_count", "size_bytes", "status"])
GIT_PREVIEW = _object({**_SOURCE, "items": _array(GIT_PREVIEW_ITEM), "skipped": _array(LIBRARY_SKIPPED)})
GIT_IMPORT = _object({**_SOURCE, "items": _array(CATALOG_ITEM), "added": _array(_STR),
                      "updated": _array(_STR), "unchanged": _array(_STR), "skipped": _array(LIBRARY_SKIPPED)})
# Reuse the catalog configuration projection; only input secret maps are write-only.
_MCP_INPUT = {key: {**schema, **({"writeOnly": True} if key in ("env", "headers") else {})}
              for key, schema in MCP_CONFIG["properties"].items()}


def _op(action, request, result, *, risk="write", credential=False, confirms=False):
    name = f"catalog_sources.{action}"
    description = f"{action.replace('_', ' ').capitalize()} into the catalog without installing items."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=confirms,
                     operator_only=credential, credential_write=credential)


OPERATIONS = (
    _op("import_machine", _object({}, closed=True), CATALOG, credential=True),
    _op("import_mcp", _object({}, closed=True), CATALOG, credential=True),
    _op("upload", _object({"filename": _STR, "content_base64": {
        "type": "string", "maxLength": 4 * ((sources.UPLOAD_MAX_BYTES + 2) // 3)}, "name": _NAME},
        ["filename", "content_base64"], closed=True),
        _object({"item": CATALOG_ITEM, "skipped": _array(LIBRARY_SKIPPED)})),
    _op("git_preview", _object(_GIT, ["url"], closed=True), GIT_PREVIEW, risk="read"),
    _op("git_import", _object({**_GIT, "commit": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
        "items": {**_array(_SELECTED), "minItems": 1}}, ["url", "commit", "items"], closed=True),
        GIT_IMPORT, risk="high", confirms=True),
    _op("create_skill", _object({"name": _NAME, "description": {
        "type": "string", "maxLength": sources.DESCRIPTION_MAX_CHARS}, "content": {
        "type": "string", "maxLength": project_skills.CONTENT_MAX_BYTES}}, closed=True),
        _object({"item": CATALOG_ITEM})),
    _op("create_mcp", _object({"name": _NAME, "description": {
        "type": "string", "maxLength": mcp.DESCRIPTION_MAX}, **_MCP_INPUT},
        ["name", "transport"], closed=True), _object({"item": CATALOG_ITEM}), credential=True),
)
RESULTS = {}


async def _answer(request, *, credential=False):
    try:
        return _plain(await request)
    except (catalog.CatalogError, catalog.CatalogNotFound, catalog.CatalogConflict,
            library.LibraryError, project_skills.ProjectSkillError) as exc:
        code = (ErrorCode.NOT_FOUND if isinstance(exc, catalog.CatalogNotFound) else
                ErrorCode.CONFLICT if isinstance(exc, catalog.CatalogConflict) else ErrorCode.INVALID)
        if credential:
            raise RequestError(STATUS[code], "invalid credential request") from None
        raise OpError(code, str(exc)) from None


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "catalog_sources.git_import":
        return None
    if principal.kind == "client":
        preview = await _answer(sources.git_preview({key: arguments[key] for key in _GIT if key in arguments}))
        if preview["commit"] != arguments["commit"]:
            raise OpError(ErrorCode.CONFLICT, "the repository moved since the preview; preview it again")
    selected = ", ".join(item["path"] for item in arguments["items"])
    # Every client import is confirmed: local edits can arrive during the next clone.
    return (f"Import selected catalog paths {selected} at commit {arguments['commit']}? "
            "Existing items from this repository will be replaced, including local edits present at execution; "
            "installations are preserved and new items are installed nowhere.")


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.split(".")[1]
    if action == "import_machine":
        request = library.import_from_machine()
    elif action == "import_mcp":
        request = mcp.import_from_machine()
    elif action == "create_mcp":
        request = mcp.create(arguments)
    else:
        request = getattr(sources, action)(arguments)
    return await _answer(request, credential=operation.credential_write)
