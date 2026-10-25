"""Project reference documents and lesson drafts through the shared knowledge service."""

from .. import knowledge
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain


_KIND = {"type": "string", "enum": ["text", "upload", "path"]}
DOCUMENT = _object({
    **dict.fromkeys(("document_id", "title", "summary", "read_path", "sha256", "created_at", "updated_at"), _STR),
    "source_kind": _KIND, "filename": _NULL_STR, "path": _NULL_STR, "missing": _BOOL, "size_bytes": _INT})
KNOWLEDGE_VIEW = _object({
    "project_id": _STR, "documents": _array(DOCUMENT), "count": _INT, "stored_bytes": _INT,
    "index": _STR, "index_chars": _INT, "caps": _object(dict.fromkeys((
        "document_max_bytes", "project_max_documents", "project_max_bytes", "index_max_chars"), _INT))})
DRAFT = _object({
    **dict.fromkeys(("draft_id", "title", "summary", "content", "created_at"), _STR),
    "task_id": _NULL_STR, "size_bytes": _INT})
DRAFTS_VIEW = _object({"project_id": _STR, "drafts": _array(DRAFT), "count": _INT})
NEW_DOCUMENT = {"oneOf": [
    _object({"title": _STR, "summary": _STR, "source_kind": {"const": kind},
             **dict.fromkeys(fields, _STR)}, closed=True)
    for kind, fields in (("text", ("content",)), ("upload", ("filename", "content_base64")), ("path", ("path",)))
]}
DOCUMENT_UPDATE = {**_object({
    **dict.fromkeys(("title", "summary", "content", "filename", "content_base64", "path"), _STR),
    "source_kind": _KIND}, [], closed=True), "minProperties": 1}
_PROJECT = {"project_id": _STR}
_DOCUMENT_ID = {**_PROJECT, "document_id": _STR}
_DRAFT_ID = {**_PROJECT, "draft_id": _STR}
_CHANGE = {**KNOWLEDGE_VIEW["properties"], "document": DOCUMENT}


def _op(action, request, result, risk):
    name = f"project_knowledge.{action}"
    description = f"{action.replace('_', ' ').capitalize()} project knowledge."
    if risk == "high":
        description += " Removes only the selected item; client tokens require session confirmation."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("list", _object(_PROJECT, closed=True), KNOWLEDGE_VIEW, "read"),
    _op("add", _object({**_PROJECT, "document": NEW_DOCUMENT}, closed=True), _object(_CHANGE), "write"),
    _op("get", _object(_DOCUMENT_ID, closed=True), _object({
        "document": DOCUMENT, "content": _NULL_STR, "content_error": _STR}, ["document", "content"]), "read"),
    _op("update", _object({**_DOCUMENT_ID, "fields": DOCUMENT_UPDATE}, closed=True), _object(_CHANGE), "write"),
    _op("delete", _object(_DOCUMENT_ID, closed=True), _object({
        **KNOWLEDGE_VIEW["properties"], "deleted": _STR}), "high"),
    _op("drafts", _object(_PROJECT, closed=True), DRAFTS_VIEW, "read"),
    _op("confirm_draft", _object({**_DRAFT_ID, "fields": _object(dict.fromkeys(
        ("title", "summary", "content"), _STR), [], closed=True)}, list(_DRAFT_ID), closed=True),
        _object({**_CHANGE, "confirmed": _STR}), "write"),
    _op("discard_draft", _object(_DRAFT_ID, closed=True), _object({
        **DRAFTS_VIEW["properties"], "discarded": _STR}), "high"),
)
RESULTS = {}


async def _answer(request):
    try:
        return _plain(await request)
    except knowledge.KnowledgeNotFound as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except knowledge.KnowledgeError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


def _root(server):
    if server._runtime_root is None:
        raise OpError(ErrorCode.UNAVAILABLE, "the server was started without a runtime root")
    return server._runtime_root


async def impact(server, operation, arguments, principal, headers):
    if operation.risk != "high":
        return None
    project_id = arguments["project_id"]
    if operation.operation_id == "project_knowledge.delete":
        document = (await _answer(knowledge.get_document(
            project_id, arguments["document_id"], _root(server))))["document"]
        return (f"Delete document {document['title']!r} ({document['document_id']}) from project {project_id}? "
                "Removes its reference and stored content from project knowledge; the original project file "
                "and existing agent copies remain unchanged.")
    drafts = await _answer(knowledge.drafts_view(project_id))
    draft = next((row for row in drafts["drafts"] if row["draft_id"] == arguments["draft_id"]), None)
    if draft is None:
        raise OpError(ErrorCode.NOT_FOUND, f"project {project_id!r} has no draft {arguments['draft_id']!r}")
    return (f"Discard lesson draft {draft['title']!r} ({draft['draft_id']}) from project {project_id}? "
            "Permanently removes this draft; confirmed documents and other drafts remain unchanged.")


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.removeprefix("project_knowledge.")
    project_id = arguments["project_id"]
    root = _root(server)
    if action == "list":
        return await _answer(knowledge.knowledge_view(project_id, root))
    if action == "add":
        return await _answer(knowledge.add_document(project_id, arguments["document"], root))
    if action == "get":
        return await _answer(knowledge.get_document(project_id, arguments["document_id"], root))
    if action == "update":
        return await _answer(knowledge.update_document(project_id, arguments["document_id"], arguments["fields"], root))
    if action == "delete":
        return await _answer(knowledge.delete_document(project_id, arguments["document_id"], root))
    if action == "drafts":
        return await _answer(knowledge.drafts_view(project_id))
    if action == "confirm_draft":
        return await _answer(knowledge.confirm_draft(project_id, arguments["draft_id"], arguments.get("fields", {}), root))
    return await _answer(knowledge.discard_draft(project_id, arguments["draft_id"]))
