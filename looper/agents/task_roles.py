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

A version records who wrote it (``Via``): the Planner, or the user on the task
page (``role_api.edit_task_role``), whose current version the Planner may not
change; and the project role template it was copied from (``role_templates``).
A role may also name catalog items its runs load besides its kind's built-in
role's installs (``items``): only items installed for such roles
(``catalog.TASK_ROLE``), the operator's consent as the profile list is. The
checks a definition passes, whoever writes it, are ``check_definition``,
``allowed_profile`` and ``check_items``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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


class Via(str, Enum):
    """Who wrote a version of a task role."""

    PLANNER = "planner"
    USER = "user"


class RoleError(ValueError):
    """A role definition Looper refuses; the message says how to fix it."""


class RoleForbidden(RoleError):
    """A definition its project's consent refuses: a profile its list does not hold."""


# What a template's description may hold: a line for the Planner.
DESCRIPTION_MAX_CHARS = 300


def check_definition(kind: Any, role_prompt: Any, harness: Any, limit: int) -> tuple[RoleKind, str, str | None]:
    """A role's kind and its two texts as stored, each at most ``limit`` characters; raises ``RoleError``."""
    try:
        checked = RoleKind(kind)
    except ValueError:
        raise RoleError(f"kind must be one of {', '.join(k.value for k in RoleKind)}; got {str(kind)[:40]!r}") from None
    prompt = role_text(role_prompt, "role_prompt", limit) if role_prompt is not None else ""
    if not prompt:
        raise RoleError("role_prompt is required: what the role is responsible for and how it works")
    return checked, prompt, role_text(harness or "", "harness", limit) or None


async def allowed_profile(project_id: str | None, wanted: str, kind: RoleKind) -> Mapping[str, Any]:
    """The Agent Profile ``wanted`` (an id or a name) for a role of ``kind`` in project ``project_id``: one on the
    project's list (``RoleForbidden`` otherwise) that can run the kind (``RoleError`` otherwise)."""
    from ..db import projects as db_projects
    from .profile_registry import RegistryError, check_selection

    allowed = await db_projects.allowed_profiles(project_id) if project_id else []
    if not allowed:
        raise RoleForbidden("the project lets no role choose an Agent Profile (its list is empty): leave profile_id "
                            "out")
    profile = next((p for p in allowed if p["id"] == wanted), None) or next(
        (p for p in allowed if str(p["name"]).casefold() == str(wanted).strip().casefold()), None)
    if profile is None:
        raise RoleForbidden(f"{wanted!r} is not on the project's list; you may choose: "
                            f"{', '.join(p['name'] for p in allowed)}")
    try:
        await check_selection(profile["id"], [kind.agent.value])
    except RegistryError as exc:
        raise RoleError(str(exc)) from None
    return profile


# The most catalog items a role names.
ITEMS_MAX = 20


def role_item_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """An item a role may name, as the Planner and the dashboard read it (``db.library.list_role_items``)."""
    return {"id": row["id"], "kind": row["kind"], "name": row["name"],
            "description": " ".join(str(row.get("description") or "").split())[:200]}


async def named_items(ids: Sequence[str]) -> list[dict[str, Any]]:
    """The catalog items ``ids`` names, in that order, as ``{id, kind, name}`` (kind and name None for one the catalog
    no longer has)."""
    from ..db import library as db_library

    if not ids:
        return []
    known = {row["id"]: row for row in await db_library.list_items()}
    return [{"id": item_id, **{key: (known.get(item_id) or {}).get(key) for key in ("kind", "name")}}
            for item_id in ids]


async def role_items(project_id: str | None) -> list[dict[str, Any]]:
    """The catalog items a role of project ``project_id`` may name: those installed for such roles, global or in
    the project, each once."""
    from ..db import library as db_library

    rows = await db_library.list_role_items(project_id) if project_id else []
    return list({row["id"]: role_item_view(row) for row in rows}.values())


async def check_items(project_id: str | None, raw: Any) -> tuple[str, ...]:
    """The items a role names (``items``: ids, or names of one item each), as their ids; raises ``RoleError`` for a
    malformed list and ``RoleForbidden`` for an item not installed for roles in the project."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise RoleError("items must be a list of catalog items, by id or name (looper_task_roles lists role_items)")
    if len(raw) > ITEMS_MAX:
        raise RoleError(f"items names {len(raw)} items; a role names at most {ITEMS_MAX}")
    if not raw:
        return ()
    available = await role_items(project_id)
    ids = []
    for wanted in raw:
        matches = [item for item in available if wanted in (item["id"], item["name"])]
        if len(matches) != 1:
            listed = ", ".join(f"{item['name']} ({item['kind']}, {item['id']})" for item in available)
            raise RoleForbidden(f"{wanted!r} is not " + ("one item " if matches else "") + "installed for "
                                "Planner-defined roles in this project; " + (
                                    f"you may name: {listed}" if listed else "none is, so leave items out"))
        ids.append(matches[0]["id"])
    if len(set(ids)) != len(ids):
        raise RoleError("items names an item twice")
    return tuple(ids)


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
    items: tuple[str, ...] = ()  # catalog item ids its runs load besides its kind's installs

    @property
    def label(self) -> str:
        return f"{self.key} v{self.version}"

    def payload(self) -> dict[str, Any]:
        """The version a run starts with, as its dispatch carries it (``task_role``)."""
        return {"id": self.key, "kind": self.kind.value, "version": self.version, "role_prompt": self.role_prompt,
                "harness": self.harness, "profile_id": self.profile_id, "items": list(self.items)}

    @classmethod
    def from_payload(cls, raw: Any) -> TaskRole | None:
        """The role a dispatch carries; None without one, or for one that does not read as a role."""
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls(str(raw["id"]), RoleKind(raw["kind"]), int(raw["version"]), str(raw["role_prompt"]),
                       raw.get("harness") or None, raw.get("profile_id") or None,
                       tuple(str(item) for item in raw.get("items") or ()))
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskRole:
        """A ``task_roles`` row."""
        return cls(row["role_key"], RoleKind(row["kind"]), int(row["version"]), row["role_prompt"],
                   row.get("harness") or None, row.get("profile_id") or None, tuple(row.get("items") or ()))

    def defines(self, kind: RoleKind, role_prompt: str, harness: str | None, profile_id: str | None,
                items: Sequence[str] = ()) -> bool:
        """Whether this version is already that definition (a new one would change nothing)."""
        return (self.kind, self.role_prompt, self.harness, self.profile_id, self.items) == (
            kind, role_prompt, harness, profile_id, tuple(items))

    def record(self) -> dict[str, Any]:
        """Its entry in a run's ``prompt_layers`` (``task_role``)."""
        return {"id": self.key, "kind": self.kind.value, "version": self.version,
                "sha256": sha256_text(self.role_prompt),
                "harness_sha256": sha256_text(self.harness) if self.harness else None}


# --- A run's role, wherever a run is shown ---


def run_role(raw: Any) -> dict[str, Any] | None:
    """The role a Worker or Reviewer run runs as, as the API shows it (``{id, kind, version}``): read from what the
    run recorded (its record's ``prompt_layers.task_role``, ``TaskRole.record``) or what its dispatch carries
    (``TaskRole.payload``); None for a run of the built-in role."""
    if not isinstance(raw, Mapping):
        return None
    try:
        return {"id": str(raw["id"]), "kind": RoleKind(raw["kind"]).value, "version": int(raw["version"])}
    except (KeyError, TypeError, ValueError):
        return None


def record_role(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """``run_role`` of an ``llm_records`` row."""
    return run_role(((record.get("metadata") or {}).get("prompt_layers") or {}).get("task_role"))


def review_role_keys(step: Mapping[str, Any]) -> list[str]:
    """The review roles a ``task_steps`` row names, in order."""
    return [str(item["role"]) for item in step.get("review_roles") or [] if isinstance(item, Mapping)]


def _reviewing(step: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The review role item whose review a ``task_steps`` row waits for; None while it waits for no role's."""
    items, position = step.get("review_roles") or [], step.get("review_position")
    if step.get("review_stage") != "role" or not isinstance(position, int) or not 0 <= position < len(items):
        return None
    return items[position]


def run_roles(runs: list[Mapping[str, Any]], steps: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The role each of a task's Worker and Reviewer runs ran as, by agent id, for the runs that ran as one: from
    their records (``llm_records.list_task_runs``), else, for a step's attempt not recorded yet (waiting for room),
    from the versions its step took when it was dispatched."""
    roles: dict[str, dict[str, Any]] = {}
    for step in steps:
        reviewing = _reviewing(step) or {}
        snapshots = ((step.get("worker_id"), step.get("role_key"), RoleKind.WORK, step.get("role_version")),
                     (step.get("reviewer_id") if reviewing else None, reviewing.get("role"), RoleKind.REVIEW,
                      reviewing.get("version")))
        for agent_id, key, kind, version in snapshots:
            if agent_id and key and (role := run_role({"id": key, "kind": kind.value, "version": version})):
                roles[agent_id] = role
    for run in runs:
        if (role := run_role(run.get("task_role"))) is not None:
            roles[run["agent_id"]] = role
    return roles


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


@dataclass
class _Inputs:
    """What one role's numbers are counted from: its runs, the steps it ran or reviewed, and the reviews of its
    work (``_stats``)."""

    runs: list[Mapping[str, Any]] = field(default_factory=list)
    steps: list[Mapping[str, Any]] = field(default_factory=list)
    reviews: list[Mapping[str, Any]] = field(default_factory=list)

    def add(self, other: _Inputs) -> None:
        self.runs += other.runs
        self.steps += other.steps
        self.reviews += other.reviews

    def stats(self) -> dict[str, Any]:
        return _stats(self.runs, self.steps, self.reviews)


def _task_inputs(versions: list[Mapping[str, Any]], runs: list[Mapping[str, Any]], steps: list[Mapping[str, Any]],
                 ) -> tuple[dict[str, tuple[list[Mapping[str, Any]], _Inputs]], dict[str, _Inputs]]:
    """One task's roles by key (their versions, oldest first, and what their numbers count), and the built-in
    Worker's and Reviewer's inputs by role."""
    role_of = {step["id"]: step.get("role_key") for step in steps}

    def runs_of(role: AgentType, key: str) -> list[Mapping[str, Any]]:
        return [row for row in runs if row["role"] == role.value and row["task_role"] == key]

    def builtin_reviews(work_key: str | None) -> list[Mapping[str, Any]]:
        """The built-in Reviewer's runs on the steps run as ``work_key`` (None: the built-in Worker)."""
        return [row for row in runs_of(AgentType.REVIEWER, "") if role_of.get(row["step_id"]) == work_key]

    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in versions:
        by_key.setdefault(row["role_key"], []).append(row)
    roles = {}
    for key, rows in by_key.items():
        if RoleKind(rows[-1]["kind"]) is RoleKind.WORK:
            inputs = _Inputs(runs_of(AgentType.WORKER, key), [s for s in steps if s.get("role_key") == key],
                             builtin_reviews(key))
        else:
            own = runs_of(AgentType.REVIEWER, key)
            inputs = _Inputs(own, [s for s in steps if key in review_role_keys(s)], own)
        roles[key] = (rows, inputs)
    builtin = {
        AgentType.WORKER.value: _Inputs(runs_of(AgentType.WORKER, ""), [s for s in steps if not s.get("role_key")],
                                        builtin_reviews(None)),
        AgentType.REVIEWER.value: _Inputs(runs_of(AgentType.REVIEWER, ""), [s for s in steps if s.get("review")],
                                          runs_of(AgentType.REVIEWER, "")),
    }
    return roles, builtin


async def roles_view(task_id: str, steps: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Task ``task_id``'s roles for its page and the API: each role's current version, its earlier versions, the
    steps that name it and its numbers (``roles``), and the built-in Worker's and Reviewer's numbers in the same
    task (``builtin``)."""
    from ..db import llm_records, task_roles as db_roles

    by_key, builtin = _task_inputs(await db_roles.versions(task_id), await llm_records.role_runs(task_id), steps)
    roles = []
    for key, (rows, inputs) in by_key.items():
        current, kind = rows[-1], RoleKind(rows[-1]["kind"])
        roles.append({
            "id": key, "kind": kind.value, "version": current["version"],
            "profile": ({"profile_id": current["profile_id"], "name": current["profile_name"],
                         "allowed": bool(current["profile_allowed"])} if current["profile_id"] else None),
            "role_prompt": current["role_prompt"], "harness": current["harness"], "reason": current["reason"],
            "created_at": current["created_at"],
            # Who wrote the current version (the Planner, or the user, whose definition stands), and the template
            # it was copied from.
            "via": current.get("via") or Via.PLANNER.value, "template": current.get("template_key"),
            "items": await named_items(current.get("items") or ()),
            "versions": [{"version": row["version"], "created_at": row["created_at"], "reason": row["reason"],
                          "via": row.get("via") or Via.PLANNER.value, "template": row.get("template_key"),
                          "sha256": sha256_text(row["role_prompt"])} for row in rows],
            "steps": [s.get("step_key") or s["id"] for s in inputs.steps if s["status"] != "skipped"],
            "stats": inputs.stats(),
        })
    return {"roles": roles, "builtin": {role: inputs.stats() for role, inputs in builtin.items()}}


async def project_role_stats(project_id: str, since: datetime | None = None) -> dict[str, Any]:
    """The numbers of the roles the Planners of project ``project_id``'s tasks defined, summed by role id across its
    tasks with roles (created ``since``, when given): the tasks each appears in and the templates it was copied
    from (one id may be another definition in each task), and the built-in Worker's and Reviewer's numbers in the
    same tasks to compare them with."""
    from ..db import llm_records, steps as db_steps, task_roles as db_roles

    task_ids = await db_roles.project_tasks(project_id, since)
    roles: dict[str, dict[str, Any]] = {}
    builtin = {role.value: _Inputs() for role in (AgentType.WORKER, AgentType.REVIEWER)}
    for task_id in task_ids:
        by_key, task_builtin = _task_inputs(await db_roles.versions(task_id), await llm_records.role_runs(task_id),
                                            await db_steps.list_steps(task_id))
        for key, (rows, inputs) in by_key.items():
            entry = roles.setdefault(key, {"id": key, "kinds": set(), "tasks": 0, "templates": set(),
                                           "inputs": _Inputs()})
            entry["kinds"].add(rows[-1]["kind"])
            entry["tasks"] += 1
            entry["templates"] |= {row["template_key"] for row in rows if row.get("template_key")}
            entry["inputs"].add(inputs)
        for role, inputs in task_builtin.items():
            builtin[role].add(inputs)
    return {
        "project_id": project_id, "tasks": len(task_ids),
        "roles": [{"id": entry["id"], "kinds": sorted(entry["kinds"]), "tasks": entry["tasks"],
                   "templates": sorted(entry["templates"]), "stats": entry["inputs"].stats()}
                  for entry in sorted(roles.values(), key=lambda e: (-e["tasks"], e["id"]))],
        "builtin": {role: inputs.stats() for role, inputs in builtin.items()},
    }
