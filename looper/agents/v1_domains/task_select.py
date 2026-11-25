"""Operator-only read: resolve a selection to one task, candidates, or nothing. No model call."""

from __future__ import annotations

import json
import os
from typing import Any

from ...db.connection import get_pool
from ...db.tasks import TaskGroup, task_group
from .. import task_select as pure
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _INT, _NULL_STR, _STR, _array, _object
from ..ops import ErrorCode, OpError

SUMMARY = "Resolve a selection to one task, a candidate list, or nothing."
DESCRIPTION = (
    SUMMARY + " An empty selection uses the working directory or the recent tasks; a task id, one merge "
    "request, or text whose every word is in the title or the latest requirement returns that task, at most "
    "ten candidates, or nothing, and never calls a model."
)

_ROW = _object({
    "task_id": _STR, "title": _NULL_STR, "project_name": _NULL_STR, "status": _STR, "group": _STR,
    "last_activity_at": _NULL_STR,
}, closed=True)
_SEARCHED = _object({
    "fields": _array(_STR), "window_days": {"type": ["integer", "null"]}, "all": _BOOL,
}, closed=True)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """An object that may be null. A ``type: null`` branch would not be a concrete result schema."""
    return {**schema, "type": [schema["type"], "null"]}


_RESULT = _object({
    "kind": {"type": "string", "enum": ["one", "candidates", "none"]},
    "task": _nullable(_ROW),
    "candidates": _array(_ROW),
    "more": _INT,
    "searched": _nullable(_SEARCHED),
    "hint": _NULL_STR,
}, closed=True)

RESULTS: dict = {}
# Reads only Looper's own task rows. Not a generated client command: the task CLI is a later change.
OPERATIONS = (
    Operation("tasks.select", "POST", "/api/v1/tasks/select", SUMMARY, DESCRIPTION,
              _object({"selection": _STR, "cwd": _STR, "all": _BOOL}, [], closed=True), _RESULT,
              risk="read", operator_only=True, cli=False, level=ActionLevel.INSIDE),
)

_HINT_TEXT = "Nothing matched. Search every task with all, or give the task id."
_HINT_ID = "No task has this id."
_HINT_RECENT = "No task had activity in the last 14 days."
_ENDED = {TaskGroup.COMPLETED, TaskGroup.CANCELLED, TaskGroup.ATTENTION}
# Literal substring, as tasks._SEARCH does: '%' and '_' in the selection stay characters, never LIKE wildcards.
_FOLD = "lower(regexp_replace(coalesce({column}, ''), '\\s+', ' ', 'g'))"
_FROM = """
SELECT t.id, t.title, t.requirement, t.original_user_text, t.status, t.work_dir, t.workspace_dir, t.updated_at,
       p.name AS project_name, act.at AS last_activity_at, wt.path AS worktree_path
FROM tasks t
LEFT JOIN projects p ON p.id = t.project_id
LEFT JOIN LATERAL (
    SELECT max(a.created_at) AS at FROM activity a WHERE a.task_id = t.id
) act ON true
LEFT JOIN task_worktrees wt ON wt.task_id = t.id AND wt.state = 'active'
"""


def _has(column: str, placeholder: str) -> str:
    return f"strpos({_FOLD.format(column=column)}, {placeholder}) > 0"


def _ended(status: str) -> bool:
    return task_group(status) in _ENDED


def _when(row) -> Any:
    # Any activity row wins, even when tasks.updated_at is later.
    return row["last_activity_at"] or row["updated_at"]


def _candidate(row, tier: int) -> pure.Candidate:
    return pure.Candidate(row["id"], tier, _ended(row["status"]), _when(row).timestamp())


def _view(row) -> dict[str, Any]:
    return {
        "task_id": row["id"], "title": row["title"], "project_name": row["project_name"],
        "status": row["status"], "group": task_group(row["status"]).value,
        "last_activity_at": _when(row).isoformat(),
    }


def _result(kind: str, task, candidates, more: int, searched, hint) -> dict[str, Any]:
    return {"kind": kind, "task": task, "candidates": candidates, "more": more,
            "searched": searched, "hint": hint}


def _searched(fields: list[str], see_all: bool, window: int | None) -> dict[str, Any]:
    return {"fields": fields, "window_days": None if see_all else window, "all": see_all}


def _pack(rows, presented, searched, hint) -> dict[str, Any]:
    kind, chosen, more = presented
    by_id = {row["id"]: row for row in rows}
    views = [_view(by_id[item.task_id]) for item in chosen]
    if kind == "one":
        return _result("one", views[0], [], 0, None, None)
    if kind == "candidates":
        return _result("candidates", None, views, more, None, None)
    return _result("none", None, [], 0, searched, hint)


async def _fetch(pool, where: str, args: list) -> list:
    return list(await pool.fetch(_FROM + " WHERE " + where, *args))


def _directories(row) -> set[str]:
    found = set()
    for raw in (row["work_dir"], row["workspace_dir"], row["worktree_path"]):
        if isinstance(raw, str) and os.path.isabs(raw):
            found.add(os.path.normpath(os.path.realpath(raw)))
    return found


def _window(see_all: bool, days: int) -> str:
    return f"($1::bool OR COALESCE(act.at, t.updated_at) >= now() - interval '{days} days')"


def _object_of(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _check_ref(spec: Any) -> pure.MrRef | None:
    check = _object_of(spec).get("check")
    if not isinstance(check, dict) or check.get("type") != "github_pr":
        return None
    number = check.get("number")
    if isinstance(number, str) and number.isdecimal():
        number = int(number)
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    repo = check.get("repo")
    return pure.MrRef(number, repo if isinstance(repo, str) and repo.strip() else None)


async def _by_directory(pool, cwd: str, see_all: bool) -> dict[str, Any]:
    if cwd:
        rows = await _fetch(pool, "TRUE", [])
        listed = [(row, _directories(row)) for row in rows]
        deepest = pure.deepest_paths(os.path.realpath(cwd), (path for _, dirs in listed for path in dirs))
        if deepest:
            chosen = [row for row, dirs in listed if dirs & deepest]
            # Several in the deepest directory: the one that has not ended, when it is the only such task.
            ranked = pure.prefer_single_open([_candidate(row, 0) for row in chosen])
            return _pack(rows, pure.present(pure.rank(ranked), limit=pure.CANDIDATE_LIMIT, single=True), None, None)
    rows = await _fetch(pool, f"COALESCE(act.at, t.updated_at) >= now() - interval '{pure.RECENT_DAYS} days'", [])
    # Newest activity, then task id. Not ended-before-open, and never an exact match, even for one row.
    ordered = sorted((_candidate(row, 0) for row in rows), key=lambda item: (-item.activity, item.task_id))
    # `all` does not widen this list, so the window stays 14 days.
    searched = {"fields": ["last_activity_at"], "window_days": pure.RECENT_DAYS, "all": see_all}
    return _pack(rows, pure.present(ordered, limit=pure.RECENT_LIMIT, single=False), searched, _HINT_RECENT)


async def _by_id(pool, task_id: str, see_all: bool) -> dict[str, Any]:
    rows = await _fetch(pool, "t.id = $1", [task_id])
    ranked = [_candidate(row, 0) for row in rows]
    return _pack(rows, pure.present(ranked, limit=1, single=True),
                 _searched(["task_id"], see_all, None), _HINT_ID)


async def _references(pool, rows) -> dict[str, set[pure.MrRef]]:
    ids = [row["id"] for row in rows]
    notes: dict[str, list[str]] = {task_id: [] for task_id in ids}
    checks: dict[str, list] = {task_id: [] for task_id in ids}
    if ids:
        for item in await pool.fetch("SELECT task_id, spec FROM task_waits WHERE task_id = ANY($1::text[])", ids):
            checks.setdefault(item["task_id"], []).append(item["spec"])
        for item in await pool.fetch(
                "SELECT task_id, summary FROM task_journal "
                "WHERE kind = 'supplement' AND task_id = ANY($1::text[])", ids):
            notes.setdefault(item["task_id"], []).append(item["summary"])
    found: dict[str, set[pure.MrRef]] = {}
    for row in rows:
        refs = {ref for spec in checks.get(row["id"], ()) if (ref := _check_ref(spec)) is not None}
        for text in (row["title"], row["requirement"], row["original_user_text"], *notes.get(row["id"], ())):
            if isinstance(text, str) and text:
                refs |= pure.text_refs(text)
        found[row["id"]] = refs
    return found


async def _by_mr(pool, ref: pure.MrRef, see_all: bool) -> dict[str, Any]:
    number = str(ref.number)
    text = " OR ".join(_has(column, "$2") for column in ("t.title", "t.requirement", "t.original_user_text"))
    supplement = ("EXISTS (SELECT 1 FROM task_journal j WHERE j.task_id = t.id AND j.kind = 'supplement' AND "
                  + _has("j.summary", "$2") + ")")
    check = ("EXISTS (SELECT 1 FROM task_waits w WHERE w.task_id = t.id "
             "AND w.spec #>> '{check,type}' = 'github_pr' AND w.spec #>> '{check,number}' = $2)")
    where = f"{_window(see_all, pure.WINDOW_DAYS)} AND ({check} OR {text} OR {supplement})"
    rows = await _fetch(pool, where, [see_all, number])
    refs = await _references(pool, rows)
    matched = []
    for row in rows:
        tier = pure.mr_match(ref, refs.get(row["id"], ()))
        if tier == "repo":
            matched.append(_candidate(row, 0))
        elif tier == "number":
            matched.append(_candidate(row, 1))
    fields = ["github_pr", "title", "requirement", "original_user_text", "supplements"]
    return _pack(rows, pure.present(pure.rank(matched), limit=pure.CANDIDATE_LIMIT, single=True),
                 _searched(fields, see_all, pure.WINDOW_DAYS), _HINT_TEXT)


async def _by_text(pool, parts: list[str], see_all: bool) -> dict[str, Any]:
    args: list = [see_all]
    clauses = []
    for part in parts:
        args.append(part)
        placeholder = f"${len(args)}"
        clauses.append(f"({_has('t.title', placeholder)} OR {_has('t.requirement', placeholder)})")
    rows = await _fetch(pool, f"{_window(see_all, pure.WINDOW_DAYS)} AND {' AND '.join(clauses)}", args)
    matched = []
    for row in rows:
        tier = pure.text_match(row["title"], row["requirement"], parts)
        if tier == "title":
            matched.append(_candidate(row, 0))
        elif tier == "requirement":
            matched.append(_candidate(row, 1))
    return _pack(rows, pure.present(pure.rank(matched), limit=pure.CANDIDATE_LIMIT, single=True),
                 _searched(["title", "requirement"], see_all, pure.WINDOW_DAYS), _HINT_TEXT)


async def resolve(arguments: dict, pool) -> dict[str, Any]:
    selection = arguments.get("selection") or ""
    cwd = arguments.get("cwd") or ""
    see_all = bool(arguments.get("all"))
    if not isinstance(selection, str) or not isinstance(cwd, str):
        raise OpError(ErrorCode.INVALID, "selection and cwd must be strings")
    if cwd and not os.path.isabs(cwd):
        raise OpError(ErrorCode.INVALID, "cwd must be an absolute path")
    # Normalize once. A second pass would strip another quote pair.
    folded = pure.normalize(selection)
    if not folded:
        return await _by_directory(pool, cwd, see_all)
    if pure.TASK_ID.fullmatch(folded):
        return await _by_id(pool, folded, see_all)
    ref = pure.parse_mr(selection)
    if ref is not None:
        return await _by_mr(pool, ref, see_all)
    return await _by_text(pool, pure.tokens(selection), see_all)


async def run(server, operation, arguments, principal, headers):
    return await resolve(arguments, get_pool())
