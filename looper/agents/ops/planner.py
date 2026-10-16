"""The Planner's platform tools: one task's Planner chooses its Workers' and Reviewers' Agent Profiles and changes
its task's or its project's concurrency (Manager tools RFC, section 8.3, decisions 9 and 10).

``looper serve`` makes one ``PlannerOps``. A Planner gets a token of its own
when it starts (``issue``), good for its task only, and its MCP entry carries
it as the Manager's does (``library_with_server``: ``looper mcp-server`` with
``LOOPER_AGENT_URL`` and ``LOOPER_AGENT_TOKEN``). The token stops working when
that Planner stops (``revoke``: its task ended, or a new Planner replaced it)
and once its task has ended. Tokens start with ``TOKEN_PREFIX``, so the
endpoint (``/api/agent/v1/``, ``http_server``) tells a Planner's from the
Manager's before it checks one; they are kept in memory as hashes and never
logged.

- **Scope.** A call acts on its token's task: one naming another task is
  ``forbidden``, and so is any call once the task has ended. The Manager's
  tools are not the Planner's (``forbidden``), nor the Planner's the Manager's.
- **Turns.** ``read`` runs in any turn; ``write`` only in a turn Looper sent
  the Planner (``begin_turn``/``end_turn``), never in one its Claude runs on
  its own (after background work ends).
- **Profiles.** ``looper_task_profile_set`` chooses, from the project's list
  (``project_allowed_profiles``, empty by default), the profile of the task's
  Workers or Reviewers for the rest of the task or for one step, and writes a
  ``profile_choice`` journal entry with it (``task_planner_profiles``). The
  WorkerPool reads it when the role's next run starts, under the user's choice
  for the task (``agent_settings.Layer.PLANNER_TASK``, ``PLANNER_STEP``), and
  only while the profile is on the task's project's list. No confirmation: the
  list is the maintainer's consent in advance.
- **Concurrency.** ``looper_task_concurrency`` changes the task's own Worker
  and Reviewer limits and ``looper_project_concurrency`` its project's,
  through the Manager's code (``concurrency.set_task_limits``,
  ``set_project_limits``), at once; each change is a ``concurrency_change``
  journal entry, and a task asked for in Telegram or Lark tells that chat
  (``task_notice``). Two tools, not one with a scope: each lists only the
  roles it takes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from ...db import activity as db_activity, agent_setting_layers as db_layers, plans as db_plans
from ...db import projects as db_projects, steps as db_steps, tasks as db_tasks
from ..agent_settings import CONCURRENCY_MAX, EMPTY_LAYER, PLANNER_CHOICE_ROLES, AgentType, Layer, index_layers
from ..agent_settings import resolve
from ..links import task_link
from ..message import MessageEnvelope, MessageType, channel_address, planner_address
from ..projects import EDITABLE_STATUSES, allowed_profile_view
from ..task_notice import Notice, NotifyTarget, link_line, notify_channel
from ..tiers.journal import SUMMARY_MAX_CHARS, JournalKind, clip
from ..tiers.task import recheck_limits
from . import ErrorCode, OpError, Risk, Tool, check_arguments
from .concurrency import COUNTS, NOTE, limits_argument, set_project_limits, set_task_limits, shown
from .service import MANAGER_TOOLS, library_with_server, server_config

logger = logging.getLogger(__name__)

# How a Planner's token starts ('.' is not in a Manager token's alphabet).
TOKEN_PREFIX = "planner."
# A step that ended takes no choice.
_ENDED_STEPS = ("verified", "skipped", "failed")
REASON_MAX_CHARS = 500

# How the Planner uses its tools: after its reply contract, in every Planner's system prompt that has them.
TOOLS_GUIDE = """\
## Your tools (Looper's MCP server; they act on this task only)
They change settings, never the plan: your replies' JSON still decides the
steps.
- looper_task_profiles: the Agent Profiles this task's project lets you choose
  for its Workers and Reviewers (model, price per million tokens), and what
  each role runs now. The list may be empty; then you cannot choose one, and
  the Workers and Reviewers run what the settings give them.
- looper_task_profile_set: run the Workers or the Reviewers ("role") on one
  of those profiles ("profile_id"): without "step" for the rest of the task,
  with "step" (a step's id in your plan) for that step and each of its
  retries; profile_id null clears your choice. Use it when the requirement
  or the user asks for it, when a step needs a stronger model (a hard step, or
  one that failed for lack of skill), or a cheaper one for routine steps. It
  applies to the runs that start after the call: call it before the reply that
  dispatches the step (the plan, a retry, a replan); a run already started
  keeps its own. Before your plan exists only the rest of the task can be
  chosen; a step not started yet can be chosen in a later turn. You never
  choose the Planner's own profile, and a profile the user chose for this
  task wins over yours.
- looper_task_concurrency: how many of this task's Workers or Reviewers run
  at once, within the project's limits. Lower it when steps that run at the
  same time would get in each other's way (one build, one database, one port)
  or when the requirement or the user asks; raise it only when independent
  steps wait for room.
- looper_project_concurrency: the project's own limits (how many of its
  tasks, Workers and Reviewers run at once), which its other tasks share: only
  when the requirement or the user asks for the project's limits.
Both apply at once, and the user is told.
Give "reason" with every change: the task page shows it.
"""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(header: str | None) -> str:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class _Grant:
    """What one Planner's token is good for: its task, and whether Looper's turn runs now."""

    task_id: str
    in_turn: bool = False


@dataclass(frozen=True)
class PlannerCall:
    """One call of a Planner's tool: the task its token is for, its checked arguments, the platform."""

    task_id: str
    arguments: Mapping[str, Any]
    platform: Any  # service.Platform
    dashboard_url: str = ""  # where a notice links the task to


class PlannerOps:
    def __init__(self, platform: Any, *, url: str, dashboard_url: str = "",
                 tools: Sequence[Tool] | None = None) -> None:
        self._platform = platform
        self._url = url.rstrip("/")
        self._dashboard_url = dashboard_url
        self._tools = {tool.name: tool for tool in (TOOLS if tools is None else tools)}
        self._grants: dict[str, _Grant] = {}  # a token's hash -> what it is good for

    # --- Tokens ---

    def issue(self, task_id: str) -> str:
        """A new token for ``task_id``'s Planner; the task's earlier one stops working."""
        self._drop(task_id)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._grants[_digest(token)] = _Grant(task_id)
        return token

    def revoke(self, token: str) -> None:
        """``token`` stops working (its Planner stopped); a newer token of the same task keeps working."""
        self._grants.pop(_digest(token), None)

    def _drop(self, task_id: str) -> None:
        for digest in [d for d, grant in self._grants.items() if grant.task_id == task_id]:
            del self._grants[digest]

    @staticmethod
    def claims(header: str | None) -> bool:
        """Whether an ``Authorization`` header carries a Planner's token, working or not."""
        return _bearer(header).startswith(TOKEN_PREFIX)

    def _grant(self, header: str | None) -> _Grant | None:
        token = _bearer(header)
        return self._grants.get(_digest(token)) if token.startswith(TOKEN_PREFIX) else None

    def authorized(self, header: str | None) -> bool:
        """Whether the header carries a Planner's token that works."""
        return self._grant(header) is not None

    # --- The Planner's turns ---

    def begin_turn(self, token: str) -> None:
        """The Planner holding ``token`` runs a turn Looper sent it: its calls may change things."""
        if (grant := self._grants.get(_digest(token))) is not None:
            grant.in_turn = True

    def end_turn(self, token: str) -> None:
        if (grant := self._grants.get(_digest(token))) is not None:
            grant.in_turn = False

    # --- The endpoint ---

    def listing(self) -> list[dict[str, Any]]:
        return [tool.listing() for tool in self._tools.values()]

    async def call(self, header: str | None, name: str, raw: Any) -> dict[str, Any]:
        """Run one tool call of the Planner whose token ``header`` carries; the protocol's answer."""
        grant = self._grant(header)
        if grant is None:
            return _error(ErrorCode.FORBIDDEN, "this Planner's token no longer works: its task ended or a new "
                                               "Planner took over")
        task_id = grant.task_id
        tool = self._tools.get(name)
        if tool is None:
            manager = name in {t.name for t in MANAGER_TOOLS}
            return _error(ErrorCode.FORBIDDEN if manager else ErrorCode.INVALID,
                          f"{name} {'is a tool of the Manager' if manager else 'is not a tool'}; your tools are: "
                          f"{', '.join(self._tools)}")
        try:
            arguments = check_arguments(tool.schema, raw)
            named = arguments.pop("task_id", None)
            if named is not None and named != task_id:
                raise OpError(ErrorCode.FORBIDDEN, f"your tools act on your own task ({task_id}) only, not on {named}")
            task = await db_tasks.get_task(task_id)
            if task is None or task["status"] not in EDITABLE_STATUSES:
                self._drop(task_id)
                raise OpError(ErrorCode.FORBIDDEN, f"task {task_id} has ended: its Planner's tools no longer work")
            if tool.risk is not Risk.READ and not grant.in_turn:
                raise OpError(ErrorCode.NEEDS_USER_TURN, "Looper changes things only in a turn it sent you, not in a "
                                                         "turn you run on your own; do it in your next turn")
            result = await tool.run(PlannerCall(task_id, arguments, self._platform, self._dashboard_url))
        except OpError as exc:
            if tool.risk is not Risk.READ:
                await _activity(task_id, f"Planner's {name} refused ({exc.code.value}): {exc}")
            return _error(exc.code, str(exc))
        except Exception as exc:
            logger.exception("task %s: the Planner's call of %s failed", task_id, name)
            return _error(ErrorCode.UNAVAILABLE, f"Looper could not do it now ({type(exc).__name__}: {exc}); try "
                                                 "again later")
        return {"status": "ok", "result": json.loads(json.dumps(result, ensure_ascii=False, default=str))}

    # --- The Planner's MCP entry ---

    @property
    def guide(self) -> str:
        """How the Planner uses its tools, for its system prompt."""
        return TOOLS_GUIDE

    async def library_with_server(self, library: Any, runtime_root: Path, token: str) -> Any:
        """``library`` with Looper's MCP server for the Planner holding ``token``."""
        return await library_with_server(library, runtime_root, server_config(self._url, token), who="a Planner")


def _error(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code.value, "message": message}}


async def _activity(task_id: str, summary: str) -> None:
    try:
        await db_activity.insert_activity(task_id=task_id, worker=db_tasks.planner_agent_id(task_id), kind="phase",
                                          summary=clip(summary, 500))
    except Exception:
        logger.warning("task %s: could not record the Planner's tool call", task_id, exc_info=True)


async def _task(task_id: str) -> dict[str, Any]:
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no task {task_id}")
    return task


def _reason(call: PlannerCall) -> str:
    return " ".join(str(call.arguments.get("reason") or "").split())


def _label(role: AgentType) -> str:
    return f"{role.value.capitalize()}s"


async def _journal(task_id: str, kind: JournalKind, summary: str, step_id: str = "") -> None:
    await db_plans.save(task_id, journal=[{"step_id": step_id, "kind": kind.value,
                                           "summary": clip(summary, SUMMARY_MAX_CHARS), "learnings": []}])


# --- looper_task_profiles ---


async def _profiles(call: PlannerCall) -> dict[str, Any]:
    task = await _task(call.task_id)
    project_id = task.get("project_id")
    allowed = await db_projects.allowed_profiles(project_id) if project_id else []
    layers = index_layers(await db_layers.list_layers(project_id, task_id=call.task_id), project_id)
    chosen = await db_layers.planner_profiles(call.task_id)
    roles = {}
    for role in PLANNER_CHOICE_ROLES:
        runs = resolve(role, layers, project_id)
        user = layers.get((Layer.TASK, role), EMPTY_LAYER).profile
        mine = next((c for c in chosen if c["role"] == role.value and c["step_id"] is None), None)
        roles[role.value] = {
            "runs": {"name": runs.profile.name, "source": runs.profile_source.value},
            "user_choice": user.name if user is not None else None,
            "your_choice": mine["profile"]["name"] if mine else None,
        }
    return {
        "allowed": [allowed_profile_view(row) for row in allowed],
        "roles": roles,
        "steps": [{"step": c["step_key"] or c["step_id"], "role": c["role"], "profile": c["profile"]["name"]}
                  for c in chosen if c["step_id"] is not None],
        "note": ("runs.source: task is the user's choice for this task, which yours cannot change; planner_task "
                 "(yours for the rest of the task) and planner_step (yours for one step) are yours; the others "
                 "are the settings. A step's choice wins over the rest of the task's.")
                + " A price of null means Looper does not know it, not that it is free."
                + ("" if allowed else " The project lets you choose no profile: its list is empty."),
    }


# --- looper_task_profile_set ---


async def _step(task_id: str, label: str) -> dict[str, Any]:
    """The task's step ``label`` (its id in the plan, or Looper's id of it) that has not ended."""
    steps = await db_steps.list_steps(task_id)
    named = [s for s in steps if label in (s.get("step_key"), s["id"])]
    step = next((s for s in named if s["status"] != "skipped"), named[0] if named else None)
    if step is None:
        open_steps = [s.get("step_key") or s["id"] for s in steps if s["status"] not in _ENDED_STEPS]
        raise OpError(ErrorCode.INVALID, f"no step {label!r} in this task's plan; the steps not done are: "
                                         f"{', '.join(open_steps) or 'none'}")
    if step["status"] in _ENDED_STEPS:
        raise OpError(ErrorCode.INVALID, f"step {label} has ended ({step['status']}): choose for a step that has not")
    return step


def _match(allowed: Sequence[Mapping[str, Any]], wanted: str) -> Mapping[str, Any] | None:
    return next((p for p in allowed if p["id"] == wanted), None) or next(
        (p for p in allowed if str(p["name"]).casefold() == wanted.strip().casefold()), None)


async def _profile_set(call: PlannerCall) -> dict[str, Any]:
    from ..profile_registry import RegistryError, check_selection

    arguments = call.arguments
    role = AgentType(arguments["role"])
    if "profile_id" not in arguments:
        raise OpError(ErrorCode.INVALID, "profile_id is required: an allowed profile's id or name, or null to clear "
                                         "your choice")
    task = await _task(call.task_id)
    project_id = task.get("project_id")
    allowed = await db_projects.allowed_profiles(project_id) if project_id else []
    if not allowed:
        raise OpError(ErrorCode.FORBIDDEN, "this task's project lets its Planner choose no Agent Profile (its list is "
                                           f"empty): the {_label(role)} run what the settings give them")
    users = await db_layers.profile_overrides(call.task_id)
    if role.value in users:
        raise OpError(ErrorCode.FORBIDDEN, f"the user chose {users[role.value]['name']} for this task's "
                                           f"{_label(role)}; their choice stands over yours")
    step = await _step(call.task_id, arguments["step"]) if arguments.get("step") else None
    where = f"step {step.get('step_key') or step['id']}" if step else "the rest of the task"
    wanted = arguments.get("profile_id")
    profile: Mapping[str, Any] | None = None
    if wanted is not None:
        profile = _match(allowed, wanted)
        if profile is None:
            raise OpError(ErrorCode.FORBIDDEN, f"{wanted!r} is not on the project's list; you may choose: "
                                               f"{', '.join(p['name'] for p in allowed)}")
        try:
            await check_selection(profile["id"], [role.value])
        except RegistryError as exc:
            raise OpError(ErrorCode.INVALID, str(exc)) from None
    step_id = step["id"] if step else None
    before = next((c for c in await db_layers.planner_profiles(call.task_id)
                   if c["role"] == role.value and c["step_id"] == step_id), None)
    if profile is None and before is None:
        return {"role": role.value, "for": where, "profile": None, "changed": False,
                "note": f"you had chosen nothing for the {_label(role)} of {where}"}
    reason = _reason(call)
    summary = (f"{_label(role)} of {where}: {profile['name']}" if profile is not None
               else f"{_label(role)} of {where}: the Planner's choice ({before['profile']['name']}) cleared")
    summary += f". Reason: {reason}" if reason else ""
    entry = {"step_id": step_id or "", "kind": JournalKind.PROFILE_CHOICE.value,
             "summary": clip(summary, SUMMARY_MAX_CHARS), "learnings": []}
    try:
        await db_layers.set_planner_profile(call.task_id, role.value, step_id, profile["id"] if profile else None,
                                            entry)
    except asyncpg.ForeignKeyViolationError:
        raise OpError(ErrorCode.CONFLICT, "the step or the profile was removed meanwhile; look again") from None
    await _activity(call.task_id, f"Planner chose the Agent Profile: {summary}")
    layers = index_layers(await db_layers.list_layers(project_id, task_id=call.task_id, step_id=step_id), project_id)
    runs = resolve(role, layers, project_id)
    return {"role": role.value, "for": where, "profile": profile["name"] if profile else None, "changed": True,
            "next_run": {"name": runs.profile.name, "source": runs.profile_source.value},
            "note": f"the {_label(role)}' runs that start from now on use it, a retry included; a run already "
                    "started keeps its own"}


# --- looper_task_concurrency and looper_project_concurrency ---


# What each role's limit counts, as the journal names it.
_COUNTED = {AgentType.PLANNER: "tasks", AgentType.WORKER: "Workers", AgentType.REVIEWER: "Reviewers"}


def _journal_summary(result: Mapping[str, Any], reason: str) -> str:
    """The changes, as the task journal shows them: "Workers at once in this task: 16 → 1. Reason: ..."."""
    where = f"project {result['project']}" if result["scope"] == "project" else "this task"
    changes = "; ".join(f"{_COUNTED[AgentType(c['role'])]} at once in {where}: {c['before']['value']} → "
                        f"{c['after']['value']}" for c in result["changes"])
    return changes + (f". Reason: {reason}" if reason else "")


def _notice_lines(result: Mapping[str, Any]) -> list[str]:
    """Each role's change, as the user is told it (as the Manager's confirmation shows a change)."""
    where = f"项目「{result['project']}」" if result["scope"] == "project" else "本任务"
    return [f"- {where}：{AgentType(c['role']).value.capitalize()}（{COUNTS[AgentType(c['role'])]}）"
            f"{shown(c['before'])} → {shown(c['after'])}" for c in result["changes"]]


async def _task_concurrency(call: PlannerCall) -> dict[str, Any]:
    limits = limits_argument(call)
    task = await _task(call.task_id)
    return await _changed(call, task, await set_task_limits(call.task_id, limits))


async def _project_concurrency(call: PlannerCall) -> dict[str, Any]:
    limits = limits_argument(call)
    task = await _task(call.task_id)
    project = await db_projects.get_project(task["project_id"]) if task.get("project_id") else None
    if project is None:
        raise OpError(ErrorCode.NOT_FOUND, "this task has no project")
    return await _changed(call, task, await set_project_limits(project, limits))


async def _changed(call: PlannerCall, task: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """After a limit changed: what waits checks again, the journal and the activity record it, and the task's chat is
    told."""
    await recheck_limits(call.platform.task_router, call.platform.worker_pool)
    reason = _reason(call)
    summary = _journal_summary(result, reason)
    try:
        await _journal(call.task_id, JournalKind.CONCURRENCY_CHANGE, summary)
    except Exception:
        logger.warning("task %s: could not journal the Planner's concurrency change", call.task_id, exc_info=True)
    await _activity(call.task_id, f"Planner changed the concurrency limits: {summary}")
    told = await _notify(call, task, _notice_lines(result), reason)
    return {**result, "user_told": told,
            "note": f"{NOTE} The task's journal records it{', and the user is told in the chat' if told else ''}."}


async def _notify(call: PlannerCall, task: Mapping[str, Any], lines: list[str], reason: str) -> bool:
    """Tell the chat a Telegram or Lark task came from about the change; whether there was one to tell."""
    channel, target = notify_channel(task.get("origin_channel_id")), NotifyTarget.from_columns(task)
    if channel is None or target is None:
        return False
    name = db_tasks.task_name(title=task.get("title"), goal_id=task.get("goal_id"),
                              requirement=task.get("requirement"), task_id=call.task_id)
    text = "\n".join([f"⚙️ 规划者调整了并发上限 · {clip(name, 40)}", *lines, *([f"理由：{reason}"] if reason else []),
                      link_line(task_link(call.dashboard_url, call.task_id), channel)])
    try:
        await call.platform.bus.publish(MessageEnvelope.create(
            source=planner_address(call.task_id), target=channel_address(channel.value),
            message_type=MessageType.CHAT_NOTICE, payload=Notice(channel, target, text).payload(),
            metadata={"origin_channel_id": task.get("origin_channel_id")},
        ))
    except Exception:
        logger.warning("task %s: could not tell its chat of the Planner's concurrency change", call.task_id,
                       exc_info=True)
        return False
    return True


_TASK_ID = {"type": "string", "minLength": 1, "description": "Your task's id; leave it out. Another task's is refused."}
_REASON = {"type": "string", "maxLength": REASON_MAX_CHARS,
           "description": "Why, in a sentence; the task page shows it."}
_ROLE = {"type": "string", "enum": [role.value for role in PLANNER_CHOICE_ROLES],
         "description": "worker or reviewer; never the Planner's own profile."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX}


def _limits(names: str, roles: Sequence[AgentType]) -> dict[str, Any]:
    return {"type": "object", "properties": {role.value: _LIMIT for role in roles},
            "description": f"The new limit of each role to change ({names}), from 1 to {CONCURRENCY_MAX}; null clears "
                           "it."}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_task_profiles",
        description="The Agent Profiles this task's project lets you choose for its Workers and Reviewers (their "
                    "model and price per million tokens; the list may be empty), what each role runs now and why, "
                    "and your choices per step.",
        schema={"type": "object", "additionalProperties": False, "properties": {"task_id": _TASK_ID}},
        risk=Risk.READ, run=_profiles,
    ),
    Tool(
        name="looper_task_profile_set",
        description="Choose the Agent Profile of this task's Workers or Reviewers, from the profiles "
                    "looper_task_profiles lists as allowed: for the rest of the task, or with step for that step "
                    "(each attempt of it). profile_id null clears your choice. The role's runs that start after "
                    "the call use it; a run already started keeps its own. The user's choice for the task wins "
                    "over yours.",
        schema={"type": "object", "required": ["role"], "additionalProperties": False, "properties": {
            "role": _ROLE,
            "profile_id": {"type": "string", "minLength": 1,
                           "description": "An allowed profile's id or name; null clears your choice."},
            "step": {"type": "string", "minLength": 1,
                     "description": "A step of the plan that has not ended (its id in the plan); leave it out for "
                                    "the rest of the task."},
            "reason": _REASON,
            "task_id": _TASK_ID,
        }},
        risk=Risk.WRITE, run=_profile_set,
    ),
    Tool(
        name="looper_task_concurrency",
        description="Change how many of this task's Workers or Reviewers run at once, within its project's limits; "
                    "it applies at once and the user is told. A null limit clears the task's own, so the project's "
                    "applies; lowering one stops nothing that runs.",
        schema={"type": "object", "required": ["limits"], "additionalProperties": False, "properties": {
            "limits": _limits("worker, reviewer", PLANNER_CHOICE_ROLES),
            "reason": _REASON,
            "task_id": _TASK_ID,
        }},
        risk=Risk.WRITE, run=_task_concurrency,
    ),
    Tool(
        name="looper_project_concurrency",
        description="Change this task's project's limits, which its other tasks share: planner (how many of its "
                    "tasks run at once), worker, reviewer. Only when the requirement or the user asks for the "
                    "project's limits; it applies at once and the user is told. A null limit clears it to what it "
                    "inherits; lowering one stops nothing that runs.",
        schema={"type": "object", "required": ["limits"], "additionalProperties": False, "properties": {
            "limits": _limits("planner, worker, reviewer", (AgentType.PLANNER, *PLANNER_CHOICE_ROLES)),
            "reason": _REASON,
            "task_id": _TASK_ID,
        }},
        risk=Risk.WRITE, run=_project_concurrency,
    ),
)
