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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from looper.agents.runtimes.omp import EDIT_TOOLS as OMP_EDIT_TOOLS
from looper.agents.runtimes.omp import edited_paths as omp_edited_paths
from looper.agents.tiers.planner_protocol import FINAL_HEADINGS
from looper.agents.tiers.worker import extract_report

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})
# The status a run's record ends with when the run failed (``llm_records.status``: running, completed or failed).
RECORD_FAILED = "failed"
# Claude Code's file-editing tools, each naming one file; Codex's patches (``runtimes.codex``), naming each file
# they change; and OMP's (``runtimes.omp.edited_paths``).
FILE_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", *OMP_EDIT_TOOLS})

_BROKE_CONTRACT = re.compile(r"^Planner reply broke the (PLAN|DECISION|CHAT|SUPERVISE|GATE) contract \(")
_REPAIR_REJECTED = "invalid reply after a repair turn"
# The Planner's answer to a change to the task, with the action it chose.
_CHANGE_ANSWERED = re.compile(r"^Planner answered a change to the task \((\w+)\)")
_TURN_KIND = re.compile(r"^\[(PLAN|DECISION|CHAT|SUPERVISE|GATE|ROOM)\]( Your last reply broke the reply contract)?")


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


def planner_violation_reasons(activity: Iterable[Mapping[str, Any]]) -> list[str]:
    """Why the parser rejected each Planner reply ``planner_violations`` counts, as the activity says it."""
    return [str(row.get("summary") or "")[:300] for row in activity if row.get("kind") == "error"
            and (_BROKE_CONTRACT.match(str(row.get("summary") or "")) or _REPAIR_REJECTED in str(row.get("summary")))]


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


def planner_profile_runs(records: Iterable[Mapping[str, Any]]) -> int:
    """Worker and Reviewer runs on a profile the task's Planner chose (for the rest of the task or a step)."""
    return sum(1 for record in records if role(record) in ("worker", "reviewer")
               and ((record.get("metadata") or {}).get("profile") or {}).get("source") in ("planner_task",
                                                                                          "planner_step"))


# What every run of a role a task's Planner defined ends with before its contract (``system_prompt.BOUNDARIES_LEAD``).
_BOUNDARIES_LEAD = "## Boundaries\nLooper sets these boundaries."


def role_metrics(detail: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """The roles the task's Planner defined and how their runs went: how many roles (``roles_defined``); the Worker
    and Reviewer runs as one (``role_runs``), on the role's own profile (``role_profile_runs``), as a review role
    (``role_reviews``); role runs whose prompt is not a version the Planner defined (``role_prompt_mismatches``) or
    whose system prompt lacks the boundaries before its contract (``role_boundary_misses``); the accepted steps with a
    review role that lack a passing built-in review, or a passing review by their role where it is required
    (``gate_breaches``); and the reviews by an advisory review role (``advisory_role_reviews``) and those of them that
    did not pass (``advisory_role_fails``)."""
    defined = {(role_["id"], version["version"]): version["sha256"] for role_ in detail.get("roles") or []
               for version in role_.get("versions") or []}
    step_runs = [r for r in records if role(r) in ("worker", "reviewer")]
    role_runs = [r for r in step_runs if ((r.get("metadata") or {}).get("prompt_layers") or {}).get("task_role")]

    def layer(record: Mapping[str, Any]) -> Mapping[str, Any]:
        return record["metadata"]["prompt_layers"]["task_role"]

    def passed(step_id: str, by_role: bool) -> bool:
        return any((r.get("metadata") or {}).get("step_id") == step_id and r["metadata"].get("outcome") == "passed"
                   and bool(((r["metadata"].get("prompt_layers") or {}).get("task_role"))) is by_role
                   for r in step_runs if role(r) == "reviewer")

    prompts = [str((r.get("metadata") or {}).get("system_prompt") or "") for r in role_runs]
    steps = detail.get("steps") or []

    def required(step: Mapping[str, Any]) -> bool:
        return step.get("review_role_required") is not False

    advisory = [r for s in steps if s.get("review_role_key") and not required(s) for r in role_runs
                if role(r) == "reviewer" and r["metadata"].get("step_id") == s["id"]]
    return {
        "roles_defined": len(detail.get("roles") or []),
        "role_runs": len(role_runs),
        "role_profile_runs": sum(1 for r in role_runs
                                 if ((r.get("metadata") or {}).get("profile") or {}).get("source") == "task_role"),
        "role_reviews": sum(1 for r in role_runs if role(r) == "reviewer"),
        "role_prompt_mismatches": sum(1 for r in role_runs
                                      if defined.get((layer(r)["id"], layer(r)["version"])) != layer(r)["sha256"]),
        "role_boundary_misses": sum(1 for text in prompts if _BOUNDARIES_LEAD not in text
                                    or text.rfind(_BOUNDARIES_LEAD) < text.rfind("## Task")),
        "gate_breaches": sum(1 for s in steps if s.get("review_role_key") and s.get("status") == "verified"
                             and not (passed(s["id"], False) and (passed(s["id"], True) or not required(s)))),
        "advisory_role_reviews": len(advisory),
        "advisory_role_fails": sum(1 for r in advisory if r["metadata"].get("outcome") == "not_passed"),
    }


# What a supplement a user gate left reads like in a Worker's or Reviewer's task section (``user_gates.conclusion_text``).
_CONCLUSION = "Settled with the user at gate "


# The agent room's tools as a run's tool calls name them (``mcp__looper__<tool>``; another runtime may drop the
# prefix).
ROOM_TOOL_PREFIX = "looper_room_"


def room_metrics(detail: Mapping[str, Any], activity: Iterable[Mapping[str, Any]],
                 events: Mapping[int, list[Mapping[str, Any]]]) -> dict[str, int]:
    """What the task's agents did in its room: the messages they posted (``room_messages``, Looper's notices and
    escalations apart), of them the questions (``room_questions``), the ones nobody answered (``room_unanswered``)
    and the interface posts (``room_interface``); Looper's escalations to the Planner (``room_escalations``); the
    reminders Claude Code runs got (``room_reminders``); the posts the room refused (``room_refused``); and the runs'
    calls of the room's tools (``room_tool_calls``, reads included)."""
    room = detail.get("room") or {}
    by_kind = (room.get("counts") or {}).get("by_kind") or {}
    rows = list(activity)
    calls = 0
    for record_events in events.values():
        for row in record_events:
            event = row.get("event", row)
            if not isinstance(event, Mapping) or event.get("type") != "assistant":
                continue
            calls += sum(1 for block in (event.get("message") or {}).get("content") or []
                         if isinstance(block, Mapping) and block.get("type") == "tool_use"
                         and ROOM_TOOL_PREFIX in str(block.get("name") or ""))
    return {
        "room_messages": sum(n for kind, n in by_kind.items() if kind not in ("notice", "escalation")),
        "room_questions": int(by_kind.get("question", 0)),
        "room_unanswered": int((room.get("counts") or {}).get("unanswered") or 0),
        "room_interface": int(by_kind.get("interface", 0)),
        "room_escalations": int(by_kind.get("escalation", 0)),
        "room_reminders": sum(1 for r in rows if r.get("summary") == "Room reminder sent"),
        "room_refused": sum(1 for r in rows if str(r.get("summary") or "").startswith("Room: ")
                            and "refused" in str(r.get("summary") or "")),
        "room_tool_calls": calls,
        # The consults of seats whose run had ended, and those that answered.
        "room_consults": int(room.get("consults", {}).get("total") or 0),
        "room_consults_answered": int(room.get("consults", {}).get("answered") or 0),
    }


def gate_metrics(detail: Mapping[str, Any], records: Iterable[Mapping[str, Any]], *, now: float) -> dict[str, int]:
    """The task's user gates and how they went: how many opened (``gates_opened``), the [GATE] turns whose reply was
    taken (``gate_turns``), how they closed (``gates_passed``, of them ``gates_ended_by_user``; ``gates_cancelled``),
    the assumptions their summaries stated (``assumptions_stated``), the steps the turns that closed them added,
    dropped and stopped (``gate_plan_changes``), the Worker runs while a gate waited (``gate_parallel_runs``), and the
    Worker and Reviewer runs whose task section carried a gate's conclusion (``conclusion_reads``)."""
    gates = [g for g in detail.get("gates") or [] if g.get("opened_at")]
    windows = [(timestamp(g["opened_at"]) or now, timestamp(g.get("closed_at")) or now) for g in gates]
    step_runs = [r for r in records if role(r) in ("worker", "reviewer")]
    workers = [(timestamp(r.get("created_at")), timestamp(r.get("finished_at")) or now) for r in step_runs
               if role(r) == "worker" and r.get("created_at")]
    return {
        "gates_opened": len(gates),
        "gate_turns": sum(int(g.get("turns") or 0) for g in gates),
        "gates_passed": sum(1 for g in gates if g.get("state") == "passed"),
        "gates_ended_by_user": sum(1 for g in gates if g.get("state") == "passed" and g.get("ended_by_user")),
        "gates_cancelled": sum(1 for g in gates if g.get("state") == "cancelled"),
        "assumptions_stated": sum(len((g.get("summary") or {}).get("assumptions") or []) for g in gates),
        "gate_plan_changes": sum(len(labels) for g in gates for labels in (g.get("plan_change") or {}).values()),
        "gate_parallel_runs": sum(1 for start, end in workers
                                  if start is not None and any(start < closed and end > opened
                                                               for opened, closed in windows)),
        "conclusion_reads": sum(1 for r in step_runs
                                if _CONCLUSION in str((r.get("metadata") or {}).get("system_prompt") or "")),
    }


@dataclass(frozen=True)
class Discipline:
    """A discipline a task's work spans: the words of a role id that name it (``fe`` in ``fe_rd``) and terms of its
    conventions (lower case), at least ``CONVENTION_TERMS`` of which the prompt of its role names."""

    name: str
    ids: tuple[str, ...]
    conventions: tuple[str, ...]


CONVENTION_TERMS = 2


def discipline_roles(roles: Iterable[Mapping[str, Any]], disciplines: Iterable[Discipline]) -> int:
    """The ``disciplines`` that have a work role of their own (its id names that discipline and no other) whose
    prompt names at least ``CONVENTION_TERMS`` of the discipline's conventions."""
    disciplines = list(disciplines)

    def named(role_id: str) -> set[str]:
        words = set(re.split(r"[_.-]", role_id.lower()))
        return {d.name for d in disciplines if words & set(d.ids)}

    work = [r for r in roles if r.get("kind") == "work"]
    return sum(1 for d in disciplines if any(
        named(str(r.get("id") or "")) == {d.name}
        and sum(term in str(r.get("role_prompt") or "").lower() for term in d.conventions) >= CONVENTION_TERMS
        for r in work))


def interface_dependents(steps: Iterable[Mapping[str, Any]]) -> int:
    """The most steps run as a work role that depend, directly or through other steps, on one same step: the
    platform steps built on one interface."""
    steps = list(steps)
    before = _before(steps)
    role_steps = [str(s.get("id")) for s in steps if s.get("role_key") and s.get("status") != "skipped"]
    return max((sum(1 for step_id in role_steps if shared in before[step_id]) for shared in before), default=0)


def turn_kinds(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """The Planner's turns by kind (``plan``, ``decision``, ``chat``, ``supervise``, ``gate``, ``room``, ``repair``)."""
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
                named = [tool_input.get("file_path") or tool_input.get("notebook_path"),
                         *(c.get("path") for c in tool_input.get("changes") or [] if isinstance(c, Mapping)),
                         *omp_edited_paths(str(block.get("name")), tool_input)]
                paths += [_relative(str(path), work_dir) for path in named if path]
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


def most_at_once(spans: Iterable[tuple[float, float]]) -> int:
    """The most of ``spans`` (start, end) that overlap at one moment; spans that only touch do not."""
    edges = sorted(edge for start, end in spans for edge in ((start, 1), (end, -1)))
    running = most = 0
    for _, change in edges:
        running += change
        most = max(most, running)
    return most


def _before(steps: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Each of the plan's steps and every step it depends on, directly or through other steps."""
    needs = {str(step.get("id")): {str(d) for d in step.get("depends_on") or []} for step in steps}

    def upstream(step_id: str) -> set[str]:
        found: set[str] = set()
        todo = list(needs.get(step_id, ()))
        while todo:
            if (current := todo.pop()) not in found:
                found.add(current)
                todo += needs.get(current, ())
        return found

    return {step_id: upstream(step_id) for step_id in needs}


def _independent(before: Mapping[str, set[str]]) -> list[tuple[str, str]]:
    ids = sorted(before)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if a not in before[b] and b not in before[a]]


def independent_pairs(steps: Iterable[Mapping[str, Any]]) -> int:
    """Pairs of the plan's steps with no dependency between them, directly or through other steps: the work the
    plan lets run at the same time."""
    return len(_independent(_before(steps)))


def bracketed_pairs(steps: Iterable[Mapping[str, Any]]) -> int:
    """Independent pairs of the plan's steps with a step both depend on (an interface they share) and a step that
    depends on both (their integration): coordinated parts built side by side."""
    before = _before(steps)
    return sum(1 for a, b in _independent(before)
               if before[a] & before[b] and any({a, b} <= upstream for upstream in before.values()))


def final_summary(detail: Mapping[str, Any]) -> int:
    """1 when the task's result is a final summary with every field (code writes one heading per field only when
    the Planner gave them all), else 0."""
    result = str(detail.get("result_summary") or "")
    return int(all(f"## {heading}\n" in result for heading in FINAL_HEADINGS.values()))


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
    the run starts, and ``tolerance`` what that measurement may be off by: a
    file written within ``tolerance`` seconds of a Planner turn is the
    Planner's (no one else writes then), unless it is that close to a step run
    too, which it is left to.
    """
    def near(moment: float, spans: Iterable[tuple[float, float]]) -> bool:
        return any(start - clock_offset - tolerance <= moment <= end - clock_offset + tolerance for start, end in spans)

    steps = [*windows.get("worker", []), *windows.get("reviewer", [])]
    return sorted(
        path for path, mtime in changes.items() if near(mtime, windows.get("planner", [])) and not near(mtime, steps)
    )


def plan_changes(activity: Iterable[Mapping[str, Any]]) -> int:
    """The changes to the task the Planner answered by changing the plan (``add_steps`` or ``replan``)."""
    return sum(1 for row in activity if (match := _CHANGE_ANSWERED.match(str(row.get("summary") or "")))
               and match.group(1) in ("add_steps", "replan"))


def flow_problems(expect: Mapping[str, int], observed: Mapping[str, int]) -> list[str]:
    """The expected flow counts (``min_<metric>``, ``max_<metric>``) the task missed."""
    problems = []
    for key, bound in expect.items():
        metric = key.removeprefix("min_").removeprefix("max_")
        value = observed.get(metric, 0)
        if key.startswith("max_") and value > bound:
            problems.append(f"{metric} {value} > {bound}")
        elif key.startswith("min_") and value < bound:
            problems.append(f"{metric} {value} < {bound}")
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
    disciplines: Iterable[Discipline] = (),
    gate_asks_beyond_script: int = 0,
    clock_offset: float = 0.0,
    now: float,
) -> dict[str, Any]:
    """One task's result: success and why not, the flow counts, cost, contract violations and the Planner's
    file changes. ``expect`` may bound ``contract_violations`` (their total) too, and the gates the scripted user
    ended because the Planner asked after its script ran out (``gate_asks_beyond_script``, the runner's count)."""
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
    windows = run_windows(records, now=now)
    changed_by_planner = planner_changed_files(changes, windows, clock_offset=clock_offset)

    kinds = turn_kinds(records)
    observed = {
        "steps": len(detail.get("steps") or []),
        "reviews": len(by_role.get("reviewer", [])),
        "retries": retries(records),
        "chat_turns": kinds.get("chat", 0),
        "plan_changes": plan_changes(activity),
        # What the plan lets run at the same time, and the most Workers that did.
        "independent_pairs": independent_pairs(detail.get("steps") or []),
        "bracketed_pairs": bracketed_pairs(detail.get("steps") or []),
        "parallel_workers": most_at_once(windows.get("worker", [])),
        # The Planner's progress notes kept in the task journal, and whether the task ended with a final summary.
        "progress_notes": sum(1 for entry in detail.get("journal") or [] if entry.get("kind") == "progress"),
        "final_summary": final_summary(detail),
        # The runs on a profile the Planner chose with its tool.
        "planner_profile_runs": planner_profile_runs(records),
        # The roles the Planner defined for its steps, and how their runs went.
        **role_metrics(detail, records),
        # The disciplines with a role of their own that knows their conventions, and the most role steps built on
        # one interface step.
        "discipline_roles": discipline_roles(detail.get("roles") or [], disciplines),
        "interface_dependents": interface_dependents(detail.get("steps") or []),
        # The user gates the Planner opened, and how they went.
        **gate_metrics(detail, records, now=now),
        "gate_asks_beyond_script": gate_asks_beyond_script,
        # What the task's agents did in its room.
        **room_metrics(detail, activity, events),
    }
    violations = {"planner": planner_violations(activity), "worker": worker_violations,
                  "reviewer": reviewer_violations}
    status = str(detail.get("status") or "")
    problems = []
    if status != "completed":
        error = " ".join(str(detail.get("error") or "").split())[:200]
        problems.append(f"task {status or 'unknown'}" + (f": {error}" if error else ""))
    if check_problem:
        problems.append(check_problem)
    problems += flow_problems(expect, {**observed, "contract_violations": sum(violations.values())})

    cost = detail.get("cost") or {}
    # Tokens are what every runtime reports, priced or not (a subscription, a provider without prices).
    tokens = {name: sum(int(r.get(f"{name}_tokens") or 0) for r in records)
              for name in ("input", "output", "cache_read", "cache_creation")}
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
        # The runs that ended in an error (their record's status), and the models each role's runs reported.
        "run_errors": sum(1 for r in records if r.get("status") == RECORD_FAILED),
        "models": {name: sorted({str(r["model"]) for r in rows if r.get("model")}) for name, rows in by_role.items()},
        "tokens": tokens,
        # How the runs were priced: a runtime's own login (an equivalent, Claude Code's figure or Codex's tokens at
        # the built-in prices), by Looper at an API key provider's prices, or not at all (tokens only).
        "cost_basis": {name: int(cost.get(f"{name}_runs") or 0) for name in ("equivalent", "provider_price",
                                                                            "unpriced")},
        "contract_violations": {**violations, "total": sum(violations.values())},
        "planner_violation_reasons": planner_violation_reasons(activity),
        "planner_edit_tool_paths": planner_edits,
        "planner_changed_files": changed_by_planner,
        "planner_modified_files": len(set(planner_edits) | set(changed_by_planner)),
    }
