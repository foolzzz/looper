"""Operator-only read: resolve a selection to one task, candidates, or nothing. No model call."""

from __future__ import annotations

import json
import os
import unicodedata
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
_MR_FIELDS = ["github_pr", "title", "requirement", "original_user_text", "supplements"]
# NFKC then lower, the same two steps as normalize(). Whitespace and one surrounding quote pair stay
# wider than Python: a search token has no space, and stripping quotes only removes a prefix and a suffix.
# The raw strpos keeps a row when the server's normalize/lower is narrower. Python still accepts or rejects it.
# '%' and '_' stay characters: this is strpos, never a LIKE pattern.
_FOLD = "lower(regexp_replace(normalize(coalesce({column}, ''), NFKC), '\\s+', ' ', 'g'))"
# The newest activity row. (task_id, seq) serves ORDER BY seq DESC LIMIT 1; max(created_at) cannot.
_ACTIVITY = """
LEFT JOIN LATERAL (
    SELECT a.created_at AS at
    FROM activity a
    WHERE a.task_id = t.id
    ORDER BY a.seq DESC
    LIMIT 1
) act ON true
"""
_COLUMNS = """
t.id, t.title, t.requirement, t.original_user_text, t.status, t.updated_at,
p.name AS project_name, act.at AS last_activity_at
"""


def _fold(column: str) -> str:
    return _FOLD.format(column=column)


def _digit_maps() -> tuple[str, str]:
    """Every Unicode decimal digit, mapped to ASCII. The same set ``str.isdecimal`` accepts."""
    grouped: list[list[str]] = [[] for _ in range(10)]
    for code in range(0x110000):
        char = chr(code)
        if char.isdecimal():
            grouped[unicodedata.decimal(char)].append(char)
    source = [char for value in range(10) for char in grouped[value]]
    target = [str(value) for value in range(10) for _ in grouped[value]]
    return "".join(source), "".join(target)


_DIGIT_FROM, _DIGIT_TO = _digit_maps()


def _decimal_key(expr: str) -> str:
    """The integer text of a Unicode decimal string, or NULL.

    ``int`` accepts ``１２``, ``００１２`` and ``١٢`` as 12. NFKC string equality does not:
    it keeps ``0012`` and leaves Arabic-Indic digits unchanged. Leading zeros are removed
    so both compare with the bound number. Anything that is not all decimal digits stays
    NULL and Python never sees it as a number.
    """
    mapped = f"translate({expr}, $digits${_DIGIT_FROM}$digits$, $digits${_DIGIT_TO}$digits$)"
    return (f"CASE WHEN {mapped} ~ '^[0-9]+$' "
            f"THEN COALESCE(NULLIF(ltrim({mapped}, '0'), ''), '0') END")


def _has(column: str, placeholder: str) -> str:
    """True when the folded text or the raw text contains the bound literal."""
    return (f"(strpos({_fold(column)}, {placeholder}) > 0 "
            f"OR strpos(coalesce({column}, ''), {placeholder}) > 0)")


def _interval(days: int) -> str:
    return f"interval '{int(days)} days'"


def _window_ids(days: int) -> str:
    """Tasks that can be inside the window: a recent update, or any recent activity row.

    A row whose newest activity is older is dropped after the seq probe. The indexes are
    (updated_at) and (created_at, task_id); neither is asked to compute max(created_at).
    """
    limit = _interval(days)
    return (f"SELECT id FROM tasks WHERE updated_at >= now() - {limit} "
            f"UNION SELECT task_id FROM activity WHERE created_at >= now() - {limit}")


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


def _ancestors(cwd: str) -> list[str]:
    """Normpath ancestors of the real working directory, the directory itself first.

    ``realpath`` runs once, on the query. Stored paths were resolved when they were written.
    """
    current = os.path.normpath(os.path.realpath(cwd))
    found = []
    while True:
        found.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return found
        current = parent


def _search_sql(match: str, see_all: bool, days: int) -> str:
    """Windowed candidates, then the literal prefilter, then one activity row per survivor.

    ``all`` reads every task: the caller asked for that. Otherwise the window indexes produce the ids,
    and each id is one primary-key lookup. The prefilter is a superset of the Python match.
    """
    if see_all:
        return f"""
            SELECT {_COLUMNS}
            FROM tasks t
            LEFT JOIN projects p ON p.id = t.project_id
            {_ACTIVITY}
            WHERE {match}
        """
    window = f"COALESCE(act.at, t.updated_at) >= now() - {_interval(days)}"
    return f"""
        WITH narrowed AS MATERIALIZED (
            {_window_ids(days)}
        )
        SELECT {_COLUMNS}
        FROM narrowed n
        JOIN LATERAL (
            SELECT t.id, t.title, t.requirement, t.original_user_text, t.status, t.updated_at, t.project_id
            FROM tasks t
            WHERE t.id = n.id AND ({match})
            -- Keeps the primary-key lookup outside the text filter. Without it the planner
            -- applies the filter to every task, then keeps the window.
            OFFSET 0
        ) t ON true
        LEFT JOIN projects p ON p.id = t.project_id
        {_ACTIVITY}
        WHERE {window}
    """


async def _rows(pool, sql: str, args: list) -> list:
    return list(await pool.fetch(sql, *args))


async def _rows_by_ids(pool, task_ids: list[str]) -> list:
    if not task_ids:
        return []
    return await _rows(pool, f"""
        SELECT {_COLUMNS}
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        {_ACTIVITY}
        WHERE t.id = ANY($1::text[])
    """, [task_ids])


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


# Equality on the stored real path. The partial indexes require the ``<> ''`` predicate.
_DIRECTORY_HITS = """
SELECT id, path FROM (
    SELECT id, work_dir_real AS path FROM tasks
    WHERE work_dir_real <> '' AND work_dir_real = ANY($1::text[])
    UNION ALL
    SELECT id, workspace_dir_real FROM tasks
    WHERE workspace_dir_real <> '' AND workspace_dir_real = ANY($1::text[])
    UNION ALL
    SELECT task_id, path_real FROM task_worktrees
    WHERE state = 'active' AND path_real <> '' AND path_real = ANY($1::text[])
) hit
"""


async def _directory_pairs(pool, ancestors: list[str]) -> list[tuple[str, str]]:
    """Tasks whose stored real work_dir, workspace_dir or active worktree contains the directory."""
    known = set(ancestors)
    return [(row["id"], row["path"]) for row in await pool.fetch(_DIRECTORY_HITS, ancestors)
            if row["path"] in known]


async def _recent(pool, see_all: bool) -> dict[str, Any]:
    """At most 20 tasks with activity in the last 14 days. ``all`` does not widen the window.

    ``count(*) OVER()`` is the full window, so ``more`` stays exact while only 20 rows are read back.
    """
    limit = _interval(pure.RECENT_DAYS)
    rows = await _rows(pool, f"""
        WITH narrowed AS MATERIALIZED (
            {_window_ids(pure.RECENT_DAYS)}
        )
        SELECT {_COLUMNS}, count(*) OVER() AS total
        FROM narrowed n
        JOIN LATERAL (
            SELECT id, title, requirement, original_user_text, status, updated_at, project_id
            FROM tasks
            WHERE id = n.id
            -- Same fence as the text lookup: one primary-key read per window id.
            OFFSET 0
        ) t ON true
        LEFT JOIN projects p ON p.id = t.project_id
        {_ACTIVITY}
        WHERE COALESCE(act.at, t.updated_at) >= now() - {limit}
        ORDER BY COALESCE(act.at, t.updated_at) DESC, t.id
        LIMIT {pure.RECENT_LIMIT}
    """, [])
    total = int(rows[0]["total"]) if rows else 0
    ordered = sorted((_candidate(row, 0) for row in rows), key=lambda item: (-item.activity, item.task_id))
    kind, chosen, _shown = pure.present(ordered, limit=pure.RECENT_LIMIT, single=False)
    more = 0 if kind == "none" else total - len(chosen)
    searched = {"fields": ["last_activity_at"], "window_days": pure.RECENT_DAYS, "all": see_all}
    return _pack(rows, (kind, chosen, more), searched, _HINT_RECENT)


async def _by_directory(pool, cwd: str, see_all: bool) -> dict[str, Any]:
    if cwd:
        ancestors = _ancestors(cwd)
        pairs = await _directory_pairs(pool, ancestors)
        deepest = pure.deepest_paths(ancestors[0], (path for _task_id, path in pairs))
        if deepest:
            # Several in the deepest directory: the one that has not ended, when it is the only such task.
            ids = list(dict.fromkeys(task_id for task_id, path in pairs if path in deepest))
            rows = await _rows_by_ids(pool, ids)
            ranked = pure.prefer_single_open([_candidate(row, 0) for row in rows])
            return _pack(rows, pure.present(pure.rank(ranked), limit=pure.CANDIDATE_LIMIT, single=True), None, None)
    return await _recent(pool, see_all)


async def _by_id(pool, task_id: str, see_all: bool) -> dict[str, Any]:
    rows = await _rows_by_ids(pool, [task_id])
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


def _mr_match_sql() -> str:
    number = "$1"
    text = " OR ".join(_has(column, number) for column in ("t.title", "t.requirement", "t.original_user_text"))
    supplement = ("EXISTS (SELECT 1 FROM task_journal j WHERE j.task_id = t.id AND j.kind = 'supplement' AND "
                  + _has("j.summary", number) + ")")
    # int() on a decimal string, not NFKC text. ``００１２`` and ``١٢`` are both 12.
    stored_number = _decimal_key("w.spec #>> '{check,number}'")
    check = ("EXISTS (SELECT 1 FROM task_waits w WHERE w.task_id = t.id "
             "AND w.spec #>> '{check,type}' = 'github_pr' AND "
             f"{stored_number} = {number})")
    return f"({check} OR {text} OR {supplement})"


async def _by_mr(pool, ref: pure.MrRef, see_all: bool) -> dict[str, Any]:
    rows = await _rows(pool, _search_sql(_mr_match_sql(), see_all, pure.WINDOW_DAYS), [str(ref.number)])
    refs = await _references(pool, rows)
    matched = []
    for row in rows:
        tier = pure.mr_match(ref, refs.get(row["id"], ()))
        if tier == "repo":
            matched.append(_candidate(row, 0))
        elif tier == "number":
            matched.append(_candidate(row, 1))
    return _pack(rows, pure.present(pure.rank(matched), limit=pure.CANDIDATE_LIMIT, single=True),
                 _searched(_MR_FIELDS, see_all, pure.WINDOW_DAYS), _HINT_TEXT)


async def _by_text(pool, parts: list[str], see_all: bool) -> dict[str, Any]:
    clauses = []
    for index, _part in enumerate(parts, start=1):
        placeholder = f"${index}"
        clauses.append(f"({_has('t.title', placeholder)} OR {_has('t.requirement', placeholder)})")
    rows = await _rows(pool, _search_sql(" AND ".join(clauses), see_all, pure.WINDOW_DAYS), list(parts))
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
