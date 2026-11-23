"""How often a project skill is loaded and called, and what the attempts that loaded it did (skill lifecycle RFC, section 7).

Observational data, not an experiment. A skill counts as *loaded* by a Worker run whose record lists it with the
current content hash (the hash, not the version number, identifies the content: a rollback is the same hash). It counts
as *invoked* when the run's stored Claude Code stream shows a ``Skill`` tool call for it; verified against Claude Code
2.1.330 with ``tests/test_skill_invocation_stream.py``. Runtimes without that event report *unknown*, never 0, and stay
out of the invocation rate. The first-pass rate is the task-metrics definition: an attempt with ``trigger = first``
that was accepted, was the step's only attempt and was never reopened, joined by ``attempt_id``. Fewer than 20 attempts
on either side shows counts only. Nothing here changes a state or sends a notice.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Sequence

from ..db import skill_usage as store
from .library import PROJECT_SKILLS_PLUGIN

WINDOW = timedelta(days=7)
COMPARISON = timedelta(days=28)  # the 28 days before the window
STALE_AFTER = timedelta(days=90)
MIN_ATTEMPTS = 20  # on each side, for a rate
NEVER_INVOKED_ABOVE = 30
NOTE = "Observational data, not a controlled experiment: skills are often added to harder tasks."
# The runtimes whose stream carries the Skill tool call; every other runtime's invocation is unknown.
CALL_RUNTIMES = frozenset({"claude-code"})
# Decisions that end an attempt as a result; skipped steps and ended tasks say nothing about the attempt.
_DECIDED = frozenset({"accept", "retry", "replan", "fail"})


class ReviewReason(StrEnum):
    NEVER_INVOKED = "never_invoked"  # loaded by more than 30 attempts whose runtime can tell, none called it
    STALE = "stale"  # a runtime that can tell loaded it for 90 days and nothing called it
    DIVERGENT = "divergent"  # the Wilson intervals of the first-pass rate with and without it do not overlap


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson interval of ``successes`` in ``n``. (Also in the metrics module once it is on main.)"""
    if n <= 0:
        return 0.0, 1.0
    p, denominator = successes / n, 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Run:
    record_id: int
    attempt_id: int | None
    created_at: datetime
    runtime: str | None
    items: tuple[dict[str, Any], ...]
    calls: frozenset[str]

    def lists(self, name: str, sha256: str | None = None) -> bool:
        return any(item.get("kind") == "skill" and item.get("plugin") == PROJECT_SKILLS_PLUGIN
                   and item.get("name") == name and (sha256 is None or item.get("sha256") == sha256)
                   for item in self.items)

    def known(self) -> bool:
        return self.runtime in CALL_RUNTIMES

    def called(self, name: str) -> bool:
        """``project:<name>``, or the bare name when no other skill the run loaded has it (Claude Code takes either)."""
        if f"{PROJECT_SKILLS_PLUGIN}:{name}" in self.calls:
            return True
        shared = any(item.get("kind") == "skill" and item.get("name") == name
                     and item.get("plugin") != PROJECT_SKILLS_PLUGIN for item in self.items)
        return name in self.calls and not shared


@dataclass(frozen=True)
class Attempt:
    id: int
    trigger: str
    decision: str | None
    started_at: datetime
    tries: int
    cost: float | None

    @property
    def in_effect(self) -> bool:
        return self.trigger == "first" and self.decision in _DECIDED

    @property
    def first_pass(self) -> bool:
        return self.decision == "accept" and self.tries == 1


@dataclass
class SkillUsage:
    skill_id: str
    name: str
    sha256: str
    lifecycle: str
    versions: list[int]
    review: list[ReviewReason]
    window: tuple[datetime, datetime]
    comparison_window: tuple[datetime, datetime]
    current: dict[str, Any]
    comparison: dict[str, Any]
    coverage_since: datetime | None

    def view(self) -> dict[str, Any]:
        def span(value: tuple[datetime, datetime]) -> dict[str, str]:
            return {"start": value[0].isoformat(), "end": value[1].isoformat()}

        return {
            "skill_id": self.skill_id, "name": self.name, "sha256": self.sha256, "lifecycle": self.lifecycle,
            "versions": self.versions, **self.current, "comparison": self.comparison,
            "review": [reason.value for reason in self.review],
            "window": span(self.window), "comparison_window": span(self.comparison_window),
            "coverage_since": self.coverage_since.isoformat() if self.coverage_since else None, "note": NOTE,
        }


def _rate(successes: int, n: int, sufficient: bool) -> dict[str, Any]:
    if not sufficient:
        return {"state": "insufficient", "value": None, "low": None, "high": None, "n": n, "successes": successes}
    low, high = wilson(successes, n)
    return {"state": "ok", "value": successes / n, "low": low, "high": high, "n": n, "successes": successes}


def _average(values: Sequence[float], sufficient: bool) -> dict[str, Any]:
    return {"avg": sum(values) / len(values) if sufficient and values else None, "n": len(values)}


def _stats(name: str, sha256: str, runs: Sequence[Run], attempts: dict[int, Attempt] | None,
           start: datetime, end: datetime) -> dict[str, Any]:
    """One period's numbers for one skill. ``attempts`` is None where the attempts are not recorded here."""
    loaded_all = [run for run in runs if run.lists(name, sha256)]
    in_period = [run for run in loaded_all if start <= run.created_at < end]
    if attempts is None:
        units: list[list[Run]] = [[run] for run in in_period]
    else:
        by_attempt: dict[int, list[Run]] = {}
        for run in loaded_all:
            if run.attempt_id in attempts and start <= attempts[run.attempt_id].started_at < end:
                by_attempt.setdefault(run.attempt_id, []).append(run)
        units = list(by_attempt.values())
    known = [unit for unit in units if any(run.known() for run in unit)]
    invoked = sum(any(run.called(name) for run in unit if run.known()) for unit in known)
    stats: dict[str, Any] = {
        "loaded_runs": len(in_period), "loaded_attempts": None if attempts is None else len(units),
        "invoked": {"count": invoked, "n": len(known), "unknown": len(units) - len(known),
                    "rate": invoked / len(known) if len(known) >= MIN_ATTEMPTS else None},
        "effect": None,
    }
    if attempts is None:
        return stats
    loaded_ids = {run.attempt_id for run in loaded_all if run.attempt_id in attempts}
    # An attempt that loaded another version of the skill is neither with it nor a control.
    any_version = {run.attempt_id for run in runs if run.attempt_id in attempts and run.lists(name)}
    inside = [a for a in attempts.values() if start <= a.started_at < end and a.in_effect]
    with_it = [a for a in inside if a.id in loaded_ids]
    control = [a for a in inside if a.id not in any_version]
    sufficient = len(with_it) >= MIN_ATTEMPTS and len(control) >= MIN_ATTEMPTS
    passed = [a for a in with_it if a.first_pass]
    control_passed = [a for a in control if a.first_pass]
    stats["effect"] = {
        "first_pass": _rate(len(passed), len(with_it), sufficient),
        "control": _rate(len(control_passed), len(control), sufficient),
        "cost": {"loaded": _average([a.cost for a in passed if a.cost is not None], sufficient),
                 "control": _average([a.cost for a in control_passed if a.cost is not None], sufficient)},
    }
    return stats


def _divergent(effect: dict[str, Any] | None) -> bool:
    if effect is None or effect["first_pass"]["state"] != "ok":
        return False
    a, b = effect["first_pass"], effect["control"]
    return a["high"] < b["low"] or b["high"] < a["low"]


def _never_invoked(stats: dict[str, Any]) -> bool:
    invoked = stats["invoked"]
    return invoked["n"] > NEVER_INVOKED_ABOVE and invoked["count"] == 0


def _reasons(name: str, sha256: str, created: datetime, runs: Sequence[Run], now: datetime,
             periods: Sequence[dict[str, Any]]) -> list[ReviewReason]:
    reasons: list[ReviewReason] = []
    if any(_never_invoked(stats) for stats in periods):
        reasons.append(ReviewReason.NEVER_INVOKED)
    # Stale: a runtime that can tell had it loaded in the last 90 days and none of those runs called it, and the
    # skill is old enough for that to have been 90 days of chances.
    lately = [run for run in runs if run.known() and run.lists(name) and run.created_at >= now - STALE_AFTER]
    if lately and created <= now - STALE_AFTER and not any(run.called(name) for run in lately):
        reasons.append(ReviewReason.STALE)
    if any(_divergent(stats["effect"]) for stats in periods):
        reasons.append(ReviewReason.DIVERGENT)
    return reasons


def _items(raw: Any) -> tuple[dict[str, Any], ...]:
    """A record's ``library.items`` (the driver hands JSONB over as text)."""
    try:
        listed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ()
    return tuple(item for item in listed if isinstance(item, dict)) if isinstance(listed, list) else ()


async def usage(project_id: str, *, now: datetime) -> list[SkillUsage]:
    """One row per project-owned skill, for its current content hash, over the last 7 days and the 28 before."""
    skills = await store.owned_skills(project_id)
    if not skills:
        return []
    window = (now - WINDOW, now)
    comparison = (now - WINDOW - COMPARISON, now - WINDOW)
    tracked = await store.has_attempts()
    rows = await store.loaded_runs(project_id, now - STALE_AFTER, now, attempts=tracked)
    known_ids = [row["id"] for row in rows if row["runtime"] in CALL_RUNTIMES]
    calls = await store.skill_calls(known_ids) if known_ids else {}
    runs = [Run(row["id"], row["attempt_id"], row["created_at"], row["runtime"], _items(row["items"]),
                frozenset(calls.get(row["id"], ())))
            for row in rows]
    attempts: dict[int, Attempt] | None = None
    coverage = None
    if tracked:
        found = await store.attempts(project_id, comparison[0], now)
        attempts = {row["id"]: Attempt(row["id"], row["trigger"], row["decision"], row["started_at"], int(row["tries"]),
                                       None if row["cost"] is None else float(row["cost"])) for row in found}
        coverage = await store.coverage_since(project_id)
    hashes = await store.version_hashes([skill["id"] for skill in skills])
    found_usage: list[SkillUsage] = []
    for skill in skills:
        name, sha256 = skill["name"], skill["sha256"]
        current = _stats(name, sha256, runs, attempts, *window)
        previous = _stats(name, sha256, runs, attempts, *comparison)
        found_usage.append(SkillUsage(
            skill_id=skill["id"], name=name, sha256=sha256, lifecycle=skill["lifecycle"],
            versions=[number for number, digest in hashes.get(skill["id"], ()) if digest == sha256],
            review=_reasons(name, sha256, skill["imported_at"], runs, now, (current, previous)),
            window=window, comparison_window=comparison, current=current, comparison=previous,
            coverage_since=coverage))
    return found_usage


async def view(project_id: str) -> dict[str, Any]:
    """``usage`` as the API shows it, for an existing project."""
    from datetime import timezone

    from ..db import projects
    from .project_skills import ProjectSkillNotFound

    if await projects.get_project(project_id) is None:
        raise ProjectSkillNotFound(f"project {project_id!r} not found")
    return {"skills": [row.view() for row in await usage(project_id, now=datetime.now(timezone.utc))]}
