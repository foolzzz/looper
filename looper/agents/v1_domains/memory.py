"""Operator-only memory candidate reads, the memory_write setting and its preflight check (memory-write-path RFC,
section 9.3). The writes that move a candidate come with the write path."""

from ...db import memory as db_memory
from .. import memory, memory_model as model, memory_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _OBJ, _NULL_OBJ
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_STRINGS = _array(_STR)
_NULL_INT = {"type": ["integer", "null"]}
_CANDIDATE_FIELDS = {
    **dict.fromkeys(("id", "kind", "state", "destination", "source_kind", "source_class", "statement", "why",
                     "created_at", "updated_at"), _STR),
    **dict.fromkeys(("reason", "title", "note_path", "content_hash", "project_id", "manager_id", "commit",
                     "merged_into", "supersedes", "expires_at", "valid_until", "written_at"), _NULL_STR),
    "keywords": _STRINGS, "revision": _INT, "sources_total": _INT,
    **dict.fromkeys(("untrusted", "flagged", "hard_rule", "purged"), _BOOL),
}
CANDIDATE = _object(_CANDIDATE_FIELDS, closed=True)
_SOURCE = _object({"kind": _STR, "source_class": _STR, **dict.fromkeys(
    ("quote", "channel", "chat", "message_id", "said_at", "task_id", "lead"), _NULL_STR)}, closed=True)
_REVISION = _object({"revision": _INT, "made_by": _STR, "title": _STR, "changes": _array(_OBJ), "diff_stats": _OBJ,
                     "large_removal": _BOOL, "content_hash": _STR, "created_at": _STR}, closed=True)
_EVENT = _object({"action": _STR, "actor": _STR, "via": _STR, "revision": _NULL_INT, "content_hash": _NULL_STR,
                  "note": _NULL_STR, "at": _STR}, closed=True)
DETAIL = _object({**_CANDIDATE_FIELDS, "current": {"anyOf": [_REVISION, {"type": "null"}]},
                  "sources": _array(_SOURCE), "events": _array(_EVENT)}, closed=True)
_VIEW = _object({"configured": _BOOL, "enabled": _BOOL, "settings": _NULL_OBJ, "problem": _NULL_STR}, closed=True)
_CHECK = _object({"configured": _BOOL, "enabled": _BOOL, "ok": _BOOL,
                  "checks": _array(_object({"name": _STR, "ok": _BOOL, "detail": _STR}, closed=True))}, closed=True)
_ID = {"type": "string", "pattern": r"^[Mm]-\d{1,18}$"}
_TARGET = _object({"shape": {"type": "string", "enum": [shape.value for shape in model.Shape]},
                   "folders": _STRINGS, "index": _NULL_STR, "files": _STRINGS}, ["shape"], closed=True)
_SOURCES = _object({"manager": _BOOL, "tasks": _BOOL, "leads": _BOOL}, [], closed=True)
_NUMBER = {"type": "number"}
_SETTINGS_PUT = _object({
    "root": _NULL_STR, "enabled": _BOOL, "rules_files": _STRINGS,
    "targets": {"type": "object", "additionalProperties": _TARGET,
                "propertyNames": {"enum": [kind.value for kind in model.Kind]}},
    "required_frontmatter": _STRINGS, "sources": _SOURCES, "branch": _NULL_STR, "worktree_root": _NULL_STR,
    "commit_prefix": _NULL_STR, "push_channel": _NULL_STR, "pending_days": _INT, "draft_budget_usd": _NUMBER,
    "daily_budget_usd": _NUMBER}, [], closed=True)  # root is required, but {"root": null} clears: the model checks


def _op(action, summary, details, request, result, *, risk="read"):
    return Operation(f"memory.{action}", "POST", f"/api/v1/memory/{action}", summary, f"{summary} {details}",
                     request, result, risk=risk, operator_only=True,
                     level=ActionLevel.INSIDE)  # reads candidates and checks or stores settings; writes no vault file


RESULTS = {}
OPERATIONS = (
    _op("list", "List memory candidates, newest first.",
        "The state filter is open (the default: not written, rejected or dropped), all, or one state; before "
        "continues from a candidate id.",
        _object({"state": {"type": "string", "enum": list(memory.LIST_FILTERS)},
                 "kind": {"type": "string", "enum": [kind.value for kind in model.Kind]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": db_memory.LIST_MAX},
                 "before": _ID}, [], closed=True),
        _object({"candidates": _array(CANDIDATE), "more": _BOOL}, closed=True)),
    _op("get", "Read one memory candidate.",
        "With its current revision's changes, its sources and its latest events.",
        _object({"candidate": _ID}, ["candidate"], closed=True), _object({"candidate": DETAIL}, closed=True)),
    _op("settings_get", "Read the memory_write setting and any problem.",
        "The setting is checked against the knowledge vault each time it is read.",
        _object({}, closed=True), _VIEW),
    _op("settings_put", "Replace the memory_write setting; a null root alone clears it.",
        "Checked against the knowledge vault, git and the channels; the branch is the main checkout's now.",
        _SETTINGS_PUT, _VIEW, risk="write"),
    _op("check", "Check what a memory write needs, without changing anything.",
        "The setting, the vault's repository and branch, no operation under way, no ignored memory path, the "
        "worktree folder and the push channel.",
        _object({}, closed=True), _CHECK),
)


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    try:
        if action == "list":
            result = await memory.list_view(state=arguments.get("state", "open"), kind=arguments.get("kind"),
                                            limit=arguments.get("limit", 50), before=arguments.get("before"))
        elif action == "get":
            found = await memory.get_view(arguments["candidate"])
            if found is None:
                raise OpError(ErrorCode.NOT_FOUND, f"no memory candidate {arguments['candidate']}")
            result = {"candidate": found}
        elif action == "settings_get":
            result = await memory_settings.settings_view()
        elif action == "settings_put":
            result = await memory_settings.update_settings(arguments)
        else:
            result = await memory_settings.check(runtime_root=getattr(server, "_runtime_root", None))
        return _plain(result)
    except memory_settings.SettingsError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
    except ValueError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
