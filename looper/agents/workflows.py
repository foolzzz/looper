"""Operator-owned workflows, validated before storage and snapshotted when a task is created."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .agent_settings import SettingsError, budget_usd
from .task_roles import RoleError, RoleKind, check_definition, role_key
from .task_templates import KEY_PATTERN
from .tiers import conditions


class WorkflowError(ValueError):
    """A workflow definition the operator must correct."""


@dataclass(frozen=True)
class WorkflowRole:
    key: str
    kind: RoleKind
    role_prompt: str
    harness: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True)
class ReviewPanel:
    members: tuple[str, ...]
    accept_when: conditions.Condition


@dataclass(frozen=True)
class Verification:
    role: str
    max_rounds: int


@dataclass(frozen=True)
class Workflow:
    key: str
    name: str
    description: str = ""
    planner_instructions: str = ""
    profiles: Mapping[str, str] = field(default_factory=dict)
    roles: tuple[WorkflowRole, ...] = ()
    work_role: str | None = None
    review_panel: ReviewPanel | None = None
    max_step_retries: int | None = None
    review_all: bool = False
    dependency_conditions: bool = False
    budget_usd: float | None = None
    distinct_reviewer_runtime: bool = False
    adjudication: bool = False
    verification: Verification | None = None
    escalate_on_exhaustion: bool = False
    phase_notices: bool = False

    def snapshot(self) -> dict[str, Any]:
        raw = asdict(self)
        for role in raw["roles"]:
            role["kind"] = role["kind"].value
        if self.review_panel:
            raw["review_panel"] = {"members": list(self.review_panel.members),
                                   "accept_when": conditions.to_json(self.review_panel.accept_when, lambda key: key)}
        raw["roles"] = list(raw["roles"])
        return raw

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Workflow:
        raw = row.get("definition") or {}
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        return cls.from_dict({**raw, **{key: row[key] for key in ("key", "name", "description")}})

    @classmethod
    def from_dict(cls, raw: Any) -> Workflow:
        if not isinstance(raw, dict) or set(raw) - cls.__dataclass_fields__.keys():
            raise WorkflowError("workflow must be an object containing only workflow fields")
        data = dict(raw)
        key = data.get("key")
        if not isinstance(key, str) or not re.fullmatch(KEY_PATTERN, key) or key == "none":
            raise WorkflowError(f"key must match {KEY_PATTERN} and cannot be 'none'")
        for name, limit, required in (("name", 60, True), ("description", 300, False),
                                      ("planner_instructions", 8000, False)):
            text = data.get(name, "")
            if not isinstance(text, str) or len(text) > limit or required and not text.strip():
                raise WorkflowError(f"{name} must be {'a nonempty' if required else 'a'} string of at most {limit} characters")
            if name == "name" and ("\n" in text or "\r" in text):
                raise WorkflowError("name must be one line")
            data[name] = text.strip()
        for name in ("review_all", "dependency_conditions", "distinct_reviewer_runtime", "adjudication",
                     "escalate_on_exhaustion", "phase_notices"):
            if name in data and type(data[name]) is not bool:
                raise WorkflowError(f"{name} must be true or false")
        retries = data.get("max_step_retries")
        if retries is not None and (type(retries) is not int or not 0 <= retries <= 20):
            raise WorkflowError("max_step_retries must be a whole number from 0 to 20 or null")
        budget = data.get("budget_usd")
        if budget is not None:
            try:
                data["budget_usd"] = budget_usd(budget, field="budget_usd")
            except SettingsError as exc:
                raise WorkflowError(str(exc)) from None
        profiles = data.get("profiles", {})
        if (not isinstance(profiles, dict) or set(profiles) - {"planner", "worker", "reviewer"}
                or any(not isinstance(value, str) or not value for value in profiles.values())):
            raise WorkflowError("profiles must map planner, worker or reviewer to profile ids")
        roles = data.get("roles", [])
        if not isinstance(roles, list) or len(roles) > 8:
            raise WorkflowError("roles must be a list of at most 8 definitions")
        checked = {}
        for item in roles:
            if not isinstance(item, dict) or set(item) - WorkflowRole.__dataclass_fields__.keys():
                raise WorkflowError("roles must contain role definitions")
            try:
                role = role_key(item.get("key"))
                kind, prompt, harness = check_definition(item.get("kind"), item.get("role_prompt"), item.get("harness"), 8000)
            except RoleError as exc:
                raise WorkflowError(str(exc)) from None
            profile = item.get("profile_id")
            if profile is not None and (not isinstance(profile, str) or not profile):
                raise WorkflowError("role profile_id must be a profile id or null")
            if role in checked:
                raise WorkflowError(f"role {role!r} is listed twice")
            checked[role] = WorkflowRole(role, kind, prompt, harness, profile)
        data["roles"] = tuple(checked.values())

        def reference(key: Any, kind: RoleKind) -> None:
            if not isinstance(key, str) or key not in checked or checked[key].kind is not kind:
                raise WorkflowError(f"{key!r} must name a workflow {kind.value} role")

        if data.get("work_role") is not None:
            reference(data["work_role"], RoleKind.WORK)
        panel = data.get("review_panel")
        if panel is not None:
            if not isinstance(panel, dict) or set(panel) != {"members", "accept_when"}:
                raise WorkflowError("review_panel needs members and accept_when")
            members = panel["members"]
            if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
                raise WorkflowError("review_panel.members must be a nonempty list of review roles or builtin")
            if len(set(members)) != len(members):
                raise WorkflowError("review_panel.members lists a member twice")
            for member in members:
                if member != "builtin":
                    reference(member, RoleKind.REVIEW)
            from .tiers.planner_protocol import ProtocolError
            try:
                accept = conditions.parse(panel["accept_when"], "review_panel.accept_when")
            except ProtocolError as exc:
                raise WorkflowError(str(exc)) from None
            if not conditions.leaves(accept) or not conditions.leaves(accept) <= set(members):
                raise WorkflowError("accept_when must contain only panel members and at least one leaf")

            def passed(cond: conditions.Condition) -> conditions.Condition:
                if isinstance(cond, conditions.Leaf):
                    if not cond.shorthand and cond.on is not conditions.On.PASSED:
                        raise WorkflowError("accept_when leaves must test passed")
                    return conditions.Leaf(cond.step, conditions.On.PASSED, False)
                return conditions.KOf(cond.k, tuple(passed(child) for child in cond.of), cond.form)

            data["review_panel"] = ReviewPanel(tuple(members), passed(accept))
            if not data.get("distinct_reviewer_runtime"):
                raise WorkflowError("review_panel requires distinct_reviewer_runtime to be true")
        verification = data.get("verification")
        if verification is not None:
            if not isinstance(verification, dict) or set(verification) != {"role", "max_rounds"}:
                raise WorkflowError("verification needs role and max_rounds")
            reference(verification["role"], RoleKind.WORK)
            rounds = verification["max_rounds"]
            if type(rounds) is not int or not 1 <= rounds <= 5:
                raise WorkflowError("verification.max_rounds must be a whole number from 1 to 5")
            data["verification"] = Verification(verification["role"], rounds)
        return cls(**data)


async def check_profiles(workflow: Workflow, *, pool: Any = None) -> None:
    from .profile_registry import RegistryError, check_selection

    selected = {}
    try:
        for agent, profile_id in workflow.profiles.items():
            selected[agent] = await check_selection(profile_id, [agent], pool=pool)
        for role in workflow.roles:
            if role.profile_id:
                selected[role.key] = await check_selection(role.profile_id, [role.kind.agent.value], pool=pool)
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from None
    if workflow.distinct_reviewer_runtime:
        work = selected.get("worker") or selected.get(workflow.work_role)
        members = workflow.review_panel.members if workflow.review_panel else ("builtin",)
        for member in members:
            review = selected.get("reviewer") or selected.get(member)
            if work and review and work.runtime.id == review.runtime.id:
                raise WorkflowError(f"review member {member!r} must use a different runtime from the work role")
