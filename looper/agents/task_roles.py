"""The roles a task's Planner defines for its steps (Planner-defined roles RFC, R1).

A role is an Agent Profile, a role prompt and a harness. The Planner writes
the role prompt and the harness with its tool (``ops.planner``,
``looper_task_role_set``); the profile may only come from its project's
allowed list. Each definition is a new version of the role
(``db.task_roles``), and a step's attempt runs the version current when it
starts (``TaskRole.payload``, carried in its ``WORK_ITEM`` or
``VERIFY_REQUEST``).

- ``RoleKind.WORK``: a step's Worker runs as the role, in the built-in
  Worker's place (``WORK_CONTRACT``, the ``worker`` row's timeout, turns,
  installs and concurrency);
- ``RoleKind.REVIEW``: once the built-in Reviewer passed a step, a Reviewer of
  the role reviews it too, and both must pass (``REVIEW_CONTRACT``, the
  ``reviewer`` row's).

A role never changes the contract, the tools or the permissions; every run of
one ends with the boundaries (``agent_settings.Boundaries``), which its own
harness replaces nothing of (``system_prompt.build_role_prompt``). A project
lets its Planners define roles only with ``planner_roles`` ``on``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .agent_settings import AgentType, SettingsError, harness_prompt, sha256_text

# Like a step's id: a short label of letters, digits, '-', '_' or '.'.
ROLE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
# A role is never named after a built-in one.
RESERVED_KEYS = frozenset({"worker", "reviewer", "planner", "manager"})


class RoleKind(str, Enum):
    WORK = "work"
    REVIEW = "review"

    @property
    def agent(self) -> AgentType:
        """The built-in role whose place, contract and settings a run of this kind takes."""
        return AgentType.WORKER if self is RoleKind.WORK else AgentType.REVIEWER

    @property
    def noun(self) -> str:
        return "work role" if self is RoleKind.WORK else "review role"


class RoleError(ValueError):
    """A role definition Looper refuses; the message says how to fix it."""


def role_key(raw: Any) -> str:
    """A role's key: ``ROLE_KEY``, not a built-in role's name."""
    if not isinstance(raw, str) or not ROLE_KEY.fullmatch(raw):
        raise RoleError(f"id must be a short label of letters, digits, '-', '_' or '.' (at most 32), starting with "
                        f"a letter or digit; got {str(raw)[:40]!r}")
    if raw.casefold() in RESERVED_KEYS:
        raise RoleError(f"{raw!r} names a built-in role; give your role another id")
    return raw


def role_text(raw: Any, field: str, limit: int) -> str:
    """A role's prompt or harness, as stored (without the blank lines around it), at most ``limit`` characters;
    ``''`` for none."""
    try:
        text = harness_prompt(raw, field=field)
    except SettingsError as exc:
        raise RoleError(str(exc)) from None
    if len(text) > limit:
        raise RoleError(f"{field} has {len(text)} characters; this project allows at most {limit} "
                        "(its role_text_max_chars, in the project's Execution settings)")
    return text


@dataclass(frozen=True)
class TaskRole:
    """One version of a role a task's Planner defined."""

    key: str
    kind: RoleKind
    version: int
    role_prompt: str
    harness: str | None = None  # None: the working-style part of the harness that applies
    profile_id: str | None = None  # None: the profile the step's built-in role of this kind would get

    @property
    def label(self) -> str:
        return f"{self.key} v{self.version}"

    def payload(self) -> dict[str, Any]:
        """The version a run starts with, as its dispatch carries it (``task_role``)."""
        return {"id": self.key, "kind": self.kind.value, "version": self.version, "role_prompt": self.role_prompt,
                "harness": self.harness, "profile_id": self.profile_id}

    @classmethod
    def from_payload(cls, raw: Any) -> TaskRole | None:
        """The role a dispatch carries; None without one, or for one that does not read as a role."""
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls(str(raw["id"]), RoleKind(raw["kind"]), int(raw["version"]), str(raw["role_prompt"]),
                       raw.get("harness") or None, raw.get("profile_id") or None)
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskRole:
        """A ``task_roles`` row."""
        return cls(row["role_key"], RoleKind(row["kind"]), int(row["version"]), row["role_prompt"],
                   row.get("harness") or None, row.get("profile_id") or None)

    def defines(self, kind: RoleKind, role_prompt: str, harness: str | None, profile_id: str | None) -> bool:
        """Whether this version is already that definition (a new one would change nothing)."""
        return (self.kind, self.role_prompt, self.harness, self.profile_id) == (kind, role_prompt, harness,
                                                                                profile_id)

    def record(self) -> dict[str, Any]:
        """Its entry in a run's ``prompt_layers`` (``task_role``)."""
        return {"id": self.key, "kind": self.kind.value, "version": self.version,
                "sha256": sha256_text(self.role_prompt),
                "harness_sha256": sha256_text(self.harness) if self.harness else None}


# --- The task page's view ---


def _stats(runs: list[Mapping[str, Any]], steps: list[Mapping[str, Any]], reviews: list[Mapping[str, Any]],
           ) -> dict[str, Any]:
    """One role's numbers in one task: its ``runs`` (rows of ``llm_records.role_runs``), the ``steps`` it ran or
    reviewed, and the ``reviews`` of its work (the review runs that judged it; for a review role, its own runs)."""
    from .tiers.worker import RunOutcome

    def count(rows: list[Mapping[str, Any]], outcome: RunOutcome) -> int:
        return sum(int(row["runs"]) for row in rows if row["outcome"] == outcome.value)

    finished = sum(int(row["runs"]) - int(row["running"]) for row in runs)
    violations = count(runs, RunOutcome.CONTRACT)
    passed, not_passed = count(reviews, RunOutcome.PASSED), count(reviews, RunOutcome.NOT_PASSED)
    live = [step for step in steps if step["status"] != "skipped"]
    return {
        "runs": sum(int(row["runs"]) for row in runs),
        "running": sum(int(row["running"]) for row in runs),
        "cost_usd": round(sum(float(row["cost_usd"]) for row in runs), 6),
        "contract_violations": violations,
        "contract_violation_rate": round(violations / finished, 4) if finished else None,
        "steps": len(live),
        "accepted": sum(1 for step in live if step["status"] == "verified"),
        "failed": sum(1 for step in live if step["status"] == "failed"),
        "retries": sum(int(step.get("retries") or 0) for step in live),
        "reviews": {"passed": passed, "not_passed": not_passed},
        "review_pass_rate": round(passed / (passed + not_passed), 4) if passed + not_passed else None,
    }


async def roles_view(task_id: str, steps: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Task ``task_id``'s roles for its page and the API: each role's current version, its earlier versions, the
    steps that name it and its numbers (``roles``), and the built-in Worker's and Reviewer's numbers in the same
    task (``builtin``)."""
    from ..db import llm_records, task_roles as db_roles

    versions = await db_roles.versions(task_id)
    runs = await llm_records.role_runs(task_id)
    role_of = {step["id"]: step.get("role_key") for step in steps}

    def runs_of(role: AgentType, key: str) -> list[Mapping[str, Any]]:
        return [row for row in runs if row["role"] == role.value and row["task_role"] == key]

    def builtin_reviews(work_key: str | None) -> list[Mapping[str, Any]]:
        """The built-in Reviewer's runs on the steps run as ``work_key`` (None: the built-in Worker)."""
        return [row for row in runs_of(AgentType.REVIEWER, "") if role_of.get(row["step_id"]) == work_key]

    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in versions:
        by_key.setdefault(row["role_key"], []).append(row)
    roles = []
    for key, rows in by_key.items():
        current, kind = rows[-1], RoleKind(rows[-1]["kind"])
        if kind is RoleKind.WORK:
            own, named = runs_of(AgentType.WORKER, key), [s for s in steps if s.get("role_key") == key]
            reviews = builtin_reviews(key)
        else:
            own, named = runs_of(AgentType.REVIEWER, key), [s for s in steps if s.get("review_role_key") == key]
            reviews = own
        roles.append({
            "id": key, "kind": kind.value, "version": current["version"],
            "profile": ({"profile_id": current["profile_id"], "name": current["profile_name"],
                         "allowed": bool(current["profile_allowed"])} if current["profile_id"] else None),
            "role_prompt": current["role_prompt"], "harness": current["harness"], "reason": current["reason"],
            "created_at": current["created_at"],
            "versions": [{"version": row["version"], "created_at": row["created_at"], "reason": row["reason"],
                          "sha256": sha256_text(row["role_prompt"])} for row in rows],
            "steps": [s.get("step_key") or s["id"] for s in named if s["status"] != "skipped"],
            "stats": _stats(own, named, reviews),
        })
    return {
        "roles": roles,
        "builtin": {
            AgentType.WORKER.value: _stats(runs_of(AgentType.WORKER, ""),
                                           [s for s in steps if not s.get("role_key")], builtin_reviews(None)),
            AgentType.REVIEWER.value: _stats(runs_of(AgentType.REVIEWER, ""), [s for s in steps if s.get("review")],
                                             runs_of(AgentType.REVIEWER, "")),
        },
    }
