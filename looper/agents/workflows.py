"""Operator-owned workflows, validated before storage and snapshotted when a task is created."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .agent_settings import AgentSettings, AgentType, Layer, SettingsError, budget_usd
from .task_roles import RoleError, RoleKind, check_definition, role_key
from .task_templates import KEY_PATTERN
from .tiers import conditions
from .workflow_reporting import Phase as Phase, phase_text as phase_text, process_record as process_record


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


def _pass_rule(condition: conditions.Condition) -> str:
    if isinstance(condition, conditions.Leaf):
        return f"{condition.step} 通过"
    children = "、".join(_pass_rule(child) for child in condition.of)
    rule = ("全部通过" if condition.k == len(condition.of) else "至少一个通过" if condition.k == 1
            else f"至少 {condition.k} 项通过")
    return f"{rule}（{children}）"


async def configuration_lines(workflow: Mapping[str, Any], settings: Mapping[AgentType, AgentSettings], *,
                              review_all: bool) -> list[str]:
    """The same effective workflow roles and acceptance rule for task confirmation and form preview."""
    from ..db import profile_registry as db_profiles
    from .profile_registry import BUILTIN_PROFILE, ResolvedAgentProfile, builtin_resolved_profile

    cache: dict[str, ResolvedAgentProfile | None] = {}

    async def profile(profile_id: str) -> ResolvedAgentProfile | None:
        if profile_id not in cache:
            row = await db_profiles.get_profile(profile_id)
            cache[profile_id] = (ResolvedAgentProfile.from_row(row) if row else
                                 builtin_resolved_profile() if profile_id == BUILTIN_PROFILE.id else None)
        return cache[profile_id]

    async def describe(profile_id: str) -> str:
        chain, seen = [], set()
        # Execution permits at most three fallback hops and never revisits a profile.
        while profile_id and profile_id not in seen and len(chain) < 4:
            seen.add(profile_id)
            selected = await profile(profile_id)
            if selected is None:
                chain.append(f"Profile {profile_id}（不可用）")
                break
            effort = f"，effort {selected.effort}" if selected.effort else ""
            chain.append(f"{selected.name}（{selected.runtime.type}/{selected.model}{effort}）")
            profile_id = selected.fallback_profile_id
        return "，回落至 ".join(chain)

    async def role_line(key: str, agent: AgentType, pinned: str | None = None) -> str:
        selected = ((workflow.get("profiles") or {}).get(agent.value)
                    or (settings[agent].profile.id if settings[agent].profile_source is Layer.TASK else None)
                    or pinned or settings[agent].profile.id)
        verification = workflow.get("verification") or {}
        purpose = (f"；验证：只读，最多 {verification['max_rounds']} 轮" if key == verification.get("role") else "")
        return f"- {key}：{await describe(selected)}{purpose}"

    lines = [await role_line("Planner", AgentType.PLANNER)]
    for role in workflow.get("roles") or []:
        agent = AgentType.WORKER if role["kind"] == "work" else AgentType.REVIEWER
        lines.append(await role_line(role["key"], agent, role.get("profile_id")))
    if not workflow.get("work_role"):
        lines.append(await role_line("工作角色", AgentType.WORKER))
    panel = workflow.get("review_panel")
    if not panel or "builtin" in panel["members"]:
        lines.append(await role_line("内置评审席位", AgentType.REVIEWER))
    if panel:
        accept = conditions.parse(panel["accept_when"], "review_panel.accept_when")
        required = conditions.leaves(accept)
        seats = "、".join(member for member in panel["members"] if member in required)
        suggestions = "、".join(member for member in panel["members"] if member not in required) or "无"
        scope = "每个工作步骤" if review_all else "由 Planner 决定步骤"
        lines.append(f"- 评审面板（{scope}）：必需席位：{seats}；建议席位：{suggestions}；验收：{_pass_rule(accept)}")
    else:
        lines.append("- 评审：每一步都要 Reviewer 评审通过" if review_all
                     else "- 评审：由 Planner 决定哪些步骤要 Reviewer 评审")
    return lines
