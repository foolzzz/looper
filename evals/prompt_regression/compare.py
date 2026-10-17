"""Compare two eval runs as a short Markdown table, ready to paste into a PR."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# (column, how to read it from a task result); each cell shows "base → new" when the runs differ.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("success", "success"),
    ("steps", "steps"),
    ("parallel Workers", "parallel_workers"),
    ("retries", "retries"),
    ("reviews", "reviews"),
    ("gates", "gates_opened"),
    ("contract violations", "violations"),
    ("Planner file edits", "planner_modified_files"),
    ("tokens (prompt / output)", "tokens"),
    ("cost (USD)", "cost_usd"),
)


def _value(task: Mapping[str, Any] | None, key: str) -> Any:
    if task is None:
        return None
    if task.get("skipped"):
        return "skipped"
    if key == "violations":
        return (task.get("contract_violations") or {}).get("total", 0)
    if key == "tokens":
        return _tokens([task])
    return task.get(key)


def _tokens(tasks: list[Mapping[str, Any]]) -> tuple[int, int] | None:
    """The tasks' prompt tokens (uncached, read from and written to the cache) and output tokens; None when no
    task has them (a run from before they were read)."""
    counted = [t["tokens"] for t in tasks if isinstance(t.get("tokens"), Mapping)]
    if not counted:
        return None
    prompt = sum(int(c.get(k) or 0) for c in counted for k in ("input", "cache_read", "cache_creation"))
    return prompt, sum(int(c.get("output") or 0) for c in counted)


def _text(value: Any, key: str) -> str:
    if value is None:
        return "–"
    if value == "skipped":
        return "skipped"
    if key == "success":
        return "pass" if value else "FAIL"
    if key == "cost_usd":
        return f"{float(value):.4f}"
    if key == "tokens":
        prompt, output = value
        return f"{prompt / 1000:.0f}k / {output / 1000:.1f}k"
    return str(value)


def _cell(base: Any, new: Any, key: str) -> str:
    if base is None or _text(base, key) == _text(new, key):
        return _text(new, key)
    return f"{_text(base, key)} → **{_text(new, key)}**"


def totals(run: Mapping[str, Any]) -> dict[str, Any]:
    """The run's sums over the tasks that ran; success is "passed/ran"."""
    ran = [t for t in run.get("tasks", []) if not t.get("skipped")]
    return {
        "success": f"{sum(1 for t in ran if t.get('success'))}/{len(ran)}",
        "steps": sum(int(t.get("steps") or 0) for t in ran),
        # The most at once in any task; none from a run before it was read.
        "parallel_workers": max((int(t["parallel_workers"]) for t in ran if "parallel_workers" in t), default=None),
        "retries": sum(int(t.get("retries") or 0) for t in ran),
        "reviews": sum(int(t.get("reviews") or 0) for t in ran),
        # None from a run before gates were counted.
        "gates_opened": sum(int(t["gates_opened"]) for t in ran if "gates_opened" in t)
        if any("gates_opened" in t for t in ran) else None,
        "violations": sum(int((t.get("contract_violations") or {}).get("total", 0)) for t in ran),
        "planner_modified_files": sum(int(t.get("planner_modified_files") or 0) for t in ran),
        "tokens": _tokens(ran),
        "cost_usd": round(sum(float(t.get("cost_usd") or 0.0) for t in ran), 6),
    }


def _label(run: Mapping[str, Any]) -> str:
    commit = str((run.get("git") or {}).get("commit") or "")[:9] or "?"
    dirty = "+local changes" if (run.get("git") or {}).get("dirty") else ""
    return f"{run.get('mode', '?')}/{run.get('model') or '-'} @ {commit}{dirty}"


def markdown(new: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> str:
    """One row per task and a total row; a cell that changed shows ``base → new``."""
    lines = []
    if base is None:
        lines.append(f"Prompt eval: {_label(new)} (no earlier run to compare with)")
    else:
        lines.append(f"Prompt eval: {_label(base)} → {_label(new)}")
    lines.append("")
    lines.append("| task | " + " | ".join(name for name, _ in _COLUMNS) + " |")
    lines.append("|---" * (len(_COLUMNS) + 1) + "|")
    base_tasks = {t["id"]: t for t in (base or {}).get("tasks", [])}
    for task in new.get("tasks", []):
        old = base_tasks.get(task["id"]) if base is not None else None
        cells = [_cell(_value(old, key) if base is not None else None, _value(task, key), key)
                 for _, key in _COLUMNS]
        lines.append(f"| {task['id']} | " + " | ".join(cells) + " |")
    new_totals = totals(new)
    base_totals = totals(base) if base is not None else None
    cells = []
    for _, key in _COLUMNS:
        value = new_totals[key]
        shown = value if key == "success" else _text(value, key)
        old = None if base_totals is None else (base_totals[key] if key == "success" else _text(base_totals[key], key))
        cells.append(str(shown) if old is None or old == shown else f"{old} → **{shown}**")
    lines.append("| **total** | " + " | ".join(cells) + " |")
    problems = [f"- {t['id']}: {' '.join('; '.join(t.get('problems') or []).split())}" for t in new.get("tasks", [])
                if t.get("problems")]
    if new.get("stopped_by_cap"):
        problems.append(f"- stopped at the spending cap ({new.get('caps', {}).get('total_cap_usd')} USD)")
    if problems:
        lines += ["", "Problems in the new run:", *problems]
    return "\n".join(lines)
