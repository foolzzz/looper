"""Compare two eval runs as a short Markdown table, ready to paste into a PR.

A run has a row per (task, rep), or per (dialog, rep) for the Manager's dialogs. The table has a line per task:
success is ``k/n``, the passes over the scored reps (a pass or a fail; an error or a skipped rep is not scored),
with the errors beside it; every number is the mean over the scored reps. The total row has the passes over
all scored rows with their Wilson 95% interval, the errors by class, and the tasks' means added up (one pass
through the set); against a base, it is over the tasks both runs have, and a second row gives the new run's own
total when it has tasks the base lacks. A cell that changed shows ``base → new``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .runner import ErrorClass, Outcome

SKIPPED = "skipped"  # a row past the spending cap: it never ran

# (column, how to read it from a row); each cell shows "base → new" when the runs differ.
_TASK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("success", "success"),
    ("steps", "steps"),
    ("parallel Workers", "parallel_workers"),
    ("retries", "retries"),
    ("reviews", "reviews"),
    ("gates", "gates_opened"),
    ("room messages", "room_messages"),
    ("contract violations", "violations"),
    ("Planner file edits", "planner_modified_files"),
    ("tokens (prompt / output)", "tokens"),
    ("cost (USD)", "cost_usd"),
)
_DIALOG_COLUMNS: tuple[tuple[str, str], ...] = (("success", "success"), ("cost (USD)", "cost_usd"))
_Z95 = 1.959963984540054  # the standard normal's 97.5% quantile


def outcome(row: Mapping[str, Any]) -> str:
    """``pass``, ``fail``, ``error`` or ``skipped``. A row from before outcomes (schema v1) is read from its typed
    fields: cancelled at the cap or after its timeout is an error, else ``success`` says."""
    if row.get("skipped"):
        return SKIPPED
    if "outcome" in row:
        return str(row["outcome"])
    if row.get("stopped_by_cap") or row.get("timed_out"):
        return Outcome.ERROR.value
    return (Outcome.PASS if row.get("success") else Outcome.FAIL).value


def error_class(row: Mapping[str, Any]) -> str | None:
    """The class of an error row (a v1 row's from ``stopped_by_cap``), else None."""
    if outcome(row) != Outcome.ERROR:
        return None
    if "error_class" in row:
        return str(row["error_class"])
    return (ErrorClass.SPENDING_CAP if row.get("stopped_by_cap") else ErrorClass.TIMEOUT).value


def wilson(passes: int, scored: int) -> tuple[float, float] | None:
    """The Wilson score 95% interval of ``passes`` out of ``scored``; None without any scored."""
    if scored <= 0:
        return None
    p, z2 = passes / scored, _Z95 ** 2
    centre = (p + z2 / (2 * scored)) / (1 + z2 / scored)
    half = _Z95 * math.sqrt(p * (1 - p) / scored + z2 / (4 * scored ** 2)) / (1 + z2 / scored)
    return max(0.0, centre - half), min(1.0, centre + half)


def rows(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The run's rows: its dialogs' for a run of the Manager's dialogs, else its tasks'."""
    return list(run["dialogs"] if "dialogs" in run else run.get("tasks", []))


def _by_id(run: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows(run):
        grouped.setdefault(str(row["id"]), []).append(row)
    return grouped


def reps(run: Mapping[str, Any]) -> int:
    """The most rows any task (or dialog) has in the run: its reps."""
    return max((len(group) for group in _by_id(run).values()), default=0)


def _tally(group: list[Mapping[str, Any]]) -> tuple[int, int, dict[str, int]]:
    """(passes, scored, errors by class) of a task's rows."""
    outcomes = [outcome(row) for row in group]
    errors: dict[str, int] = {}
    for row in group:
        if (found := error_class(row)) is not None:
            errors[found] = errors.get(found, 0) + 1
    scored = sum(1 for o in outcomes if o in (Outcome.PASS, Outcome.FAIL))
    return outcomes.count(Outcome.PASS.value), scored, errors


def _tokens(row: Mapping[str, Any]) -> tuple[int, int] | None:
    """A row's prompt tokens (uncached, read from and written to the cache) and output tokens; None for a row
    from before they were read."""
    counted = row.get("tokens")
    if not isinstance(counted, Mapping):
        return None
    prompt = sum(int(counted.get(k) or 0) for k in ("input", "cache_read", "cache_creation"))
    return prompt, int(counted.get("output") or 0)


def _number(row: Mapping[str, Any], key: str) -> Any:
    if key == "violations":
        return (row.get("contract_violations") or {}).get("total", 0)
    if key == "tokens":
        return _tokens(row)
    return row.get(key)


def _mean(group: list[Mapping[str, Any]], key: str) -> Any:
    """The mean of ``key`` over the scored rows that have it (a pair for tokens); None when none has it."""
    values = [v for row in group if outcome(row) in (Outcome.PASS, Outcome.FAIL)
              if (v := _number(row, key)) is not None]
    if not values:
        return None
    if key == "tokens":
        return tuple(sum(v[i] for v in values) / len(values) for i in (0, 1))
    return sum(float(v) for v in values) / len(values)


def _success(passes: int, scored: int, errors: Mapping[str, int]) -> str:
    text = f"{passes}/{scored}"
    return text + (f" · {sum(errors.values())} err" if errors else "")


def _text(value: Any, key: str) -> str:
    if value is None:
        return "–"
    if key == "cost_usd":
        return f"{value:.4f}"
    if key == "tokens":
        prompt, output = value
        return f"{prompt / 1000:.0f}k / {output / 1000:.1f}k"
    return f"{value:.1f}"


def _cells(group: list[Mapping[str, Any]] | None, columns: tuple[tuple[str, str], ...]) -> list[str] | None:
    """A task's cells, or None when the run does not have it."""
    if group is None:
        return None
    if all(outcome(row) == SKIPPED for row in group):
        return [SKIPPED] * len(columns)
    return [_success(*_tally(group)) if key == "success" else _text(_mean(group, key), key) for _, key in columns]


def _changed(old: list[str] | None, new: list[str]) -> list[str]:
    if old is None:
        return new
    return [n if o == n else f"{o} → **{n}**" for o, n in zip(old, new)]


def _total(groups: dict[str, list[Mapping[str, Any]]], columns: tuple[tuple[str, str], ...]) -> list[str]:
    passes = scored = 0
    errors: dict[str, int] = {}
    for group in groups.values():
        p, s, e = _tally(group)
        passes, scored = passes + p, scored + s
        for name, count in e.items():
            errors[name] = errors.get(name, 0) + count
    cells = []
    for _, key in columns:
        if key == "success":
            text = f"{passes}/{scored}"
            if (interval := wilson(passes, scored)) is not None:
                low, high = interval
                text += f" ({passes / scored:.0%}, {low * 100:.0f}–{high * 100:.0f}%)"
            if errors:
                text += " · " + ", ".join(f"{count} {name}" for name, count in sorted(errors.items()))
            cells.append(text)
            continue
        means = [m for group in groups.values() if (m := _mean(group, key)) is not None]
        if not means:
            cells.append(_text(None, key))
        elif key == "tokens":
            cells.append(_text((sum(m[0] for m in means), sum(m[1] for m in means)), key))
        elif key == "parallel_workers":  # the most at once in any task
            cells.append(_text(max(means), key))
        else:
            cells.append(_text(sum(means), key))
    return cells


def _label(run: Mapping[str, Any]) -> str:
    commit = str((run.get("git") or {}).get("commit") or "")[:9] or "?"
    dirty = "+local changes" if (run.get("git") or {}).get("dirty") else ""
    who = (run.get("manager_agent") or run.get("agent")) if "dialogs" in run else run.get("model")
    count = reps(run)
    return f"{run.get('mode', '?')}/{who or '-'} @ {commit}{dirty} ({count} rep{'' if count == 1 else 's'})"


def _problems(run: Mapping[str, Any]) -> list[str]:
    several = reps(run) > 1
    lines = []
    for row in rows(run):
        name = f"{row['id']} (rep {row.get('rep', 1)})" if several else str(row["id"])
        if (found := error_class(row)) is not None:
            lines.append(f"- {name}: error ({found}): {' '.join(str(row.get('error') or '').split())}")
        elif row.get("problems") and outcome(row) == Outcome.FAIL:
            lines.append(f"- {name}: {' '.join('; '.join(row['problems']).split())}")
    if run.get("stopped_by_cap"):
        lines.append(f"- stopped at the spending cap ({run.get('caps', {}).get('total_cap_usd')} USD)")
    if run.get("aborted"):
        lines.append(f"- the run stopped midway: {run['aborted']}")
    return lines


def _user_gates(group: list[Mapping[str, Any]]) -> str | None:
    """Whether a task's Planner could open user gates in a run (``on``, ``off``); None for a run that did not record
    it (before user gates G2, when an unset setting was off)."""
    values = {str(row["user_gates"]) for row in group if row.get("user_gates")}
    return "/".join(sorted(values)) if values else None


def _gates_notes(new_groups: dict[str, list[Mapping[str, Any]]],
                 base_groups: dict[str, list[Mapping[str, Any]]]) -> list[str]:
    """The tasks whose Planner could open user gates on one side only: their rows compare different contracts."""
    differ, unknown = [], []
    for task_id, group in new_groups.items():
        if task_id not in base_groups:
            continue
        before, after = _user_gates(base_groups[task_id]), _user_gates(group)
        if after is None:
            continue
        if before is None:
            unknown.append(task_id)
        elif before != after:
            differ.append(f"{task_id} ({before} → {after})")
    lines = []
    if differ:
        lines.append("- user gates differ from the base, so these rows compare different contracts: "
                     + ", ".join(differ))
    if unknown:
        lines.append("- the base does not record user gates per task (a run before user gates G2, where an unset "
                     "setting was off): " + ", ".join(unknown))
    return lines


def _notes(new: Mapping[str, Any], base: Mapping[str, Any] | None) -> list[str]:
    """What the table alone does not say: the tasks whose pass rate moved, the tasks whose user gates differ, the
    single samples, the tasks one side lacks."""
    new_groups, base_groups = _by_id(new), _by_id(base) if base is not None else {}
    lines = _gates_notes(new_groups, base_groups) if base is not None and "dialogs" not in new else []
    moved = []
    for task_id, group in new_groups.items():
        if task_id not in base_groups:
            continue
        (bp, bs, _), (np_, ns, _) = _tally(base_groups[task_id]), _tally(group)
        if (bs == 0) != (ns == 0) or bp * ns != np_ * bs:
            moved.append(f"{task_id} {bp}/{bs} → {np_}/{ns}")
    if moved:
        lines.append("- pass rate changed: " + ", ".join(moved))
    once = [side for side, run in (("the base", base), ("the new run", new)) if run is not None and reps(run) == 1]
    if once:
        lines.append(f"- {' and '.join(once)} ran each case once: a pass or fail per case is a single sample")
    if base is not None and (missing := [task_id for task_id in new_groups if task_id not in base_groups]):
        lines.append("- no baseline: " + ", ".join(missing))
    if gone := [task_id for task_id in base_groups if task_id not in new_groups]:
        lines.append("- in the base, not in the new run: " + ", ".join(gone))
    return lines


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def markdown(new: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> str:
    """A row per task (or dialog) and a total row; a cell that changed shows ``base → new``."""
    dialogs = "dialogs" in new
    columns = _DIALOG_COLUMNS if dialogs else _TASK_COLUMNS
    title = "Manager dialogs" if dialogs else "Prompt eval"
    lines = [f"{title}: {_label(new)} (no earlier run to compare with)" if base is None
             else f"{title}: {_label(base)} → {_label(new)}", ""]
    kind = "dialog" if dialogs else "task"
    lines.append(f"| {kind} | " + " | ".join(name for name, _ in columns) + " |")
    lines.append("|---" * (len(columns) + 1) + "|")
    new_groups = _by_id(new)
    base_groups = _by_id(base) if base is not None else {}
    for task_id, group in new_groups.items():
        cells = _changed(_cells(base_groups.get(task_id), columns), _cells(group, columns) or [])
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")
    if base is None:
        lines.append("| **total** | " + " | ".join(_total(new_groups, columns)) + " |")
    else:
        # Both totals over the same tasks, so the row never puts one set of tasks next to another.
        shared = [task_id for task_id in new_groups if task_id in base_groups]
        if shared:  # with none shared there is nothing to compare: the new run's own total and the notes say it
            total = _changed(_total({i: base_groups[i] for i in shared}, columns),
                             _total({i: new_groups[i] for i in shared}, columns))
            label = ("**total**" if len(shared) == len(new_groups) == len(base_groups)
                     else f"**total ({_count(len(shared), 'shared ' + kind)})**")
            lines.append(f"| {label} | " + " | ".join(total) + " |")
        if not shared or len(shared) < len(new_groups):
            lines.append(f"| **total (all {_count(len(new_groups), kind)} of the new run)** | "
                         + " | ".join(_total(new_groups, columns)) + " |")
    if notes := _notes(new, base):
        lines += ["", *notes]
    if problems := _problems(new):
        lines += ["", "Problems in the new run:", *problems]
    return "\n".join(lines)
