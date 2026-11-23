"""Task-quality figures from the typed attempt and outcome records (task-metrics RFC, section 5).

The functions here are pure: ``compute`` reads a window's rows through ``db.metrics`` once, and every figure is a
function of those rows. A figure that cannot be computed is ``{"error": ...}`` and the others still answer (section 11).
This module never renders model input.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import metrics as db_metrics
from .attempts import Decision, FailureKind, Trigger, TaskOutcome, StopCause

logger = logging.getLogger(__name__)

MIN_SAMPLE = 20  # a ratio is shown from this many cases (D11, D19)
COMPARISON_FACTOR = 4  # the comparison window is this many times as long as the window, and ends where it begins
Z95 = 1.959963984540054
_REWORK = (Trigger.RETRY, Trigger.RESTART, Trigger.REOPEN_LATE_REVIEW, Trigger.REOPEN_VERIFICATION)
_REOPEN = (Trigger.REOPEN_LATE_REVIEW, Trigger.REOPEN_VERIFICATION)
_RUN_FAILURES = (FailureKind.RUN_ERROR, FailureKind.TIMEOUT, FailureKind.RUN_CAP, FailureKind.CRASH)
_RESOLVED = (Decision.ACCEPT, Decision.FAIL, Decision.REPLAN)
_FIRST_TRIGGERS = (Trigger.FIRST, Trigger.REPLANNED)


# --- numbers ---

def wilson(k: int, n: int) -> tuple[float, float]:
    """The Wilson 95% interval of k cases in n."""
    p = k / n
    centre = (p + Z95 ** 2 / (2 * n)) / (1 + Z95 ** 2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n ** 2)) / (1 + Z95 ** 2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def ratio(k: int, n: int) -> dict[str, Any]:
    """k cases of n: the value and its interval from MIN_SAMPLE cases, else only that the sample is too small."""
    if n < MIN_SAMPLE:
        return {"insufficient": True, "n": n}
    low, high = wilson(k, n)
    return {"n": n, "k": k, "value": k / n, "wilson": [low, high]}


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between the closest ranks, as SQL's percentile_cont."""
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * q
    low, high = math.floor(at), math.ceil(at)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def spread(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95)}


def _ms(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


# --- cost tree ---

_CATEGORIES = ("worker", "review", "advisor")
_KIND_CATEGORY = {"worker": "worker", "explorer": "worker", "reviewer": "review",
                  "advisor": "advisor", "advice_check": "advisor"}


class Cost:
    """What runs cost, per billing basis (the ``spend_since`` split): money, runs, and runs whose cost is unknown."""

    __slots__ = ("usd", "runs", "unknown")

    def __init__(self) -> None:
        self.usd = {"provider_priced": 0.0, "equivalent": 0.0}
        self.runs = {"provider_priced": 0, "equivalent": 0}
        self.unknown = {"provider_priced": 0, "equivalent": 0}

    def add(self, record: Mapping[str, Any]) -> None:
        basis = "equivalent" if record["equivalent"] else "provider_priced"
        self.runs[basis] += 1
        if record["cost_usd"] is None:
            self.unknown[basis] += 1
        else:
            self.usd[basis] += float(record["cost_usd"])

    def merge(self, other: Cost) -> None:
        for basis in self.usd:
            self.usd[basis] += other.usd[basis]
            self.runs[basis] += other.runs[basis]
            self.unknown[basis] += other.unknown[basis]

    def view(self) -> dict[str, Any]:
        """Per basis ``usd`` (null when every run of it is unknown, never 0), ``runs`` and ``unknown_runs``."""
        return {basis: {"usd": None if self.runs[basis] and self.unknown[basis] == self.runs[basis]
                        else round(self.usd[basis], 6),
                        "runs": self.runs[basis], "unknown_runs": self.unknown[basis]} for basis in self.usd}


def _group() -> dict[str, Cost]:
    return {category: Cost() for category in _CATEGORIES}


@dataclass
class CostTree:
    """One task's runs attributed: to attempts (Worker, review, advisor), to the Planner's planning, or to no attempt."""

    attempts: dict[int, dict[str, Cost]] = field(default_factory=lambda: defaultdict(_group))
    planning: Cost = field(default_factory=Cost)
    unattributed: dict[str, Cost] = field(default_factory=_group)


def attribute(records: Iterable[Mapping[str, Any]]) -> CostTree:
    """Walk each run up its parents to the nearest one that names an attempt. The Planner's own turns, including the
    decision turns and the advise turns that carry an ``attempt_id``, are planning (D24): the decision itself is read
    from ``step_attempts``, never from which Planner records carry an attempt. An advisor's or a judge's run belongs to
    the attempt of the turn that started it; when that turn has none (a consult not followed by ``proceed``), the
    spend stays unattributed, in its own bucket, and is not dropped. A parent that is gone ends the walk."""
    rows = {row["id"]: row for row in records}
    tree = CostTree()
    for row in rows.values():
        category = _KIND_CATEGORY.get(row["kind"])
        if category is None:  # the Planner's turns and what it asks outbound reviews of
            tree.planning.add(row)
            continue
        owner, seen, current = None, set(), row
        while current is not None and current["id"] not in seen:
            if current["attempt_id"] is not None:
                owner = current["attempt_id"]
                break
            seen.add(current["id"])
            current = rows.get(current["parent_record_id"])
        (tree.attempts[owner] if owner is not None else tree.unattributed)[category].add(row)
    return tree


def _sum(costs: Iterable[Cost]) -> Cost:
    total = Cost()
    for cost in costs:
        total.merge(cost)
    return total


def group_view(group: Mapping[str, Cost]) -> dict[str, Any]:
    return {**{category: group[category].view() for category in _CATEGORIES}, "total": _sum(group.values()).view()}


# --- steps ---

def _is_rework(attempt: Mapping[str, Any]) -> bool:
    return attempt["trigger"] in _REWORK


@dataclass
class Step:
    """A step's attempts within one run of its task, oldest first."""

    task_id: str
    run_no: int
    key: str
    attempts: list[dict[str, Any]]

    @property
    def first(self) -> dict[str, Any]:
        return self.attempts[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.attempts[-1]

    @property
    def accepted_at(self) -> datetime | None:
        accepted = [a["decided_at"] for a in self.attempts if a["decision"] == Decision.ACCEPT]
        return accepted[-1] if accepted else None


def group_steps(attempts: Iterable[Mapping[str, Any]]) -> list[Step]:
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        grouped[(attempt["task_id"], attempt["task_run_no"], attempt["step_key"])].append(dict(attempt))
    return [Step(*key, sorted(rows, key=lambda a: a["attempt"])) for key, rows in grouped.items()]


@dataclass(frozen=True)
class Filters:
    project: str | None = None
    profile: str | None = None
    difficulty: str | None = None


class Data:
    """A window's rows, read once. A part that failed to read raises its error where a figure asks for it."""

    def __init__(self, lo: datetime, hi: datetime, filters: Filters) -> None:
        self.lo, self.hi, self.filters = lo, hi, filters
        self.parts: dict[str, Any] = {}

    def part(self, name: str) -> Any:
        value = self.parts[name]
        if isinstance(value, BaseException):
            raise value
        return value

    def within(self, moment: datetime | None) -> bool:
        return moment is not None and self.lo <= moment < self.hi

    @property
    def runs(self) -> list[dict[str, Any]]:
        return self.part("tasks")

    @cached_property
    def denominator(self) -> list[dict[str, Any]]:
        """Each task's last run ended in the window, without the cancelled and the restarted ones (section 5.1)."""
        return [r for r in self.runs if r["last"] and r["outcome"] != TaskOutcome.CANCELLED
                and r["stop_cause"] != StopCause.RESTART]

    @cached_property
    def steps(self) -> list[Step]:
        """Steps with an attempt decided in the window. A profile filter keeps every step that has an attempt of that
        profile, so a step that used more than one profile is counted under each of them; a difficulty filter keeps
        the steps of that difficulty."""
        keep = []
        for step in group_steps(self.part("attempts")):
            if self.filters.profile and not any(a["profile_id"] == self.filters.profile for a in step.attempts):
                continue
            if self.filters.difficulty and step.first["difficulty"] != self.filters.difficulty:
                continue
            keep.append(step)
        return keep

    @property
    def decided(self) -> list[dict[str, Any]]:
        """Attempts decided in the window (of the filtered steps); with a profile, only the attempts of that profile."""
        profile = self.filters.profile
        return [a for step in self.steps for a in step.attempts
                if self.within(a["decided_at"]) and (profile is None or a["profile_id"] == profile)]

    def accepted_steps(self) -> list[Step]:
        return [s for s in self.steps if self.within(s.accepted_at)]


# --- figures ---

def _share(rows: Sequence[Any], test: Callable[[Any], bool]) -> dict[str, Any]:
    return ratio(sum(1 for row in rows if test(row)), len(rows))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _average_cost(rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, Any]:
    """The mean of (usd, unknown_runs) pairs: a case with an unknown cost is counted apart, never as 0."""
    known = [usd for usd, unknown in rows if not unknown]
    return {"mean_usd": _mean(known), "n_known": len(known), "unknown": len(rows) - len(known)}


def task_success_rate(d: Data) -> dict[str, Any]:
    return _share(d.denominator, lambda r: r["outcome"] == TaskOutcome.COMPLETED)


def task_budget_failure_rate(d: Data) -> dict[str, Any]:
    return _share(d.denominator, lambda r: r["outcome"] == TaskOutcome.FAILED and r["failure_kind"] == "task_budget")


def task_restarts(d: Data) -> dict[str, Any]:
    return {"count": sum(1 for r in d.runs if r["run_no"] > 1)}


def task_cost_per_completed(d: Data) -> dict[str, Any]:
    done = [r for r in d.denominator if r["outcome"] == TaskOutcome.COMPLETED]
    return {"n": len(done), **{basis: _average_cost([(r[f"{basis}_usd"] or 0.0, r["unpriced_runs"]) for r in done])
                               for basis in ("provider_priced", "equivalent")}}


def task_duration(d: Data) -> dict[str, Any]:
    return spread([_ms(r["started_at"], r["ended_at"]) for r in d.denominator if r["started_at"]])


def task_agent_duration(d: Data) -> dict[str, Any]:
    return spread([max(0.0, _ms(r["started_at"], r["ended_at"]) - r["human_wait_ms"])
                   for r in d.denominator if r["started_at"]])


def task_queue_time(d: Data) -> dict[str, Any]:
    """From creating the task to its first Planner run; a restarted run waited for no queue, only the first one counts."""
    return spread([_ms(r["task_created_at"], r["started_at"]) for r in d.denominator
                   if r["started_at"] and r["run_no"] == 1])


def task_user_corrections(d: Data) -> dict[str, Any]:
    rows = d.denominator
    return {"supplement": _share(rows, lambda r: r["supplemented"]), "revision": _share(rows, lambda r: r["revised"]),
            "feedback": _share(rows, lambda r: r["feedback"] is not None)}


def step_first_pass_rate(d: Data) -> dict[str, Any]:
    """Steps of the original plan (first attempt ``first``) that were accepted at their only attempt. A step still open,
    or skipped, or cut off by the task ending is not counted. One that was reopened and is still pending stays in the
    denominator: the reopen already spent the first pass. Steps added by a replan are apart (``replans``)."""
    steps = [s for s in d.steps if s.first["trigger"] == Trigger.FIRST and d.within(s.first["decided_at"])
             and (s.last["decision"] in _RESOLVED or any(a["trigger"] in _REOPEN for a in s.attempts))]
    return _share(steps, lambda s: len(s.attempts) == 1 and s.last["decision"] == Decision.ACCEPT)


def step_replans(d: Data) -> dict[str, Any]:
    return {"decisions": sum(1 for a in d.decided if a["decision"] == Decision.REPLAN),
            "steps_added": sum(1 for s in d.steps if s.first["trigger"] == Trigger.REPLANNED
                               and d.within(s.first["decided_at"])),
            "added_first_pass": _share([s for s in d.steps if s.first["trigger"] == Trigger.REPLANNED
                                        and d.within(s.first["decided_at"]) and s.last["decision"] in _RESOLVED],
                                       lambda s: len(s.attempts) == 1 and s.last["decision"] == Decision.ACCEPT)}


def step_attempts_per_accepted(d: Data) -> dict[str, Any]:
    counts = [len(s.attempts) for s in d.accepted_steps()]
    return {"n": len(counts), "mean": _mean(counts),
            "distribution": {"1": counts.count(1), "2": counts.count(2), "3": counts.count(3),
                             "4+": sum(1 for c in counts if c >= 4)}}


def step_rework_rate(d: Data) -> dict[str, Any]:
    return _share(d.decided, _is_rework)


def step_failure_kinds(d: Data) -> dict[str, Any]:
    failed = [a for a in d.decided if a["decision"] != Decision.ACCEPT]
    counts: dict[str, int] = defaultdict(int)
    for a in failed:
        counts[a["failure_kind"] or "none"] += 1
    return {"n": len(failed), "counts": dict(counts)}


def step_duration(d: Data) -> dict[str, Any]:
    return spread([_ms(s.first["started_at"], s.accepted_at) for s in d.accepted_steps()])


def step_cost_per_accepted(d: Data) -> dict[str, Any]:
    """What an accepted step cost over all its attempts, the failed ones too: its first attempt and its retries apart,
    per basis. Planning is not spread over steps (D24)."""
    steps = d.accepted_steps()
    tree = attribute(d.part("records"))
    result: dict[str, Any] = {"n": len(steps)}
    for basis in ("provider_priced", "equivalent"):
        rows: dict[str, list[tuple[float, int]]] = {"first": [], "retry": [], "total": []}
        for step in steps:
            parts = {"first": Cost(), "retry": Cost()}
            for a in step.attempts:
                parts["first" if a["trigger"] in _FIRST_TRIGGERS else "retry"].merge(
                    _sum(tree.attempts.get(a["id"], _group()).values()))
            for name, cost in (*parts.items(), ("total", _sum(parts.values()))):
                rows[name].append((cost.usd[basis], cost.unknown[basis]))
        result[basis] = {name: _average_cost(pairs) for name, pairs in rows.items()}
    return result


def alert_step_cost(d: Data) -> dict[str, Any]:
    """Median accepted-step cost, counting only known steps with runs of that billing basis.

    Unlike the public average, an absent basis is no observation for this rule. An unknown run excludes that
    step from that basis; it is never silently turned into a free step. Failed attempts and attributed child
    runs still belong to the accepted step's total (D8).
    """
    tree = attribute(d.part("records"))
    costs = [_sum(cost for attempt in step.attempts for cost in tree.attempts.get(attempt["id"], {}).values())
             for step in d.accepted_steps()]
    result = {}
    for basis in ("provider_priced", "equivalent"):
        values = [cost.usd[basis] for cost in costs if cost.runs[basis] and not cost.unknown[basis]]
        result[basis] = {"n": len(values), "median": percentile(values, 0.5)}
    return result


def alert_review_misses(d: Data) -> dict[str, Any]:
    """The count's sample is the decided attempts in the window, including reopen decisions."""
    return {"n": len(d.decided), **review_misses(d)}


def review_block_rate(d: Data) -> dict[str, Any]:
    reviewed = [a for a in d.decided if a["review_outcome"] in ("passed", "not_passed", "passed_by_adjudication")]
    return _share(reviewed, lambda a: a["review_outcome"] == "not_passed")


def _effective(d: Data) -> tuple[int, int]:
    blocked = effective = 0
    profile = d.filters.profile
    for step in d.steps:
        for a in step.attempts:
            if (a["review_outcome"] != "not_passed" or not d.within(a["decided_at"])
                    or (profile and a["profile_id"] != profile)):
                continue
            blocked += 1
            effective += any(b["attempt"] > a["attempt"] and b["decision"] == Decision.ACCEPT for b in step.attempts)
    return effective, blocked


def review_effective_blocks(d: Data) -> dict[str, Any]:
    effective, blocked = _effective(d)
    return {"count": effective, "blocked": blocked, "rate": ratio(effective, blocked)}


def review_misses(d: Data) -> dict[str, Any]:
    return {"count": sum(1 for a in d.decided if a["trigger"] in _REOPEN)}


def review_catch_rate(d: Data) -> dict[str, Any]:
    effective, _ = _effective(d)
    misses = review_misses(d)["count"]
    return ratio(effective, effective + misses)


def review_defective_deliveries(d: Data) -> dict[str, Any]:
    done = [r for r in d.denominator if r["outcome"] == TaskOutcome.COMPLETED]
    verdicts: dict[str, int] = defaultdict(int)
    for r in done:
        if r["feedback"]:
            verdicts[r["feedback"]] += 1
    return {"count": verdicts.get("defect", 0), "completed": len(done), "by_verdict": dict(verdicts)}


def review_adjudication_rate(d: Data) -> dict[str, Any]:
    blocked = [a for a in d.decided if a["blocking_findings"] > 0 or a["adjudicated"]
               or a["review_outcome"] in ("not_passed", "passed_by_adjudication")]
    return _share(blocked, lambda a: a["adjudicated"])


def _rate(k: int, n: int) -> dict[str, Any]:
    """Rejections over turns: more than one a turn is possible, so this is a rate, not a proportion with an interval."""
    return {"insufficient": True, "n": n} if n < MIN_SAMPLE else {"n": n, "k": k, "value": k / n}


def contract_rates(d: Data) -> dict[str, Any]:
    c = d.part("contract")
    return {"worker": ratio(c["worker_contract"], c["worker_runs"]),
            "reviewer": ratio(c["reviewer_contract"], c["reviewer_runs"]),
            "planner": _rate(c["planner_rejections"], c["planner_turns"])}


def run_error_rate(d: Data) -> dict[str, Any]:
    ran = [a for a in d.decided if a["worker_runs"] > 0]
    return _share(ran, lambda a: a["failure_kind"] in _RUN_FAILURES)


FIGURES: dict[str, dict[str, Callable[[Data], dict[str, Any]]]] = {
    "tasks": {"success_rate": task_success_rate, "budget_failure_rate": task_budget_failure_rate,
              "restarts": task_restarts, "cost_per_completed_task": task_cost_per_completed,
              "duration": task_duration, "agent_duration": task_agent_duration, "queue_time": task_queue_time,
              "user_corrections": task_user_corrections},
    "steps": {"first_pass_rate": step_first_pass_rate, "replans": step_replans,
              "attempts_per_accepted_step": step_attempts_per_accepted, "rework_rate": step_rework_rate,
              "failure_kinds": step_failure_kinds, "cost_per_accepted_step": step_cost_per_accepted,
              "duration": step_duration},
    "reviews": {"block_rate": review_block_rate, "effective_blocks": review_effective_blocks,
                "misses": review_misses, "catch_rate": review_catch_rate,
                "defective_deliveries": review_defective_deliveries, "adjudication_rate": review_adjudication_rate},
    "contract": {"rates": contract_rates, "run_error_rate": run_error_rate},
}


def _evaluate(figure: Callable[[Data], dict[str, Any]], data: Data) -> dict[str, Any]:
    try:
        return figure(data)
    except Exception as exc:
        logger.exception("Metric %s failed", figure.__name__)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _attempt(label: str, read) -> Any:
    try:
        return await read()
    except Exception as exc:
        logger.exception("Metric rows failed: %s", label)
        return exc


async def load(lo: datetime, hi: datetime, filters: Filters, floor: datetime | None, *, pool=None) -> Data:
    """Read a window's rows; each part on its own, so one failing read costs only the figures that need it."""
    data = Data(lo, hi, filters)
    args = (lo, hi, filters.project, filters.profile, filters.difficulty)
    data.parts["tasks"] = await _attempt("tasks", lambda: db_metrics.task_rows(*args, pool=pool))
    data.parts["attempts"] = await _attempt("attempts", lambda: db_metrics.attempt_rows(lo, hi, filters.project, pool=pool))
    data.parts["contract"] = await _attempt(
        "contract", lambda: db_metrics.contract_counts(*args, floor, pool=pool))

    async def records():
        accepted = [s.task_id for s in data.accepted_steps()]
        return await db_metrics.task_records(sorted(set(accepted)), pool=pool)

    data.parts["records"] = await _attempt("records", records)
    return data


async def compute(days: int, filters: Filters, *, now: datetime | None = None, pool=None,
                  alerts: bool = False) -> dict[str, Any]:
    """Every figure of the window (the last ``days`` days) and the same figure of the comparison window (the
    ``COMPARISON_FACTOR * days`` days before it). ``coverage_since`` says from when the records exist (D18);
    ``missing_attempts`` counts Worker runs since then that have no attempt (a failed write, section 11)."""
    now = now or datetime.now(timezone.utc)
    split = now - timedelta(days=days)
    windows = {"current": (split, now), "previous": (split - timedelta(days=COMPARISON_FACTOR * days), split)}
    coverage = await db_metrics.coverage_since(pool=pool)
    loaded = {name: await load(lo, hi, filters, coverage, pool=pool) for name, (lo, hi) in windows.items()}
    result: dict[str, Any] = {
        "days": days, "window": {"since": split.isoformat(), "until": now.isoformat()},
        "comparison_window": {"since": windows["previous"][0].isoformat(), "until": split.isoformat()},
        "filters": {"project": filters.project, "profile": filters.profile, "difficulty": filters.difficulty},
        "coverage_since": coverage.isoformat() if coverage else None, "missing_attempts": None}
    contract = loaded["current"].parts["contract"]
    if not isinstance(contract, BaseException):
        result["missing_attempts"] = contract["missing_attempts"]
    for group, figures in FIGURES.items():
        result[group] = {}
        for name, figure in figures.items():
            item = _evaluate(figure, loaded["current"])
            if "error" not in item:
                item["previous"] = _evaluate(figure, loaded["previous"])
            result[group][name] = item
    if alerts:
        # Internal rule inputs reuse the same read. Public metrics keep their exact response schema.
        result["alert_figures"] = {}
        for name, figure in (("cost", alert_step_cost), ("misses", alert_review_misses)):
            item = _evaluate(figure, loaded["current"])
            if "error" not in item:
                item["previous"] = _evaluate(figure, loaded["previous"])
            result["alert_figures"][name] = item
    return result


def _attempt_view(attempt: Mapping[str, Any], tree: CostTree) -> dict[str, Any]:
    cost = tree.attempts.get(attempt["id"], _group())
    return {**{key: attempt[key] for key in (
        "id", "attempt", "task_run_no", "trigger", "profile_id", "worker_runs", "run_outcome", "review_outcome",
        "review_runs", "blocking_findings", "adjudicated", "decision", "failure_kind")},
        "started_at": attempt["started_at"].isoformat(),
        "decided_at": attempt["decided_at"].isoformat() if attempt["decided_at"] else None,
        "cost": group_view(cost)}


async def task_attempts(task_id: str, *, pool=None) -> dict[str, Any] | None:
    """One task's attempts, step by step, with what each cost and what the task cost beyond them. Attempts, the
    task's planning and what no attempt owns add up to ``task_costs`` (per basis). None for an unknown task."""
    task = await db_metrics.task_state(task_id, pool=pool)
    if task is None:
        return None
    attempts = await db_metrics.task_attempt_rows(task_id, pool=pool)
    tree = attribute(await db_metrics.task_records([task_id], pool=pool))
    coverage = await db_metrics.coverage_since(pool=pool)
    steps = group_steps(attempts)
    first, retry = _group(), _group()
    for step in steps:
        for a in step.attempts:
            target = first if a["trigger"] in _FIRST_TRIGGERS else retry
            for category in _CATEGORIES:
                target[category].merge(tree.attempts.get(a["id"], _group())[category])
    owned = {a["id"] for s in steps for a in s.attempts}
    for attempt_id, group in tree.attempts.items():  # an attempt that is no longer listed is not lost
        if attempt_id not in owned:
            for category in _CATEGORIES:
                tree.unattributed[category].merge(group[category])
    everything = _sum([*first.values(), *retry.values(), tree.planning, *tree.unattributed.values()])
    return {
        "task_id": task_id, "status": task["status"], "run_no": task["run_no"],
        "coverage_since": coverage.isoformat() if coverage else None,
        "missing_attempts": await db_metrics.task_missing_attempts(task_id, coverage, pool=pool),
        "steps": [{"step_key": s.key, "step_id": s.first["step_id"], "task_run_no": s.run_no,
                   "difficulty": s.first["difficulty"], "accepted": s.accepted_at is not None,
                   "attempts": [_attempt_view(a, tree) for a in s.attempts]} for s in steps],
        "summary": {
            "steps": len(steps), "attempts": len(attempts),
            "accepted_steps": sum(1 for s in steps if s.accepted_at is not None),
            "first_pass_steps": sum(1 for s in steps if s.first["trigger"] == Trigger.FIRST
                                    and len(s.attempts) == 1 and s.last["decision"] == Decision.ACCEPT),
            "open_attempts": sum(1 for a in attempts if a["decision"] is None),
            "failure_kinds": dict(_count(a["failure_kind"] for a in attempts
                                         if a["decision"] not in (None, Decision.ACCEPT) and a["failure_kind"]))},
        "costs": {"first": group_view(first), "retry": group_view(retry), "planning": tree.planning.view(),
                  "unattributed": group_view(tree.unattributed), "total": everything.view()}}


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return counts
