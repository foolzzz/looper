"""What one eval task shows, read back from the server's API.

Pure functions over the API's rows (task detail, activity, Claude run records
and their events) and a working-directory diff, so they can be checked
without a server.

Contract violations are counted where the agent system rejects a reply:

- the Planner: ``PlannerAgent._ask`` logs ``Planner reply broke the <KIND>
  contract (...)`` for a reply its parser rejects and asks again; a repair
  reply that is rejected too raises ``invalid reply after a repair turn``,
  which every caller logs once;
- a Worker or Reviewer: ``worker.judge_result`` fails the step when the final
  message has no JSON report (or verdict) of the contract's shape.
  ``report_violation`` applies the same reading with the same parser.

Whether the Planner modified files (it must leave that to Workers) is read two
ways: its runs' file-editing tool calls, and files of the working directory
that changed while one of its turns ran and no Worker or Reviewer ran.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from loopx.agents.tiers.worker import extract_report

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})
FILE_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_BROKE_CONTRACT = re.compile(r"^Planner reply broke the (PLAN|DECISION|CHAT|SUPERVISE) contract \(")
_REPAIR_REJECTED = "invalid reply after a repair turn"
_TURN_KIND = re.compile(r"^\[(PLAN|DECISION|CHAT|SUPERVISE)\]( Your last reply broke the reply contract)?")


def planner_violations(activity: Iterable[Mapping[str, Any]]) -> int:
    """Planner replies the reply-contract parser rejected: first tries and rejected repair turns."""
    count = 0
    for row in activity:
        if row.get("kind") != "error":
            continue
        summary = str(row.get("summary") or "")
        if _BROKE_CONTRACT.match(summary) or _REPAIR_REJECTED in summary:
            count += 1
    return count


def report_violation(text: str, *, is_verify: bool) -> bool:
    """Whether a Worker's (or a Reviewer's) final message breaks its report (verdict) contract, read as
    ``worker.judge_result`` reads it."""
    if is_verify:
        verdict = extract_report(text, "passed")
        return verdict is None or not isinstance(verdict.get("passed"), bool)
    report = extract_report(text, "status")
    return (
        report is None
        or report.get("status") not in ("completed", "failed")
        or not isinstance(report.get("changed_files", {}), dict)
        or not isinstance(report.get("deleted_files", []), list)
    )


def role(record: Mapping[str, Any]) -> str:
    """``planner``, ``worker``, ``reviewer`` or ``other``, from the run's agent id."""
    agent_id = str(record.get("agent_id") or "")
    for name in ("planner", "worker", "reviewer"):
        if agent_id.startswith(f"{name}-"):
            return name
    return "other"


def result_text(events: Iterable[Mapping[str, Any]]) -> str | None:
    """The final message of a run that ended with a successful ``result``; None otherwise."""
    result = None
    for row in events:
        event = row.get("event", row)
        if isinstance(event, Mapping) and event.get("type") == "result":
            result = event
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        return None
    return str(result.get("result") or "")


def turn_kinds(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """The Planner's turns by kind (``plan``, ``decision``, ``chat``, ``supervise``, ``repair``)."""
    kinds: dict[str, int] = {}
    for record in records:
        if role(record) != "planner":
            continue
        match = _TURN_KIND.match(str(record.get("input_text") or ""))
        kind = "other" if match is None else ("repair" if match.group(2) else match.group(1).lower())
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def retries(records: Iterable[Mapping[str, Any]]) -> int:
    """Worker runs beyond the first of each step."""
    runs: dict[str, int] = {}
    for record in records:
        if role(record) == "worker":
            step = str((record.get("metadata") or {}).get("step_id") or record.get("agent_id"))
            runs[step] = runs.get(step, 0) + 1
    return sum(n - 1 for n in runs.values())


def _relative(path: str, work_dir: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(work_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return f"<outside the working directory>/{candidate.name}"


def edit_tool_paths(events: Iterable[Mapping[str, Any]], work_dir: Path) -> list[str]:
    """The files a run's file-editing tool calls named, relative to ``work_dir`` (only a name outside it)."""
    paths = []
    for row in events:
        event = row.get("event", row)
        if not isinstance(event, Mapping) or event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, Mapping) and block.get("type") == "tool_use" and block.get("name") in FILE_EDIT_TOOLS:
                tool_input = block.get("input") or {}
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if path:
                    paths.append(_relative(str(path), work_dir))
    return paths


def timestamp(value: Any) -> float | None:
    """Seconds since the epoch from an API timestamp (ISO 8601); None when absent."""
    if not value:
        return None
    return datetime.fromisoformat(str(value)).timestamp()


def run_windows(records: Iterable[Mapping[str, Any]], *, now: float) -> dict[str, list[tuple[float, float]]]:
    """Each role's runs as (start, end) in seconds since the epoch, as the database clock gave them."""
    windows: dict[str, list[tuple[float, float]]] = {}
    for record in records:
        start = timestamp(record.get("created_at"))
        if start is None:
            continue
        end = timestamp(record.get("finished_at")) or now
        windows.setdefault(role(record), []).append((start, end))
    return windows


def planner_changed_files(
    changes: Mapping[str, float],
    windows: Mapping[str, list[tuple[float, float]]],
    *,
    clock_offset: float = 0.0,
    tolerance: float = 0.1,
) -> list[str]:
    """Changed files (path to mtime, local clock) last written while a Planner turn ran and no Worker or
    Reviewer ran.

    ``clock_offset`` is the database clock minus the local clock, measured when
    the run starts; a file written within ``tolerance`` seconds of a step run
    (what that measurement may be off by) is left to the step.
    """
    def inside(moment: float, spans: Iterable[tuple[float, float]], slack: float) -> bool:
        return any(start - clock_offset - slack <= moment <= end - clock_offset + slack for start, end in spans)

    steps = [*windows.get("worker", []), *windows.get("reviewer", [])]
    return sorted(
        path for path, mtime in changes.items()
        if inside(mtime, windows.get("planner", []), 0.0) and not inside(mtime, steps, tolerance)
    )


def flow_problems(expect: Mapping[str, int], observed: Mapping[str, int]) -> list[str]:
    """The expected flow counts (``min_<metric>``) the task fell short of."""
    problems = []
    for key, minimum in expect.items():
        metric = key.removeprefix("min_")
        if observed.get(metric, 0) < minimum:
            problems.append(f"{metric} {observed.get(metric, 0)} < {minimum}")
    return problems


def task_metrics(
    *,
    detail: Mapping[str, Any],
    activity: list[Mapping[str, Any]],
    records: list[Mapping[str, Any]],
    events: Mapping[int, list[Mapping[str, Any]]],
    changes: Mapping[str, float],
    work_dir: Path,
    check_problem: str | None,
    expect: Mapping[str, int],
    clock_offset: float = 0.0,
    now: float,
) -> dict[str, Any]:
    """One task's result: success and why not, the flow counts, cost, contract violations and the Planner's
    file changes."""
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_role.setdefault(role(record), []).append(record)

    worker_violations = reviewer_violations = 0
    for name in ("worker", "reviewer"):
        for record in by_role.get(name, []):
            text = result_text(events.get(int(record["id"]), []))
            if text is not None and report_violation(text, is_verify=name == "reviewer"):
                if name == "worker":
                    worker_violations += 1
                else:
                    reviewer_violations += 1
    planner_edits = sorted({
        path for record in by_role.get("planner", []) for path in edit_tool_paths(events.get(int(record["id"]), []),
                                                                                  work_dir)
    })
    changed_by_planner = planner_changed_files(changes, run_windows(records, now=now), clock_offset=clock_offset)

    kinds = turn_kinds(records)
    observed = {
        "steps": len(detail.get("steps") or []),
        "reviews": len(by_role.get("reviewer", [])),
        "retries": retries(records),
        "chat_turns": kinds.get("chat", 0),
    }
    status = str(detail.get("status") or "")
    problems = []
    if status != "completed":
        problems.append(f"task {status or 'unknown'}" + (f": {str(detail['error'])[:200]}" if detail.get("error")
                                                       else ""))
    if check_problem:
        problems.append(check_problem)
    problems += flow_problems(expect, observed)

    cost = detail.get("cost") or {}
    violations = {"planner": planner_violations(activity), "worker": worker_violations,
                  "reviewer": reviewer_violations}
    return {
        "status": status,
        "success": not problems,
        "problems": problems,
        **observed,
        "planner_turns": kinds,
        "cost_usd": round(float(cost.get("cost_usd") or 0.0), 6),
        "cost_by_role": {name: round(float((value or {}).get("cost_usd") or 0.0), 6)
                         for name, value in (cost.get("by_role") or {}).items()},
        "runs": int(cost.get("runs") or len(records)),
        "contract_violations": {**violations, "total": sum(violations.values())},
        "planner_edit_tool_paths": planner_edits,
        "planner_changed_files": changed_by_planner,
        "planner_modified_files": len(set(planner_edits) | set(changed_by_planner)),
    }
