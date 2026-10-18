"""Planner Agent: one per task, backed by a resident Claude Code process.

Each task has one PlannerAgent for its whole life, and the Planner keeps one
Claude Code process running for as long as the task runs: stream-json in and
out, in the task's directory, with every tool and no permission prompts like
every agent; its role prompt keeps it to planning and deciding, and Workers
make the changes. Each event is one user turn on it:

- the TASK_REQUEST: Claude splits the requirement into steps, each with an
  instruction, acceptance criteria, dependencies and whether an independent
  Reviewer must pass it;
- each Worker or Reviewer result: Claude decides to accept the step, send it
  to review, retry it in a new Worker, replan the steps not started yet, or
  fail the task;
- each change to the task (``db.task_changes``): a supplement (every chat
  message from a user is one, and the Manager passes others on) or an edit of
  the requirement. Claude answers, and decides whether the plan still fits:
  it may pass text to a running Worker, add steps, or replan the steps not
  started (stopping running steps the change makes wrong). Every change
  reaches Claude in a turn: one arriving while a turn runs waits for it to
  end, several waiting are given together, and a change is delivered only once
  its turn's reply is done, so a failed turn or a restart gives it again.

Every reply ends with JSON that ``planner_protocol`` validates; code then
does the dispatching. Code writes the task journal (``journal``) as things
happen: a step accepted, failed or retried, with its agents' learnings; the
Planner's replans, added steps and spend-cap stop, with the reason; the
supplements; a new session and the handoff before it. The Planner's
DECISION, SUPERVISE and CHAT turns and each Worker's first turn carry the
journal back, each Worker's and Reviewer's task section the supplements, and a project with ``distill_lessons`` gets one LESSONS turn when
a task completes, saved as a project knowledge document or a draft of one; a
learning a Worker or Reviewer says holds for the whole project becomes a draft
when it arrives. The plan, its steps,
its journal and the Planner's Claude session live in PostgreSQL (``plans``,
``agent_sessions``), written as they change; a
Planner started after the server restarted reads them back and carries on
(TASK_RESUME) without planning again. When the Planner's agent can no longer
resume its session (``session_switch``), the old session, while it runs, hands
over in one HANDOFF turn, and the new session's first turn starts with a resume
brief; so does the first turn of any new session for a plan under way. The
system prompt is rebuilt for every turn from the harness, the ``planner``
config and the task's project as it is now (its
background and the index of its knowledge included, the knowledge's
directories mounted with ``--add-dir``) and the catalog items installed for
the Planner, globally or in the project (each a ``--plugin-dir``). Its agent
settings (harness, model, effort, environment, ``--max-budget-usd``) are
resolved for the Planner in the task's project every turn
(``agent_settings.load_agent_settings``); when the prompt, a flag or the
environment changes, Claude restarts and resumes its session. The project's
execution settings (``project_execution``) set the supervision interval,
whether every step needs a review and the task's spend cap, read before each
event's turns. An
invalid reply gets one repair turn. Planning that
still fails fails the task; a decision that still fails falls back to the
step's own verdict. When the task completes or fails, the Planner signals the
TaskRouter, which tears it down and ends its Claude.

Under ``looper serve`` the Planner also has platform tools of its own
(``ops.planner``): Looper's MCP server, with a token for this task that it
gets when it starts and that stops working when it stops. With them it
chooses its Workers' and Reviewers' Agent Profiles from its project's list
and changes its task's concurrency; they change things only
in a turn Looper sends it, and its system prompt says how to use them after
the reply contract. Where its project lets it (``planner_roles`` ``on``), it also
defines roles for its steps (``task_roles``): a step's Worker may run as a work
role, and a reviewed step may have review roles that review it, one after
another, once the built-in Reviewer passed it (``ReviewStage``,
``StepReviewRole``): the built-in Reviewer and each required one must pass; an
advisory one's verdict only goes to the decision.

Where its task may (``user_gates.GatePolicy``: its project has ``user_gates``
``on`` and no schedule started it), a step of its plan may be a user gate: when
the steps it depends on are done it opens and waits for the user, and the
steps that depend on it wait with it, while the rest of the plan runs on. The
user's messages in it (``db.user_gates``, stored first; ``GATE_MESSAGE`` only
wakes the Planner) reach Claude after each event, a gate's waiting ones
together, in a [GATE] turn, whose reply updates the agenda and either goes on
or closes the gate, the summary kept as a supplement (``via`` ``gate``) in the
same transaction, and may change the plan as a CHAT reply does. A turn that
fails is tried again on its own after each of ``GATE_RETRY_WAIT_SEC``, then
with the user's next message. A DECISION or CHAT reply may update a gate or
cancel it (``gates``). While a gate waits the Planner reports the task as
``waiting_user``; when nothing else runs and no turn came for
``GATE_IDLE_STOP_SEC``, its resident Claude stops, and the next turn resumes
its session (a new session's resume brief carries the gates' discussions).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from ..bus import MessageBus
from ..supervisor import cancel_and_wait
from ..agent_settings import AgentSettings, AgentType, Harness, Launch, load_agent_settings
from ..claude_cli import (
    AUTONOMOUS_INPUT,
    AutonomousHandler,
    AutonomousRun,
    ClaudeRunRecorder,
    ResidentTurn,
    exit_note,
)
from .. import knowledge as project_knowledge
from .. import session_switch
from .. import step_workspaces as workspaces
from ..knowledge import AgentKnowledge, documents_label, load_knowledge
from ..operator_notes import notes_note
from ..library import AgentLibrary, items_label, load_installed
from ..project_execution import LessonsMode, ProjectExecution, TaskBudgetAction, format_usd
from ..run_usage import Usage
from ..runtimes import CLAUDE_CODE
from ..session_switch import SessionSwitch
from ..step_workspaces import MergeError, MergeResult, Repo, StepWorkspace, StepWorkspaceState, Unusable
from ..task_roles import RoleKind, TaskRole
from ..user_gates import (
    MESSAGE_MAX_CHARS,
    RETRY_WAIT_SEC,
    THREAD_MAX_CHARS,
    WAITING_USER,
    Gate,
    GateAction,
    GatePolicy,
    GateState,
    GateVerdict,
    GateView,
    MessageKind,
    MessageRole,
    conclusion_text,
    gate_policy,
    resolved,
)
from ..system_prompt import SystemPrompt, build_system_prompt, is_recommended, plan_task_section
from ..message import (
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    RestartMode,
    planner_address,
    worker_address,
)
from . import journal
from .journal import JournalEntry, JournalKind, LearningScope
from .planner_protocol import (
    ChatAction,
    ChatReply,
    Decision,
    DecisionContext,
    DecisionReply,
    GateChange,
    GateContext,
    GateReply,
    PlanView,
    ProgressNote,
    ProtocolError,
    ResultKind,
    StepSpec,
    SuperviseAction,
    TurnKind,
    parse_chat,
    parse_decision,
    parse_gate,
    parse_handoff,
    parse_lessons,
    parse_plan,
    parse_supervise,
    parse_wrap_up,
    reply_contract,
)

logger = logging.getLogger(__name__)

from ...db import activity as db_activity, role_configs as db_role_configs, llm_records as db_llm_records
from ...db import agent_sessions as db_agent_sessions, plans as db_plans, projects as db_projects
from ...db import journal as db_journal, step_workspaces as db_step_workspaces, task_changes as db_changes
from ...db import role_templates as db_templates, task_roles as db_roles, tasks as db_tasks
from ...db import user_gates as db_user_gates
from ...db.task_changes import TaskChanges
from ...db.tasks import FailKind, update_task_phase

# How long to wait for the worker pool to confirm a forwarded user turn.
WORKER_INPUT_TIMEOUT = 5.0
# A Planner turn's time limit without a readable ``planner`` config row.
DEFAULT_TURN_TIMEOUT_SEC = 1200
# How often the Planner looks at the Workers running now when no result has
# arrived (a SUPERVISE turn), to steer or stop one that drifts or stalls.
# 0 turns the periodic check off. Any Planner turn re-arms the interval.
DEFAULT_SUPERVISE_INTERVAL_SEC = 300
# A plan whose task still runs.
_LIVE = ("executing", "verifying")
# A step in flight: its Worker runs, a decision on its result is due, or it is in review.
_IN_FLIGHT = ("dispatched", "deciding", "completed")
# How long a task that only waits for the user (a gate waits, no step is in flight) keeps its Planner's resident
# Claude without a turn, and how often that is checked; the next turn resumes the session.
GATE_IDLE_STOP_SEC = 900
GATE_IDLE_CHECK_SEC = 60
# A [GATE] turn that failed is tried again on its own once for each wait (``user_gates.RETRY_WAIT_SEC``), checked
# every ``GATE_IDLE_CHECK_SEC``; after the last, the user's next message sends the messages again.
GATE_RETRY_WAIT_SEC = RETRY_WAIT_SEC
# Why an accepted step's branch could not be merged, for the decision turn.
_MERGE_REASONS = {
    MergeError.CONFLICT: "its changes conflict with what the branch has now",
    MergeError.LOCAL_CHANGES: "uncommitted changes in the task directory are in the way",
    MergeError.BRANCH_CHANGED: "the task directory's branch changed under the task",
    MergeError.STEP_BRANCH_CHANGED: "the step's own branch was switched or rewritten",
    MergeError.GIT_FAILED: "a git command failed",
}

# The Planner has every tool, like every agent; this recommended role prompt
# is what keeps it to planning and deciding.
DEFAULT_SYSTEM_PROMPT = """\
You are the Planner of one Looper task. You live as long as the task: you plan
its steps, decide what happens after each step's result, and answer the
user's messages about it.

- You plan and decide; Workers do the work. Each step runs in a fresh Claude
  Code session that sees only its step. Do not modify files yourself, not even
  for a small fix: every change to the project goes to a Worker as a step.
- You have every tool. Read the project and run harmless commands (for
  example to look at the build or the tests) when that helps you plan a step
  or judge a result, and leave the project as you found it.
- Split the work into steps that each deliver one result that can be checked
  on its own. Keep a small piece of work whole when splitting it would only
  add hand-offs.
- Steps without a dependency between them run at the same time. Make a step
  depend on another only when it needs that step's result, and let
  independent parts run side by side instead of chaining them.
- When parts must fit together (a front end and its back end, a library and
  the code that uses it), first have one step pin the interface between them
  in a file (for example the API's routes and payloads, or the library's
  function signatures), then build the parts in parallel against that file,
  and finish with a step that integrates them and checks them together.
- Steps that run at the same time share the working directory: give each its
  own files, and never ask a step to commit with `git add -A` or `git add .`;
  a Worker stages only the paths it changed.
- Give each step an instruction a Worker can follow on its own and acceptance
  criteria that say how to tell it is done.
- Ask for an independent Reviewer when a step is risky or its result is hard
  to judge from the Worker's own report.
- Accept a step only when its result meets the acceptance criteria. Retry with
  specific feedback when a Worker can fix what is wrong; replan when the
  approach must change; fail when the task cannot be done.
- A replan builds on what is done: the accepted steps stay, and new steps
  depend on the ones whose results they need instead of doing their work
  again. It replaces only work that is not done: the steps not started, a
  step whose result cannot be accepted and, when the task changes, the
  running steps the change makes wrong.
"""

T = TypeVar("T")


@dataclass(frozen=True)
class PlannerSettings:
    """The ``planner`` agent config, read for every turn."""

    system_prompt: str
    timeout_seconds: int  # per turn; 0 for no limit
    max_turns: int  # 0 for no limit
    supervise_interval_seconds: int  # periodic SUPERVISE turn; 0 to turn off
    recommended: bool = True  # whether system_prompt is the recommended one (default_prompt)


async def load_settings() -> PlannerSettings:
    """Read the ``planner`` agent config; without a readable row use the defaults."""
    try:
        config = await db_role_configs.get_config("planner")
    except Exception:
        logger.warning("cannot read the planner agent config; using defaults", exc_info=True)
        config = None
    if config is None:
        return PlannerSettings(DEFAULT_SYSTEM_PROMPT, DEFAULT_TURN_TIMEOUT_SEC, 0, DEFAULT_SUPERVISE_INTERVAL_SEC)
    interval = config.get("supervise_interval_seconds")
    system_prompt = str(config.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT
    return PlannerSettings(
        system_prompt=system_prompt,
        recommended=is_recommended(system_prompt, str(config.get("default_prompt") or "") or DEFAULT_SYSTEM_PROMPT),
        timeout_seconds=int(config.get("timeout_seconds") or 0),
        max_turns=int(config.get("max_turns") or 0),
        supervise_interval_seconds=(
            DEFAULT_SUPERVISE_INTERVAL_SEC if interval is None else max(0, int(interval))
        ),
    )


class PlannerTurnError(Exception):
    """A Planner turn produced no valid reply; the message says why."""


class _SessionNotResumed(Exception):
    """Claude never started the saved session its process was told to resume; the message says so."""


@dataclass(frozen=True)
class _Process:
    """What the Planner's running Claude was started with; each turn on it is recorded with these."""

    command: list[str]
    signature: Any
    system_prompt: SystemPrompt
    launch: Launch


class ReviewStage(str, Enum):
    """Which review a step in review waits for: the built-in Reviewer's, or then one of its review roles'
    (``PlanStep.review_position``)."""

    BUILTIN = "builtin"
    ROLE = "role"


@dataclass
class StepReviewRole:
    """A review role of a step: whether its verdict blocks the step, and the version its latest review ran with."""

    role: str
    required: bool = True
    version: int | None = None

    def row(self) -> dict[str, Any]:
        return {"role": self.role, "required": self.required, "version": self.version}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> StepReviewRole:
        return cls(str(row["role"]), row.get("required") is not False, row.get("version"))

    @property
    def label(self) -> str:
        return self.role + ("" if self.required else " (advisory)")


@dataclass
class PlanStep:
    step_id: str  # unique across tasks: the task_steps row id
    index: int
    instruction: str
    key: str = ""  # the Planner's label for the step, unique within the task
    target_files: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)  # step_ids
    review: bool = False  # a Reviewer must pass the step before it is accepted
    # "pending" | "dispatched" | "deciding" | "completed" (in review) | "verified" | "failed" | "skipped"
    status: str = "pending"
    result: dict[str, Any] | None = None
    worker_id: str | None = None
    reviewer_id: str | None = None
    retries: int = 0
    feedback: list[str] = field(default_factory=list)  # for retries
    # User text the Planner forwarded that reached this step's Worker too late.
    missed_messages: list[str] = field(default_factory=list)
    # Its latest worktree, in a project with step workspaces (step_workspaces rows, not the step's).
    workspace: StepWorkspace | None = None
    # The roles its task's Planner defined that it runs as (None: the built-in Worker), with the version its latest
    # attempt ran with, and that review it after the built-in Reviewer, in order (none: the built-in Reviewer alone).
    role: str | None = None
    role_version: int | None = None
    review_roles: list[StepReviewRole] = field(default_factory=list)
    review_stage: ReviewStage | None = None  # in review: which review it waits for
    review_position: int | None = None  # in a role's review: the index of that role in review_roles
    # A gate step's gate (``user_gates``): its instruction is its topic, and no Worker runs it.
    gate: Gate | None = None
    # When its run was first dispatched; kept through its retries and reviews, gone when it is sent back to not
    # started (task_steps.started_at).
    started_at: datetime | None = None

    @property
    def label(self) -> str:
        return self.key or self.step_id

    @property
    def isolated(self) -> bool:
        """Whether the step works in a worktree of its own: in it now, being merged, or awaiting a decision on
        a merge that failed."""
        return self.workspace is not None and self.workspace.state in (
            StepWorkspaceState.ACTIVE, StepWorkspaceState.MERGING, StepWorkspaceState.UNMERGED)

    @property
    def started(self) -> bool:
        """A work step started when its first Worker did, a gate when it opened."""
        if self.gate is not None:
            return self.status != "pending"
        return self.worker_id is not None


@dataclass
class TaskPlan:
    plan_id: str
    goal_id: str
    requirement: str
    work_dir: str
    steps: list[PlanStep] = field(default_factory=list)
    # "planning" | "executing" | "verifying" | "stopping" (at the spend cap) | "completed" | "failed"
    status: str = "planning"
    correlation_id: str = ""
    origin_channel_id: str = ""
    project_id: str = ""
    # The task's project as last read; refreshed before every event's turns, never saved.
    project_name: str = ""
    project_description: str = ""
    project_background: str = ""
    project_updated_at: str = ""  # the project's version, for the system prompt's record
    project_work_dir: str = ""  # the project's default directory, where its path documents are
    session_id: str | None = None  # the Planner's Claude session (agent_sessions), resumed after a restart
    # The session's running totals at its last result; a process that resumes the session starts from them.
    session_totals: Usage = field(default_factory=Usage)
    # The runtime type and the provider the session belongs to and the Agent Profile that ran it last (None: a
    # session saved before they were recorded).
    session_runtime: str | None = None
    session_profile: str | None = None
    session_provider: str | None = None
    # The task's result: the final summary of the decision that completed it, else the last accepted step's summary.
    final_summary: str = ""
    fail_reason: str = ""
    # The task journal (task_journal), oldest first: an entry per accepted step, written as it is accepted.
    journal: list[JournalEntry] = field(default_factory=list)
    # With step workspaces: the task directory's branch and its commit when the task dispatched its first step.
    start_branch: str = ""
    start_commit: str = ""

    @property
    def is_complete(self) -> bool:
        return all(s.status in ("verified", "skipped") for s in self.steps)

    @property
    def has_failed(self) -> bool:
        return any(s.status == "failed" for s in self.steps)

    def last_open(self, step: PlanStep) -> bool:
        """Whether ``step`` is the only step not done: settling it settles the task."""
        return all(s.status in ("verified", "skipped") for s in self.steps if s is not step)

    def ready_steps(self) -> list[PlanStep]:
        """Steps whose dependencies are all verified."""
        done = {s.step_id for s in self.steps if s.status == "verified"}
        return [
            s for s in self.steps
            if s.status == "pending" and all(d in done for d in s.depends_on)
        ]

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def labels(self) -> dict[str, str]:
        """step_id → label."""
        return {s.step_id: s.label for s in self.steps}

    def view(self) -> PlanView:
        labels = self.labels()

        def deps(step: PlanStep) -> tuple[str, ...]:
            return tuple(labels.get(d, d) for d in step.depends_on)

        live = [s for s in self.steps if s.status != "skipped"]
        dropped = [s for s in self.steps if s.status == "skipped"]
        return PlanView(
            started={s.label: deps(s) for s in live if s.started},
            pending={s.label: deps(s) for s in live if not s.started},
            retired=frozenset(s.label for s in dropped),
            running=frozenset(s.label for s in live if s.status == "dispatched"),
            done=frozenset(s.label for s in live if s.started and s.status == "verified"),
            gates={s.label: GateView(s.gate.state, s.gate.agenda) for s in live if s.gate is not None},
        )

    def waiting_gates(self) -> list[PlanStep]:
        """The gate steps that wait for the user, in plan order."""
        return [s for s in self.steps if s.gate is not None and s.gate.state.waiting]

    def progress_summary(self) -> str:
        live = [s for s in self.steps if s.status != "skipped"]
        total = len(live)
        done = sum(1 for s in live if s.status == "verified")
        running = sum(1 for s in live if s.status in ("dispatched", "deciding", "completed"))
        failed = sum(1 for s in live if s.status == "failed")
        waiting = sum(1 for s in live if s.status == WAITING_USER)
        parts = [f"{done}/{total} 完成"]
        if running:
            parts.append(f"{running} 执行中")
        if waiting:
            parts.append(f"{waiting} 等待用户")
        if failed:
            parts.append(f"{failed} 失败")
        return ", ".join(parts)

    def row(self) -> dict[str, Any]:
        """The plan's own state, as ``plans.save`` writes it."""
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "origin_channel_id": self.origin_channel_id,
            "final_summary": self.final_summary,
            "fail_reason": self.fail_reason,
            "start_branch": self.start_branch,
            "start_commit": self.start_commit,
        }


def _step_row(step: PlanStep) -> dict[str, Any]:
    """``step`` as ``plans.save`` writes it."""
    return {
        "id": step.step_id,
        "index": step.index,
        "key": step.key,
        "instruction": step.instruction,
        "target_files": step.target_files,
        "validation_commands": step.validation_commands,
        "acceptance_criteria": step.acceptance_criteria,
        "depends_on": step.depends_on,
        "review": step.review,
        "status": step.status,
        "result": step.result,
        "worker_id": step.worker_id,
        "reviewer_id": step.reviewer_id,
        "retries": step.retries,
        "feedback": step.feedback,
        "missed_messages": step.missed_messages,
        "role_key": step.role,
        "role_version": step.role_version,
        "review_roles": [item.row() for item in step.review_roles],
        "review_stage": step.review_stage.value if step.review_stage else None,
        "review_position": step.review_position,
        "kind": "gate" if step.gate is not None else "work",
        "started_at": step.started_at,
    }


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _sentence(text: str) -> str:
    """``text`` with a period at its end, unless it already ends a sentence."""
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def _ago(moment: datetime) -> str:
    """How long ago ``moment`` was, roughly: ``3 h``, ``12 min``, ``2 d``."""
    seconds = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 172_800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86_400)} d"


def _message_line(message: Mapping[str, Any]) -> str:
    """A message of a gate's discussion as a turn's message shows it: who (the user's: from where), when, the option
    chosen."""
    role = str(message.get("role") or MessageRole.USER.value)
    where = [str(message.get("via") or "") or "dashboard"] if role == MessageRole.USER.value else []
    if message.get("author"):
        where.append(str(message["author"]))
    created = message.get("created_at")
    if isinstance(created, datetime):
        where.append(created.astimezone(timezone.utc).strftime("%H:%M UTC"))
    if message.get("option"):
        where.append(f"chose \"{message['option']}\" for {message.get('agenda_item') or 'the agenda'}")
    if message.get("kind") == MessageKind.END.value:
        where.append("ended the discussion")
    who = {MessageRole.PLANNER.value: "you", MessageRole.LOOPER.value: "Looper"}.get(role, "")
    text = journal.kept_text(message.get("text"), MESSAGE_MAX_CHARS).replace("\n", "\n  ")
    return f"- ({', '.join(filter(None, [who, *where]))}) {text}".rstrip()


def _retry_due(gate: Gate, now: datetime) -> bool:
    """Whether ``gate``'s failed [GATE] turn is due for its automatic retry: one is left (``GATE_RETRY_WAIT_SEC``),
    and its wait since the failure is over."""
    due = gate.retry_at(GATE_RETRY_WAIT_SEC)
    return due is not None and now >= due


def _thread_lines(messages: Sequence[Mapping[str, Any]], limit: int) -> list[str]:
    """``messages`` as ``_message_line`` shows them, the newest that fit in ``limit`` characters, oldest first,
    after a note on how many older ones were left out."""
    shown: list[str] = []
    used = 0
    for message in reversed(messages):
        line = _message_line(message)
        if used + len(line) > limit:
            break
        shown.insert(0, line)
        used += len(line)
    omitted = len(messages) - len(shown)
    return ([f"({omitted} older message(s) left out)"] if omitted else []) + shown


def _verdict_reason(verdict: Mapping[str, Any], findings: str, summary: str) -> str:
    """Why a review did not pass, in a line: its findings, else its summary."""
    found = "; ".join(_clip(f, 200) for f in verdict.get(findings) or [] if str(f).strip())
    return found or _clip(verdict.get(summary), 300) or "no reason given"


def _role_reviews(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The verdicts of a step's review roles on its last result, in the order they reviewed it."""
    reviews = result.get("role_reviews")
    return [r for r in reviews if isinstance(r, Mapping)] if isinstance(reviews, list) else []


def result_outcome(result: Mapping[str, Any]) -> str:
    """What a step's last result was, in a line: what a retry, a failure or an unfinished step is about."""
    failed = [r for r in _role_reviews(result) if r.get("passed") is not True]
    # A required review role's failure is what stopped the chain; an advisory one's is the reason otherwise.
    role_review = next((r for r in failed if r.get("required") is not False), failed[0] if failed else None)
    if role_review is not None:
        return f"review by role {role_review.get('role')} failed: {_verdict_reason(role_review, 'findings', 'summary')}"
    if "verify_passed" in result and result.get("verify_passed") is not True:
        return f"review failed: {_verdict_reason(result, 'verify_findings', 'verify_summary')}"
    status = str(result.get("status") or "unknown")
    detail = result.get("summary") if status == "completed" else result.get("error") or result.get("summary")
    return f"{status}: {_clip(detail, 400) or 'no reason given'}"


# How a closing note names a step that is not done, by its status.
_NOT_DONE = {"pending": "not started", "dispatched": "was running", "deciding": "awaiting a decision",
             "completed": "awaiting review", "failed": "failed", WAITING_USER: "a gate waiting for the user"}


def closing_note(why: str, steps: Iterable[tuple[str, str, Mapping[str, Any] | None]]) -> str:
    """What a task that ended without completing leaves as its result: why it ended, what its steps delivered, and
    what is not done. ``steps`` are (label, status, last result); a dropped step is neither."""
    done: list[str] = []
    not_done: list[str] = []
    for label, status, result in steps:
        if status == "verified":
            done.append(f"- {label}: {_clip((result or {}).get('summary'), 300) or 'done'}")
        elif status != "skipped":
            outcome = f": {result_outcome(result)}" if result else ""
            not_done.append(f"- {label} ({_NOT_DONE.get(status, status)}){outcome}")
    return "\n\n".join([f"## Why it ended\n{why}", "## Done\n" + ("\n".join(done) or "- nothing"),
                        "## Not done\n" + ("\n".join(not_done) or "- nothing")])


def _requirement_diff(edit: Any) -> str:
    """What a requirement edit changed: a unified diff of its lines, else (a diff longer than the new text) the
    new text whole."""
    import difflib

    before, after = edit.before_text.splitlines(), edit.text.splitlines()
    diff = "\n".join(difflib.unified_diff(before, after, f"version {edit.before_version}", f"version {edit.version}",
                                          lineterm=""))
    return diff if len(diff) <= len(edit.text) + 200 else "(rewritten; the new text is in the Task section)"


def _learning_lines(value: Any) -> list[str]:
    """A report's learnings as decision-turn lines, a project-wide one marked as such."""
    return [f"- learning{' (holds for the whole project)' if item.scope is LearningScope.PROJECT else ''}: "
            f"{item.text}" for item in journal.parse_learnings(value)]


def _prepare_work_dir(work_dir: str) -> str | None:
    """Create the task's directory if needed; the problem, if it cannot be used."""
    if not work_dir:
        return "the task has no work directory"
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create work directory {work_dir}: {exc}"
    return None


@dataclass(frozen=True)
class _RoundBase:
    """Where the new worktrees of a round of steps start: a commit on the task directory's branch, or why there
    is none."""

    repo: Repo | None = None
    commit: str = ""
    checkpoint: bool = False
    error: str = ""


class _NoWorkspace(Exception):
    """A step that needs a worktree could not get one; the message says why."""


class PlannerAgent:
    """Per-task Planner: one instance and one resident Claude per task."""

    def __init__(self, *, task_id: str, runtime_root: Path, claude_bin: str = "claude", ops: Any = None) -> None:
        self._task_id = task_id
        self._runtime_root = runtime_root
        self._claude_bin = claude_bin
        # The Planner's own platform tools (``ops.planner.PlannerOps``), and the token they know it by while it runs;
        # without them it has none.
        self._ops = ops
        self._token: str | None = None
        # The MCP configuration files that carry the token, removed when the Planner stops.
        self._token_files: set[str] = set()
        self._bus: MessageBus | None = None
        self._plan: TaskPlan | None = None
        self._loop_task: asyncio.Task[Any] | None = None
        self._running = False
        self._claude = CLAUDE_CODE.resident_session(self.agent_id)
        self._claude_runtime = CLAUDE_CODE  # the runtime type whose session self._claude is
        self._turn_lock = asyncio.Lock()
        self._supervise_task: asyncio.Task[Any] | None = None
        # When the last turn ended, so a periodic SUPERVISE is skipped right
        # after a result/chat/plan turn already looked at the plan.
        self._last_turn_at = 0.0
        # The project background the last turn's system prompt carried (None before the first turn).
        self._prompt_background: str | None = None
        # The project knowledge the last turn ran with (None before the first turn); a turn that cannot
        # read the knowledge keeps it, as a failed project read keeps the last project.
        self._knowledge: AgentKnowledge | None = None
        # The installed items the last turn ran with, kept the same way when a turn cannot read them.
        self._library: AgentLibrary | None = None
        # The agent settings the last turn ran with, kept the same way when a turn cannot read them.
        self._agent_settings: AgentSettings | None = None
        # The project's execution settings as last read; everything inherits until the project is read.
        self._execution = ProjectExecution()
        # What the running Claude was started with (None before the first start).
        self._process: _Process | None = None
        # Why the session was last dropped and the handoff its old session wrote, until the new session's first
        # turn has its resume brief.
        self._pending_switch: SessionSwitch | None = None
        self._pending_handoff: str | None = None
        # The project-wide learnings already saved as drafts, so a retry that reports one again adds none.
        self._drafted: set[str] = set()
        # Why the last round of steps started fewer than were ready (step workspaces), noted once.
        self._round_note = ""
        # The task's roles by key as last read (after each reply, before it is checked); None while its project lets
        # its Planner define none.
        self._roles: dict[str, RoleKind] | None = None
        # Whether a schedule started the task (it runs unattended, so it opens no gate), as its project was last read.
        self._scheduled = False
        self._idle_task: asyncio.Task[Any] | None = None

    @property
    def agent_id(self) -> str:
        return f"planner-{self._task_id}"

    @property
    def agent_address(self) -> AgentAddress:
        return planner_address(self._task_id)

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._replies: dict[int, str] = {}  # a supplement's journal id → the reply of the turn that delivered it
        self._running = True
        if self._ops is not None:
            self._token = self._ops.issue(self._task_id)  # the task's earlier Planner's stops working
        await self._load_plan()
        self._loop_task = asyncio.create_task(self._message_loop())
        self._supervise_task = asyncio.create_task(self._supervise_loop())
        self._idle_task = asyncio.create_task(self._idle_loop())
        logger.info("planner %s started", self.agent_id)

    async def stop(self) -> None:
        """Stop handling messages and end the Planner's Claude."""
        self._running = False
        if self._ops is not None and self._token is not None:
            self._ops.revoke(self._token)
        # The files that carry the token go with it, before this stop waits for anything: a stop the server's
        # shutdown cuts off would leave them behind.
        self._remove_token_files()
        supervise_task, self._supervise_task = self._supervise_task, None
        idle_task, self._idle_task = self._idle_task, None
        if idle_task:
            idle_task.cancel()
        loop_task, self._loop_task = self._loop_task, None
        cancelled = False
        try:
            if supervise_task:
                await cancel_and_wait(supervise_task)
            # Stop the Workers this task still runs; otherwise they keep
            # changing files after the task is gone.
            await self._kill_running_workers()
            if loop_task:
                await cancel_and_wait(loop_task)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            async def cleanup(kill: bool) -> None:
                try:
                    for task in (supervise_task, loop_task):
                        if task is not None and not task.done():
                            await cancel_and_wait(task)
                except asyncio.CancelledError:
                    kill = True  # hurried: Claude is ended at once, and the loops not waited for yet are cancelled
                    for task in (supervise_task, loop_task):
                        if task is not None and not task.done():
                            task.cancel()
                    raise
                finally:
                    # A turn the loop was in has already killed Claude; an idle one
                    # ends when its stdin closes. Never leave it running.
                    await self._claude.stop(kill=kill)
                    self._remove_token_files()  # and one a turn wrote meanwhile

            # Run to its end even when this stop is cancelled again: a second cancellation only hurries it (Claude
            # ended at once rather than after its grace).
            cleaning = asyncio.create_task(cleanup(cancelled))
            hurried = False
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    if not hurried:
                        hurried = True
                        cleaning.cancel()
            if hurried:
                raise asyncio.CancelledError
            cleaning.result()

    def _remove_token_files(self) -> None:
        """Delete the MCP configuration files that carry this Planner's token, which no longer works."""
        for path in list(self._token_files):
            Path(path).unlink(missing_ok=True)
        self._token_files.clear()

    def is_healthy(self) -> bool:
        return self._running

    async def _message_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.agent_id):
                if not self._running:
                    break
                try:
                    await self._handle(envelope)
                except Exception:
                    logger.exception("planner %s error handling %s", self.agent_id, envelope.message_id)
        except asyncio.CancelledError:
            pass

    async def _handle(self, envelope: MessageEnvelope) -> None:
        if self._plan is not None and envelope.message_type is not MessageType.TASK_REQUEST:
            # Turns this event runs see the project as it is now, in their
            # message and in the system prompt (a new plan reads it itself).
            await self._refresh_project(self._plan)
            if envelope.message_type is not MessageType.TASK_CANCEL:
                # A task at its spend cap stops before another turn; the handlers below see it ended.
                await self._within_budget(self._plan)
            # Steps this event dispatches see every change stored so far; Claude gets them in a turn below.
            await self._read_changes(self._plan, await self._pending_changes())
            # Its gates as the user's messages left them (an open one the user wrote in is under discussion).
            await self._read_gates(self._plan)
        match envelope.message_type:
            case MessageType.TASK_REQUEST if envelope.payload.get("restart") == RestartMode.AT_CAP:
                await self._continue_plan(envelope)
            case MessageType.TASK_REQUEST if envelope.payload.get("restart") == RestartMode.CONTINUE:
                await self._restart_plan(envelope)
            case MessageType.TASK_REQUEST:
                await self._plan_and_dispatch(envelope)
            case MessageType.TASK_RESUME:
                await self._resume(envelope)
            case MessageType.WORK_RESULT:
                await self._handle_work_result(envelope)
            case MessageType.VERIFY_RESULT:
                await self._handle_verify_result(envelope)
            case MessageType.TASK_CANCEL:
                await self._handle_cancel(envelope)
            case MessageType.USER_MESSAGE:
                await self._handle_user_message(envelope)
            case MessageType.TASK_CHANGE | MessageType.GATE_MESSAGE:
                pass  # delivered below, with any other change or message waiting
            case MessageType.SYSTEM_EVENT:
                await self._handle_system_event(envelope)
        # Every change waiting reaches Claude once the event's turns are done: one that arrived meanwhile, or
        # one a failed turn or a restart left undelivered; then the user's messages in its gates.
        if self._plan is not None:
            await self._deliver_changes(self._plan)
            await self._deliver_gate_messages(self._plan)

    # --- Planning ---

    async def _plan_and_dispatch(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        payload = envelope.payload
        await update_task_phase(self._task_id, "planning")
        await self._activity("phase", "Planner started: analyzing requirement")

        plan = TaskPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:12]}",
            goal_id=payload.get("goal_id", ""),
            requirement=payload.get("requirement", ""),
            work_dir=payload.get("work_dir", ""),
            correlation_id=envelope.correlation_id,
            origin_channel_id=envelope.metadata.get("origin_channel_id", ""),
            project_id=payload.get("project_id", "") or "",
        )
        await self._refresh_project(plan)
        # The task's changes so far: a requirement edited while the task waited, and its supplements.
        changes = await self._pending_changes()
        await self._read_changes(plan, changes)
        self._plan = plan
        # A new plan is a new conversation, which needs no resume brief.
        async with self._turn_lock:
            await self._claude.stop()
        self._pending_switch = self._pending_handoff = None
        await self._save(plan, plan_row=True)
        await self._save_session(plan)

        problem = _prepare_work_dir(plan.work_dir)
        if problem:
            await self._fail_plan(plan, reason=f"Planning failed: {problem}")
            return
        try:
            reply = await self._ask(TurnKind.PLAN, self._plan_message(plan, await self._template_lines(plan)),
                                    lambda text: parse_plan(text, self._roles, gate_policy=self._gate_policy))
        except PlannerTurnError as exc:
            await self._fail_plan(plan, reason=f"Planning failed: {exc}")
            return

        steps = self._add_steps(plan, reply.steps)
        plan.status = "executing"
        await self._save(plan, *steps, plan_row=True)
        await self._mark_delivered(changes)
        await update_task_phase(self._task_id, "executing")
        await self._activity("phase", f"Plan created: {len(steps)} step(s): {self._describe(plan, steps)}")

        # Notify TaskRouter + Manager: task accepted
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_ACCEPTED,
            payload={
                "task_id": self._task_id,
                "plan_id": plan.plan_id,
                "step_count": len(steps),
                "steps": [{"step_id": s.step_id, "instruction": s.instruction} for s in steps],
            },
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

        await self._report_progress(plan)
        await self._dispatch_ready(plan)

    def _add_steps(self, plan: TaskPlan, specs: tuple[StepSpec, ...]) -> list[PlanStep]:
        """Add or replace (same label, not started) steps; returns them in spec order.

        Labels and dependencies were validated against this plan.
        """
        by_label = {s.label: s for s in plan.steps if s.status != "skipped"}
        changed: list[PlanStep] = []
        for spec in specs:
            step = by_label.get(spec.id)
            if step is None:
                step = PlanStep(
                    step_id=f"step-{uuid.uuid4().hex[:8]}",
                    index=len(plan.steps),
                    instruction=spec.instruction,
                    key=spec.id,
                )
                plan.steps.append(step)
                by_label[spec.id] = step
            step.instruction = spec.instruction
            step.acceptance_criteria = list(spec.acceptance_criteria)
            # A gate is never reviewed, whatever the project's review policy.
            step.review = spec.gate is None and (spec.review or self._execution.review_all)
            step.role = spec.role
            step.review_roles = [StepReviewRole(item.role, item.required) for item in spec.review_roles]
            if spec.gate is not None:
                step.gate = Gate(spec.gate.topic, spec.gate.agenda, spec.gate.findings)
            changed.append(step)
        for spec, step in zip(specs, changed):
            step.depends_on = [by_label[label].step_id for label in spec.depends_on]
        return changed

    def _describe(self, plan: TaskPlan, steps: list[PlanStep]) -> str:
        labels = plan.labels()
        parts = []
        for step in steps:
            notes = []
            if step.depends_on:
                notes.append("after " + "+".join(labels.get(d, d) for d in step.depends_on))
            if step.review:
                notes.append(" + ".join(["review", *(item.label for item in step.review_roles)]))
            if step.role:
                notes.append(f"as {step.role}")
            parts.append(step.label + (f" ({', '.join(notes)})" if notes else ""))
        return ", ".join(parts)

    async def _dispatch_ready(self, plan: TaskPlan) -> None:
        # Every ready gate opens; every ready step starts, and the worker pool runs them within its project's
        # Worker limit.
        ready = plan.ready_steps()
        gates = [step for step in ready if step.gate is not None]
        for gate in gates:
            if plan.status not in _LIVE:
                return
            await self._open_gate(plan, gate)
        if gates:
            await self._report_progress(plan)
        ready = [step for step in ready if step.gate is None]
        base: _RoundBase | None = None
        if ready and self._execution.isolate_steps:
            ready, base = await self._plan_round(plan, ready)
        for step in ready:
            if plan.status not in _LIVE:
                return  # the task stopped at its spend cap
            if step.status == "pending":  # a decision on an earlier step of the round may have started it
                await self._dispatch_step(plan, step, base=base)

    # --- User gates ---

    async def _open_gate(self, plan: TaskPlan, step: PlanStep) -> None:
        """The steps ``step``, a gate, depends on are done: it opens and waits for the user."""
        gate = step.gate
        assert gate is not None
        gate.state, gate.opened_at = GateState.OPEN, datetime.now(timezone.utc)
        step.status = WAITING_USER
        entry = JournalEntry(step.step_id, step.label, journal.clip(
            f"gate opened: {gate.topic} ({len(gate.agenda)} question(s))", journal.SUMMARY_MAX_CHARS),
            kind=JournalKind.GATE_OPENED)
        await self._save(plan, step, entries=[entry],
                         gate_messages=[self._looper_message(step, MessageKind.OPENED, f"Gate opened: {gate.topic}")])
        await self._activity("phase", f"Gate {step.label} opened: {gate.topic}"[:500])

    @staticmethod
    def _looper_message(step: PlanStep, kind: MessageKind, text: str) -> dict[str, Any]:
        """A message of code's in ``step``'s gate's discussion."""
        return {"step_id": step.step_id, "role": MessageRole.LOOPER.value, "kind": kind.value, "text": text}

    async def _read_gates(self, plan: TaskPlan) -> None:
        """Take into ``plan``'s gates what the user's messages changed (an open gate under discussion, the first
        reply's time); nothing to read while none waits."""
        if not plan.waiting_gates():
            return
        try:
            rows = {row["step_id"]: row for row in await db_user_gates.list_for_task(self._task_id)}
        except Exception:
            logger.warning("planner %s cannot read its gates", self.agent_id, exc_info=True)
            return
        for step in plan.waiting_gates():
            row = rows.get(step.step_id)
            if row is not None and step.gate is not None:
                if row.get("state") == GateState.DISCUSSING.value:
                    step.gate.state = GateState.DISCUSSING
                step.gate.first_reply_at = row.get("first_reply_at")

    async def _deliver_gate_messages(self, plan: TaskPlan) -> None:
        """Give Claude the user's messages in each gate that waits, a gate's together in one [GATE] turn, and do
        what it decides. A gate's messages count as delivered only once that turn's reply is taken."""
        if plan.status not in _LIVE or not plan.waiting_gates():
            return
        try:
            rows = await db_user_gates.pending(self._task_id)
        except Exception:
            logger.warning("planner %s cannot read the messages in its gates", self.agent_id, exc_info=True)
            return
        now = datetime.now(timezone.utc)
        for step in plan.waiting_gates():
            gate = step.gate
            if gate is None or not gate.state.waiting or plan.status not in _LIVE:
                continue
            # After the delivery mark in memory too: a turn whose save failed had them.
            messages = [row for row in rows if row["step_id"] == step.step_id and row["id"] > gate.delivered_through]
            # A message no turn failed on goes now; the ones a turn failed on, at their automatic retry.
            if messages and (max(m["id"] for m in messages) > gate.failed_through or _retry_due(gate, now)):
                await self._gate_turn(plan, step, messages)

    async def _gate_turn(self, plan: TaskPlan, step: PlanStep, messages: list[dict[str, Any]]) -> None:
        """One [GATE] turn on ``step``'s gate with the user's ``messages``; a turn that fails tells the user and
        leaves them for its automatic retries (``GATE_RETRY_WAIT_SEC``) and the user's next message."""
        gate = step.gate
        assert gate is not None
        if gate.state is GateState.OPEN:
            gate.state = GateState.DISCUSSING
        forced = any(m.get("kind") == MessageKind.END.value for m in messages)
        others = [s for s in plan.steps if s is not step and s.status not in ("verified", "skipped")]
        context = GateContext(step.label, gate.agenda, forced=forced, last_open=not others)
        view = self._checked(plan.view())
        newest = max(int(m["id"]) for m in messages)
        try:
            reply = await self._ask(TurnKind.GATE, self._gate_message(plan, step, messages, context),
                                    lambda text: parse_gate(text, context, view))
        except PlannerTurnError as exc:
            # The same messages again (an automatic retry) count on; a newer one starts the count again.
            gate.failures = gate.failures + 1 if newest <= gate.failed_through else 1
            gate.failed_through, gate.failed_at = newest, datetime.now(timezone.utc)
            if gate.failures <= len(GATE_RETRY_WAIT_SEC):
                again = (f"it tries again in {max(1, round(GATE_RETRY_WAIT_SEC[gate.failures - 1] / 60))} min, "
                         "and your next message sends it again too.")
            else:
                again = "your message is kept, and your next message sends it again."
            await self._activity("error", f"Planner could not take the messages in gate {step.label} "
                                          f"(failure {gate.failures}): {exc}"[:500])
            await self._save(plan, step, gate_messages=[self._looper_message(
                step, MessageKind.ERROR, f"The Planner could not take this ({exc}); {again}")])
            return
        gate.agenda = reply.agenda
        gate.turns += 1
        gate.delivered_through = newest
        gate.failures, gate.failed_at = 0, None
        answer = {"step_id": step.step_id, "role": MessageRole.PLANNER.value, "kind": MessageKind.REPLY.value,
                  "text": reply.chat.reply}
        if reply.verdict is GateVerdict.CONTINUE:
            await self._save(plan, step, gate_messages=[answer])
            await self._activity("phase", f"Planner answered in gate {step.label} ({reply.chat.action.value}): "
                                          f"{reply.chat.reply}"[:300])
            if reply.chat.action is not ChatAction.NONE:
                await self._apply_chat(plan, reply.chat, None, at=f"gate {step.label}")
            await self._report_progress(plan)
            return
        await self._close_gate(plan, step, reply, forced=forced, answer=answer)

    async def _close_gate(self, plan: TaskPlan, step: PlanStep, reply: GateReply, *, forced: bool,
                          answer: dict[str, Any]) -> None:
        """Close ``step``'s gate with ``reply``'s summary, kept as a supplement in the same transaction, then do the
        reply's action and go on."""
        gate = step.gate
        assert gate is not None and reply.summary is not None
        gate.plan_change = self._plan_change(plan, reply.chat)
        gate.state, gate.summary, gate.closed_at = GateState.PASSED, reply.summary, datetime.now(timezone.utc)
        gate.ended_by_user = forced
        conclusion = conclusion_text(step.label, gate.topic, reply.summary)
        step.status = gate.state.step_status
        step.result = {"status": GateState.PASSED.value, "summary": conclusion}
        if reply.final is not None:
            plan.final_summary = reply.final.markdown()
        summary = reply.summary
        how = "the user ended the discussion" if forced else "the Planner closed it"
        change = ", ".join(f"{name} {'+'.join(labels)}" for name, labels in gate.plan_change.items() if labels)
        closed = JournalEntry(step.step_id, step.label, journal.clip(
            f"gate passed ({how}): {len(summary.decisions)} decision(s), {len(summary.assumptions)} assumption(s), "
            f"{len(summary.open)} open; plan: {change or 'unchanged'}", journal.SUMMARY_MAX_CHARS),
            kind=JournalKind.GATE_CLOSED)
        supplement = JournalEntry(step.step_id, step.label, conclusion, kind=JournalKind.SUPPLEMENT, via="gate")
        at = f"gate {step.label}"
        entries = [supplement, closed]
        added: list[PlanStep] = []
        if reply.chat.action is ChatAction.ADD_STEPS:
            # The steps the answers decide are saved with the close: a gate never passes without them.
            added = self._add_steps(plan, reply.chat.steps)
            entries.append(self._added_entry(plan, added, reply.chat, at))
        await self._save(plan, step, *added, entries=entries, gate_messages=[
            answer, self._looper_message(step, MessageKind.CLOSED, conclusion)])
        await self._activity("phase", f"Gate {step.label} passed ({how}): {len(summary.decisions)} decision(s), "
                                      f"{len(summary.assumptions)} assumption(s); plan: {change or 'unchanged'}"[:500])
        if added:
            await self._activity("phase", f"Steps added at {at}: {self._describe(plan, added)}")
        elif reply.chat.action is not ChatAction.NONE:
            # A replan stops Workers and drops their worktrees before it saves, which no transaction holds.
            await self._apply_chat(plan, reply.chat, None, at=at)
        await self._settle_plan(plan)

    @staticmethod
    def _plan_change(plan: TaskPlan, reply: ChatReply) -> dict[str, list[str]]:
        """The steps a CHAT-shaped reply adds, drops (the steps not started a replan leaves out) and stops."""
        if reply.action is ChatAction.ADD_STEPS:
            return {"added": [spec.id for spec in reply.steps], "dropped": [], "stopped": []}
        if reply.action is not ChatAction.REPLAN:
            return {"added": [], "dropped": [], "stopped": []}
        keep = {spec.id for spec in reply.steps}
        pending = {s.label for s in plan.steps if s.status == "pending"}
        return {"added": [spec.id for spec in reply.steps if spec.id not in pending],
                "dropped": sorted(pending - keep), "stopped": list(reply.stop)}

    async def _apply_gate_changes(self, plan: TaskPlan, changes: tuple[GateChange, ...], at: str) -> bool:
        """Do what a DECISION or CHAT reply's ``gates`` say, ``at`` what the journal says it answered; whether it
        cancelled a gate (the steps that depend on it may start)."""
        cancelled = False
        for change in changes:
            step = next((s for s in plan.steps if s.label == change.id and s.gate is not None), None)
            if step is None or step.gate is None or step.gate.state.closed:
                continue  # checked against the plan the reply answered
            gate = step.gate
            told = gate.state.waiting
            messages: list[dict[str, Any]] = []
            if change.action is GateAction.CANCEL:
                cancelled = True
                gate.state, gate.resolution, gate.closed_at = GateState.CANCELLED, change.resolution, \
                    datetime.now(timezone.utc)
                step.status = gate.state.step_status
                step.result = {"status": GateState.CANCELLED.value, "summary": f"Cancelled: {change.resolution}"}
                entry = JournalEntry(step.step_id, step.label, journal.clip(
                    f"gate cancelled at {at}: {change.resolution}", journal.SUMMARY_MAX_CHARS),
                    kind=JournalKind.GATE_CANCELLED)
                if told:
                    messages.append(self._looper_message(step, MessageKind.CANCELLED,
                                                         f"No answer needed any more: {change.resolution}"))
                activity = f"Gate {step.label} cancelled: {change.resolution}"
            else:
                what = []
                if change.topic is not None:
                    gate.topic = step.instruction = change.topic
                    what.append("topic")
                if change.agenda is not None:
                    gate.agenda = change.agenda
                    what.append("agenda")
                if change.findings is not None:
                    gate.findings = change.findings
                    what.append("findings")
                note = f": {change.note}" if change.note else ""
                entry = JournalEntry(step.step_id, step.label, journal.clip(
                    f"gate updated at {at} ({', '.join(what)}){note}", journal.SUMMARY_MAX_CHARS),
                    kind=JournalKind.GATE_UPDATED)
                if told:
                    messages.append(self._looper_message(step, MessageKind.UPDATED, change.note))
                activity = f"Gate {step.label} updated ({', '.join(what)}){note}"
            await self._save(plan, step, entries=[entry], gate_messages=messages)
            await self._activity("phase", activity[:500])
        return cancelled

    async def _settle_plan(self, plan: TaskPlan) -> None:
        """After a change that may have finished the last open step (a gate closed or cancelled): finish the task,
        or start what is ready."""
        if plan.status not in _LIVE:
            return
        await self._report_progress(plan)
        if plan.has_failed:
            await self._fail_plan(plan)
        elif plan.is_complete:
            await self._complete_plan(plan)
        else:
            await self._dispatch_ready(plan)

    def _idle_waiting(self, plan: TaskPlan) -> bool:
        """Whether the task only waits for the user: a gate waits, and no step is in flight."""
        return bool(plan.waiting_gates()) and not any(s.status in _IN_FLIGHT for s in plan.steps)

    async def _idle_loop(self) -> None:
        """Wake every ``GATE_IDLE_CHECK_SEC`` and post the tick ``_idle_tick`` decides on, in the message loop: a
        retry tick's event ends with the gates' messages, an idle tick stops Claude."""
        while self._running:
            try:
                await asyncio.sleep(GATE_IDLE_CHECK_SEC)
            except asyncio.CancelledError:
                return
            try:
                await self._idle_tick()
            except Exception:
                # The loop drives the gates' automatic retries: it keeps going.
                logger.warning("planner %s: the idle check failed", self.agent_id, exc_info=True)

    async def _idle_tick(self) -> None:
        """Post a ``gate_retry`` tick when a gate's failed turn is due for its retry, else an ``idle`` one when the task
        has only waited for the user for ``GATE_IDLE_STOP_SEC`` with Claude resident."""
        assert self._bus is not None
        plan = self._plan
        if plan is None:
            return
        now = datetime.now(timezone.utc)
        if any(_retry_due(s.gate, now) for s in plan.waiting_gates() if s.gate is not None):
            tick = "gate_retry"
        elif self._claude.started and self._idle_waiting(plan) and \
                time.monotonic() - self._last_turn_at >= GATE_IDLE_STOP_SEC:
            tick = "idle"
        else:
            return
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address, target=self.agent_address, message_type=MessageType.SYSTEM_EVENT,
            payload={"type": tick}))

    async def _stop_idle_claude(self) -> None:
        """Stop Claude while the task only waits for the user; the next turn resumes its session."""
        plan = self._plan
        if plan is None or not self._idle_waiting(plan) or time.monotonic() - self._last_turn_at < GATE_IDLE_STOP_SEC:
            return
        async with self._turn_lock:
            if not self._claude.started:
                return
            await self._claude.stop()
        await self._activity("phase", "Planner Claude Code stopped while the task waits for the user; the next turn "
                                      "resumes its session")

    async def _dispatch_step(self, plan: TaskPlan, step: PlanStep, *, base: _RoundBase | None = None,
                             same_attempt: bool = False) -> None:
        """Start ``step``'s Worker: in the step's worktree when it has one or ``base`` gives it one (a retry after
        a merge that failed gets a new one), else in the task directory; as the current version of its role when
        it has one (the version it ran with, for the ``same_attempt`` a restart runs again)."""
        assert self._bus is not None
        if not await self._within_budget(plan):
            return
        worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        step.status = "dispatched"
        step.worker_id = worker_id
        step.started_at = step.started_at or datetime.now(timezone.utc)
        previous = (step.result or {}) if (step.result or {}).get("status") == "unmerged" else {}
        if previous and step.workspace is not None and \
                not await workspaces.branch_exists(step.workspace.repo_dir, str(previous.get("branch") or "")):
            previous = {}  # the user discarded the attempt's branch: nothing to bring over
        task_role: TaskRole | None = None
        if step.role is not None:
            task_role = await self._role(step.role, step.role_version if same_attempt else None)
            if task_role is None:
                await self._save(plan, step)
                await self._attempt_failed(plan, step, f"Cannot read role {step.role} of step {step.label}, which its "
                                                       "Worker runs as")
                return
            step.role_version = task_role.version
        try:
            await self._open_workspace(plan, step, base)
        except _NoWorkspace as exc:
            await self._save(plan, step)
            await self._workspace_failed(plan, step, str(exc))
            return
        await self._save(plan, step)
        await self._activity("phase", f"Worker dispatched{f' as {task_role.label}' if task_role else ''}: "
                                      f"{step.instruction[:80]}", worker=worker_id)
        context: dict[str, Any] = {"work_dir": plan.work_dir}
        if step.isolated and step.workspace is not None:
            context = {"work_dir": step.workspace.work_dir, "workspace": step.workspace.context()}
            if previous:
                context["workspace"].update(previous_branch=previous.get("branch"),
                                            previous_error=previous.get("merge_error"),
                                            previous_files=list(previous.get("merge_files") or [])[:20])

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=worker_address(worker_id),
            message_type=MessageType.WORK_ITEM,
            payload={
                "task_id": step.step_id,
                "parent_task_id": self._task_id,
                "plan_id": plan.plan_id,
                "step_index": step.index,
                # The Worker's task section: the task and its supplements, then this step and its retry feedback.
                "requirement": plan.requirement,
                "supplements": journal.supplement_texts(plan.journal),
                "step_key": step.label,
                "instruction": step.instruction,
                "target_files": step.target_files,
                "validation_commands": step.validation_commands,
                "acceptance_criteria": step.acceptance_criteria,
                "feedback": list(step.feedback),
                "attempt": step.retries + 1,
                # Its first user turn: what the steps it depends on reported, and the task journal's findings.
                "dependencies": self._dependency_reports(plan, step),
                "findings": journal.worker_section(plan.journal, step.depends_on),
                "context": context,
                # The version of the role its Worker runs as, when it has one.
                **({"task_role": task_role.payload()} if task_role is not None else {}),
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    @staticmethod
    def _dependency_reports(plan: TaskPlan, step: PlanStep) -> list[dict[str, Any]]:
        """What the steps ``step`` depends on reported, for its Worker."""
        reports = []
        for dependency in (plan.step(step_id) for step_id in step.depends_on):
            if dependency is None or not dependency.result:
                continue
            changed = dependency.result.get("changed_files")
            reports.append({
                "step": dependency.label,
                "summary": _clip(dependency.result.get("summary"), 1000),
                "changed_files": [str(path) for path in changed][:50] if isinstance(changed, dict) else [],
            })
        return reports

    # --- Step workspaces (a project with step_workspaces = worktree) ---

    async def _plan_round(self, plan: TaskPlan, ready: list[PlanStep]) -> tuple[list[PlanStep], _RoundBase | None]:
        """Which of the ready steps start now (``workspaces.plan_round``), and where the new worktrees among them
        start (None: none of them needs one)."""
        repo = await self._task_repo(plan)
        decided = workspaces.plan_round(in_flight=[s.isolated for s in plan.steps if s.status in _IN_FLIGHT],
                                        ready=[s.isolated for s in ready], repo=repo)
        if decided.note and decided.note != self._round_note:
            await self._activity("phase", decided.note[:500])
        self._round_note = decided.note
        ready = ready[: decided.start]
        if not decided.isolate or not ready or not isinstance(repo, Repo):
            return ready, None
        if all(s.workspace is not None and s.workspace.state is StepWorkspaceState.ACTIVE for s in ready):
            return ready, None  # they carry on in their worktrees
        return ready, await self._round_base(repo)

    async def _task_repo(self, plan: TaskPlan) -> Repo | Unusable:
        """The task directory as step workspaces see it now; the first time it can have them, where its branch
        starts is recorded (undoing the task's commits goes back there)."""
        repo = await workspaces.inspect(plan.work_dir)
        if isinstance(repo, Repo) and not plan.start_commit:
            plan.start_branch, plan.start_commit = repo.branch, repo.head
            await self._save(plan, plan_row=True)
            await self._activity("phase", f"Step workspaces: the task starts with {workspaces.short_branch(repo.branch)} "
                                          f"at {repo.head[:12]}; `git reset {repo.head[:12]}` in the task directory "
                                          "undoes the commits the task adds")
        return repo

    async def _round_base(self, repo: Repo) -> _RoundBase:
        """The commit a round's new worktrees start from: the branch's tip, after a checkpoint commit of the task
        directory's pending changes when it has any."""
        try:
            commit, files = await workspaces.checkpoint(repo, task_id=self._task_id)
        except workspaces.GitError as exc:
            return _RoundBase(error=f"cannot commit the task directory's pending changes as a checkpoint: {exc}")
        if files:
            await self._activity("phase", f"Checkpoint {commit[:12]} on {workspaces.short_branch(repo.branch)}: "
                                          f"{files} pending file(s) of the task directory committed for the steps "
                                          "that start now")
        return _RoundBase(repo=repo, commit=commit, checkpoint=bool(files))

    async def _open_workspace(self, plan: TaskPlan, step: PlanStep, base: _RoundBase | None) -> None:
        """Give ``step`` its worktree: the one it works in, or a new one from ``base`` (a retry after a merge that
        failed gets one from the branch as it is now; the old branch stays for its commits). Nothing for a step
        that works in the task directory. Raises ``_NoWorkspace``."""
        current = step.workspace
        if current is not None and current.state is StepWorkspaceState.ACTIVE:
            try:
                await workspaces.ensure_worktree(current)
            except workspaces.GitError as exc:
                raise _NoWorkspace(str(exc)) from None
            return
        if current is not None and current.state is StepWorkspaceState.UNMERGED:
            step.workspace = await self._discard(current, keep_branch=True)
            if base is None:
                repo = await self._task_repo(plan)
                base = await self._round_base(repo) if isinstance(repo, Repo) else _RoundBase(error=repo.reason)
        elif base is None:
            return
        if base.error or base.repo is None:
            raise _NoWorkspace(base.error)
        try:
            workspace = workspaces.new_workspace(
                base.repo, runtime_root=self._runtime_root, workspace_id=await db_step_workspaces.next_id(),
                task_id=self._task_id, step_id=step.step_id, step_key=step.label, base=base.commit,
                checkpointed=base.checkpoint)
            await db_step_workspaces.save(workspace.row())
        except Exception as exc:
            raise _NoWorkspace(f"cannot record its workspace: {exc}") from None
        step.workspace = workspace
        try:
            await workspaces.ensure_worktree(workspace)
        except workspaces.GitError as exc:
            step.workspace = await self._discard(workspace)
            raise _NoWorkspace(str(exc)) from None
        skipped: list[str] = []
        if self._execution.link_paths:
            workspace, skipped = workspaces.link(workspace, self._execution.link_paths)
            step.workspace = workspace
            await self._save_workspace(workspace)
        linked = f"; linked to the task directory's: {', '.join(workspace.links)}" if workspace.links else ""
        await self._activity("phase", f"Step {step.label} works in a worktree of its own: branch {workspace.branch}, "
                                      f"from {workspace.target} at {workspace.base_commit[:12]}{linked}"[:500],
                             worker=step.worker_id)
        if skipped:
            await self._activity("phase", f"Step {step.label}'s worktree did not link {'; '.join(skipped)}"[:500],
                                 worker=step.worker_id)

    async def _workspace_failed(self, plan: TaskPlan, step: PlanStep, problem: str) -> None:
        """A step that needed a worktree got none: the attempt failed, and the Planner decides what follows."""
        await self._attempt_failed(plan, step, f"Cannot create a worktree for step {step.label}: {problem}")

    async def _attempt_failed(self, plan: TaskPlan, step: PlanStep, error: str) -> None:
        """A step's attempt could not start (``error`` says why): it failed, and the Planner decides what follows."""
        result = {"status": "failed", "error": error}
        step.result = result
        await self._activity("error", result["error"][:500], worker=step.worker_id)
        context = DecisionContext(step=step.label, result=ResultKind.WORK, succeeded=False,
                                  review_required=self._needs_review(step), retries=step.retries,
                                  max_retries=self._execution.step_retries)
        await self._decide(plan, step, context, self._work_report(step, result))

    async def _merged(self, plan: TaskPlan, step: PlanStep, *, decide: bool = True) -> bool:
        """Merge the worktree of ``step``, just accepted, into the task directory's branch; True when it has
        none or the merge landed. A merge that failed leaves the task directory as it was and goes to the
        Planner's decision instead (False); without ``decide`` it only leaves the step's result unmerged."""
        current = step.workspace
        if current is None or current.state not in (StepWorkspaceState.ACTIVE, StepWorkspaceState.MERGING):
            return True
        outcome = await workspaces.merge(current, step_key=step.label, title=_clip(step.instruction, 72),
                                         save=self._save_workspace)
        merged = step.workspace = outcome.workspace
        if outcome.leftovers:
            await self._activity("phase", f"Step {step.label} left changes uncommitted: committed on its branch as "
                                          f"{outcome.leftovers[:12]}")
        if outcome.replaced_links:
            replaced = ", ".join(outcome.replaced_links)
            await self._activity("phase", f"Step {step.label} replaced links to the task directory with files of its "
                                          f"own, which are part of its result: {replaced}"[:500])
        if merged.state is not StepWorkspaceState.MERGED:
            await self._merge_failed(plan, step, outcome, decide=decide)
            return False
        if merged.merge_commit and outcome.files:
            note = f"Step {step.label} merged into {merged.target} as {merged.merge_commit[:12]}: " \
                   f"{len(outcome.files)} file(s)"
        elif merged.merge_commit:
            note = f"Step {step.label} merged into {merged.target} as {merged.merge_commit[:12]}"
        else:
            note = f"Step {step.label} changed nothing to merge into {merged.target}"
        await self._activity("phase", note)
        step.workspace = await self._remove_workspace(merged)  # its commits are on the branch now
        return True

    async def _merge_failed(self, plan: TaskPlan, step: PlanStep, outcome: MergeResult, *, decide: bool = True) -> None:
        failed = outcome.workspace
        assert failed.merge_error is not None
        error = (f"Step {step.label} was accepted, but its branch could not be merged into {failed.target} "
                 f"({failed.merge_error.value}): {failed.detail}")
        step.result = {**(step.result or {}), "status": "unmerged", "error": error,
                       "merge_error": failed.merge_error.value, "merge_files": list(outcome.files[:50]),
                       "branch": failed.branch}
        await self._activity("error", error[:500], worker=step.worker_id)
        if not decide:
            return
        context = DecisionContext(step=step.label, result=ResultKind.WORK, succeeded=False,
                                  review_required=self._needs_review(step), retries=step.retries,
                                  max_retries=self._execution.step_retries)
        await self._decide(plan, step, context, self._merge_report(step, failed, outcome.files))

    @staticmethod
    def _merge_report(step: PlanStep, failed: StepWorkspace, files: Sequence[str]) -> list[str]:
        assert failed.merge_error is not None
        lines = [
            f"Step {step.label} was accepted, but Looper could not merge its branch {failed.branch} into "
            f"{failed.target} in the task directory:",
            f"- why: {_MERGE_REASONS[failed.merge_error]} ({failed.merge_error.value})",
            f"- detail: {_clip(failed.detail, 500)}",
        ]
        if files:
            lines.append(f"- files: {', '.join(files[:20])}" + (f" (+{len(files) - 20} more)" if len(files) > 20 else ""))
        lines.append(f"The task directory is as it was before the merge. A retry starts from {failed.target} as it is "
                     f"now, and its Worker is told the previous attempt's branch {failed.branch} to bring the work over.")
        return lines

    async def _finish_merge(self, plan: TaskPlan, step: PlanStep) -> None:
        """Finish the merge of an accepted step that a restart cut off, then settle the step."""
        if await self._merged(plan, step):
            step.status = "verified"
            await self._step_settled(plan, step)

    async def _drop_workspace(self, step: PlanStep) -> None:
        """A step dropped from the plan: its worktree and branch go with it."""
        if step.isolated and step.workspace is not None:
            step.workspace = await self._discard(step.workspace)

    async def _discard(self, workspace: StepWorkspace, *, keep_branch: bool = False) -> StepWorkspace:
        """Drop a workspace without merging it: its directory goes, and its branch unless ``keep_branch``."""
        if StepWorkspaceState.DISCARDED in workspaces.ALLOWED_TRANSITIONS[workspace.state]:
            workspace = workspace.moved(StepWorkspaceState.DISCARDED)
        return await self._remove_workspace(workspace, keep_branch=keep_branch)

    async def _remove_workspace(self, workspace: StepWorkspace, *, keep_branch: bool = False) -> StepWorkspace:
        try:
            workspace = await workspaces.remove(workspace, keep_branch=keep_branch)
        except workspaces.GitError:
            logger.warning("planner %s cannot remove the worktree %s", self.agent_id, workspace.path, exc_info=True)
        await self._save_workspace(workspace)
        return workspace

    async def _remove_workspaces(self) -> None:
        """Remove every worktree and step branch the task still has (it completed)."""
        try:
            removed = await workspaces.remove_task(self._task_id)
        except Exception as exc:
            logger.warning("planner %s cannot remove its step worktrees", self.agent_id, exc_info=True)
            await self._activity("error", f"Warning: the task's step worktrees were not all removed: {exc}"[:500])
            return
        if removed:
            await self._activity("phase", f"Removed the task's {removed} step worktree(s) and their branches")

    async def _save_workspace(self, workspace: StepWorkspace) -> None:
        """Write a workspace's row; a failed write is logged, and only a restart would miss it."""
        try:
            await db_step_workspaces.save(workspace.row())
        except Exception:
            logger.exception("failed to save step workspace %s of task %s", workspace.id, self._task_id)

    # --- Results and decisions ---

    async def _handle_work_result(self, envelope: MessageEnvelope) -> None:
        result = envelope.payload
        plan = self._plan
        if not plan or plan.status not in ("executing", "verifying"):
            return
        step = plan.step(result.get("task_id", ""))
        if step is None or step.status != "dispatched" or envelope.source.agent_id != step.worker_id:
            logger.info("planner %s ignores a result that is not for a running step", self.agent_id)
            return

        step.result = result
        await self._capture_project_learnings(plan, step, result.get("learnings"), "Worker")
        succeeded = result.get("status") == "completed"
        await self._activity(
            "text" if succeeded else "error",
            str(result.get("summary") or result.get("error") or result.get("status") or "")[:200],
            worker=step.worker_id,
        )
        context = DecisionContext(
            step=step.label, result=ResultKind.WORK, succeeded=succeeded,
            review_required=self._needs_review(step), retries=step.retries,
            max_retries=self._execution.step_retries,
        )
        await self._decide(plan, step, context, self._work_report(step, result))

    async def _spawn_reviewer(self, plan: TaskPlan, step: PlanStep, *, same_attempt: bool = False) -> None:
        """Start the review ``step`` waits for (``review_stage``): the built-in Reviewer's, or that of its review
        role at ``review_position`` (the version it ran with, for the ``same_attempt`` a restart runs again)."""
        assert self._bus is not None
        if not await self._within_budget(plan):
            return
        task_role: TaskRole | None = None
        if (item := self._reviewing_role(step)) is not None:
            task_role = await self._role(item.role, item.version if same_attempt else None)
            if task_role is None:
                step.reviewer_id = None
                await self._role_review_done(plan, step, item, {
                    "passed": False, "summary": (problem := f"Cannot read role {item.role}, which reviews step "
                                                           f"{step.label}"), "findings": [problem], "learnings": []})
                return
            item.version = task_role.version
        elif step.review_stage is None:
            step.review_stage = ReviewStage.BUILTIN
        reviewer_id = f"reviewer-{uuid.uuid4().hex[:12]}"
        step.reviewer_id = reviewer_id
        await self._save(plan, step)
        await self._activity("phase", f"Reviewer dispatched for step {step.index + 1} ({step.label})"
                                      + (f" as {task_role.label}" if task_role else ""), worker=reviewer_id)
        context: dict[str, Any] = {"work_dir": plan.work_dir}
        if step.isolated and step.workspace is not None:
            # It reviews the step's own result, in the step's worktree.
            context = {"work_dir": step.workspace.work_dir, "workspace": step.workspace.context()}

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=worker_address(reviewer_id),
            message_type=MessageType.VERIFY_REQUEST,
            payload={
                "task_id": step.step_id,
                "parent_task_id": self._task_id,
                "plan_id": plan.plan_id,
                "requirement": plan.requirement,
                "supplements": journal.supplement_texts(plan.journal),
                "step_key": step.label,
                "instruction": step.instruction,
                "work_result": step.result or {},
                "acceptance_criteria": step.acceptance_criteria,
                "context": context,
                # The version of the review role that reviews the step now, when that is the review it waits for.
                **({"task_role": task_role.payload()} if task_role is not None else {}),
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _handle_verify_result(self, envelope: MessageEnvelope) -> None:
        result = envelope.payload
        plan = self._plan
        if not plan or plan.status not in ("executing", "verifying"):
            return
        step = plan.step(result.get("task_id", ""))
        if step is None or step.status != "completed" or envelope.source.agent_id != step.reviewer_id:
            logger.info("planner %s ignores a review that is not for a step in review", self.agent_id)
            return

        passed = result.get("passed") is True
        findings = result.get("findings") or []
        await self._capture_project_learnings(plan, step, result.get("learnings"), "Reviewer")
        if (item := self._reviewing_role(step)) is not None:
            await self._role_review_done(plan, step, item, {
                "passed": passed, "findings": findings, "summary": result.get("summary", ""),
                "learnings": journal.reported_learnings(result.get("learnings"))})
            return
        step.result = {**(step.result or {}), "verify_passed": passed, "verify_findings": findings,
                       "verify_summary": result.get("summary", ""),
                       "verify_learnings": journal.reported_learnings(result.get("learnings"))}
        await self._activity(
            "text" if passed else "error",
            f"Verify {'passed' if passed else 'failed'}: {str(result.get('summary') or '')[:150]}",
            worker=step.reviewer_id,
        )
        if passed and step.review_roles:
            # The built-in Reviewer passed it: its review roles review it too, in order, before any decision.
            step.review_stage, step.review_position = ReviewStage.ROLE, 0
            await self._spawn_reviewer(plan, step)
            return
        context = DecisionContext(
            step=step.label, result=ResultKind.REVIEW, succeeded=passed,
            review_required=self._needs_review(step), retries=step.retries,
            max_retries=self._execution.step_retries,
        )
        await self._decide(plan, step, context, self._review_report(step, result))

    @staticmethod
    def _reviewing_role(step: PlanStep) -> StepReviewRole | None:
        """The review role whose review ``step`` waits for; None while it waits for the built-in Reviewer's."""
        position = step.review_position
        if step.review_stage is not ReviewStage.ROLE or position is None or not 0 <= position < len(step.review_roles):
            return None
        return step.review_roles[position]

    async def _role_review_done(self, plan: TaskPlan, step: PlanStep, item: StepReviewRole,
                                verdict: Mapping[str, Any]) -> None:
        """Keep review role ``item``'s ``verdict`` on ``step`` (its review ran, or could not start), then go on: the
        next review role reviews it, unless this one was required and did not pass or was the last; then the
        Planner decides, the step acceptable only when every required review passed."""
        passed = verdict.get("passed") is True
        step.result = {**(step.result or {}), "role_reviews": [
            *_role_reviews(step.result or {}),
            {"role": item.role, "version": item.version, "required": item.required, **verdict}]}
        await self._activity(
            "text" if passed else "error",
            f"Review by role {item.role}" + (f" v{item.version}" if item.version else "")
            + f"{'' if item.required else ' (advisory)'} {'passed' if passed else 'failed'}: "
            f"{str(verdict.get('summary') or '')[:150]}", worker=step.reviewer_id,
        )
        position = step.review_position or 0
        if (passed or not item.required) and position + 1 < len(step.review_roles):
            step.review_position = position + 1
            await self._spawn_reviewer(plan, step)
            return
        await self._save(plan, step)
        context = DecisionContext(
            step=step.label, result=ResultKind.REVIEW, succeeded=self._roles_acceptable(step),
            review_required=self._needs_review(step), retries=step.retries,
            max_retries=self._execution.step_retries,
        )
        await self._decide(plan, step, context, self._review_report(step, verdict))

    @staticmethod
    def _roles_acceptable(step: PlanStep) -> bool:
        """Whether every required review role's verdict on ``step``'s result passed (the built-in Reviewer passed
        it, or none of them would have reviewed it); an advisory one's never blocks."""
        return all(r.get("passed") is True for r in _role_reviews(step.result or {}) if r.get("required") is not False)

    async def _decide(self, plan: TaskPlan, step: PlanStep, context: DecisionContext, report: list[str]) -> None:
        """Ask the Planner's Claude what to do with ``step``'s result, then do it.

        Without a valid decision the step's own verdict decides, and no
        Reviewer is spawned.
        """
        view = plan.view()
        others = [s for s in plan.steps if s is not step and s.status not in ("verified", "skipped")]
        open_gates = frozenset(s.label for s in others) if others and all(s.gate for s in others) else None
        context = replace(context, last_open=plan.last_open(step), open_gates=open_gates)
        step.status = "deciding"
        message = self._decision_message(plan, step, context, report)
        # Saved with its result and the late messages, so a restart can take the decision again.
        await self._save(plan, step)
        step.missed_messages.clear()
        try:
            reply = await self._ask(
                TurnKind.DECISION, message, lambda text: parse_decision(text, context, self._checked(view)),
            )
        except PlannerTurnError as exc:
            if self._execution.review_all and context.result is ResultKind.WORK and context.succeeded:
                # The project reviews every step, so a Worker's own success never accepts one.
                await self._activity(
                    "error",
                    f"Warning: the Planner could not decide on step {step.index + 1} ({step.label}): {exc}. "
                    "This project reviews every step: it goes to a Reviewer.",
                )
                await self._send_to_review(plan, step)
                return
            whose = "Reviewer's" if context.result is ResultKind.REVIEW else "Worker's own"
            if context.succeeded and not await self._merged(plan, step):
                return
            step.status = "verified" if context.succeeded else "failed"
            await self._activity(
                "error",
                f"Warning: the Planner could not decide on step {step.index + 1} ({step.label}): {exc}. "
                f"The {whose} verdict decides: {step.status}.",
            )
            await self._step_settled(plan, step, note=f"The Planner could not decide on it; the {whose} verdict "
                                                      "failed it.")
            return
        await self._apply_decision(plan, step, context, reply)

    async def _apply_decision(
        self, plan: TaskPlan, step: PlanStep, context: DecisionContext, reply: DecisionReply,
    ) -> None:
        """Do what a DECISION reply decided: its changes to gates first, then the decision on ``step``."""
        cancelled = await self._apply_gate_changes(plan, reply.gates, f"the decision on step {step.label}")
        await self._decided(plan, step, context, reply)
        if cancelled and plan.status in _LIVE:
            # A decision that settles no step (a review, a retry) still lets the cancelled gate's dependents start.
            await self._report_progress(plan)
            await self._dispatch_ready(plan)

    async def _decided(
        self, plan: TaskPlan, step: PlanStep, context: DecisionContext, reply: DecisionReply,
    ) -> None:
        decision = reply.decision
        note = f": {reply.reason}" if reply.reason else ""
        if decision is Decision.RETRY:
            note = f": {reply.feedback}"
        await self._activity("phase", f"Planner decision on step {step.index + 1} ({step.label}): {decision.value}{note}"[:500])
        if reply.progress is not None:
            await self._record_progress(plan, reply.progress)
        keeps_work = decision is Decision.ACCEPT or (decision is Decision.REPLAN and context.acceptable)
        if keeps_work and not await self._merged(plan, step):
            return  # the decision turn on the failed merge took over
        if reply.final is not None:
            plan.final_summary = reply.final.markdown()

        if decision is Decision.ACCEPT:
            step.status = "verified"
            if reply.summary and reply.final is None:
                plan.final_summary = reply.summary
        elif decision is Decision.REVIEW:
            await self._send_to_review(plan, step)
            return
        elif decision is Decision.RETRY:
            # The attempt's pitfalls, saved with the retry.
            retried = self._step_entry(
                JournalKind.STEP_RETRY, step, f"attempt {step.retries + 1} {self._outcome(step)}. "
                                              f"Retry: {reply.feedback}")
            step.retries += 1
            step.feedback.append(reply.feedback)
            step.reviewer_id, step.review_stage, step.review_position = None, None, None
            await self._save(plan, step, entries=[retried])
            await self._dispatch_step(plan, step)
            await self._report_progress(plan)
            return
        elif decision is Decision.REPLAN:
            step.status = "verified" if context.acceptable else "skipped"
            if step.status == "skipped":
                await self._drop_workspace(step)
            await self._replan(plan, reply.steps, step, reply.reason)
        elif decision is Decision.FAIL:
            step.status = "failed"
            plan.fail_reason = reply.reason
            await self._step_settled(plan, step, note=f"The Planner failed the task: {reply.reason}")
            return
        await self._step_settled(plan, step)

    async def _send_to_review(self, plan: TaskPlan, step: PlanStep) -> None:
        step.review = True
        step.status = "completed"
        step.review_stage, step.review_position = ReviewStage.BUILTIN, None
        # A new review chain: the review roles' verdicts on this result from before (a restart) no longer stand.
        step.result = {k: v for k, v in (step.result or {}).items() if k != "role_reviews"} if step.result else None
        await self._save(plan, step)
        await update_task_phase(self._task_id, "verifying")
        await self._spawn_reviewer(plan, step)
        await self._report_progress(plan)

    def _needs_review(self, step: PlanStep) -> bool:
        """Whether ``step`` needs a passing review before it is accepted: the Planner asked for one, or the
        project reviews every step."""
        return step.review or self._execution.review_all

    @staticmethod
    def _skip(step: PlanStep) -> None:
        """Drop ``step``, which has not started (a gate that has not opened with it)."""
        step.status = "skipped"
        if step.gate is not None:
            step.gate.state = GateState.SKIPPED

    async def _replan(self, plan: TaskPlan, specs: tuple[StepSpec, ...], decided: PlanStep, reason: str) -> None:
        """Replace every step that has not started with ``specs``; saved with the ``decided`` step and the
        decision's journal entry at once."""
        keep = {spec.id for spec in specs}
        dropped = [s for s in plan.steps if s.status == "pending" and s.label not in keep]
        for step in dropped:
            self._skip(step)
            await self._drop_workspace(step)
        steps = self._add_steps(plan, specs)
        summary = f"Plan changed: {self._describe(plan, steps) or 'no new steps'}"
        if dropped:
            summary += f"; dropped {', '.join(s.label for s in dropped)}"
        note = f"replan: {reason or 'no reason given'}. {summary}"
        if decided.status == "skipped":
            # The step's attempt is dropped: what went wrong with it and what it learned stay in the entry.
            entry = self._step_entry(JournalKind.DECISION, decided, f"{note}. The step was dropped "
                                                                    f"({self._outcome(decided)})")
        else:
            entry = JournalEntry(decided.step_id, decided.label, journal.clip(note, journal.SUMMARY_MAX_CHARS),
                                 kind=JournalKind.DECISION)
        await self._save(plan, decided, *dropped, *steps, entries=[entry])
        await self._activity("phase", summary)

    async def _step_settled(self, plan: TaskPlan, step: PlanStep, *, note: str = "") -> None:
        """Save ``step``'s outcome, then finish the task or start the steps now ready.

        An accepted or failed step's journal entry is saved with it, in one
        transaction; ``note`` says why a step failed.
        """
        entry: JournalEntry | None = None
        if step.status == "verified":
            entry = self._accepted_entry(plan, step)
        elif step.status == "failed":
            outcome = self._outcome(step)
            entry = self._step_entry(JournalKind.STEP_FAILED, step, f"{outcome}. {note}" if note else outcome)
        # An accept's summary or a fail's reason, and the journal entry.
        await self._save(plan, step, plan_row=True, entries=[entry] if entry else [])
        await self._report_progress(plan)
        if plan.has_failed:
            await self._fail_plan(plan)
        elif plan.is_complete:
            await self._complete_plan(plan)
        else:
            await self._dispatch_ready(plan)

    @classmethod
    def _accepted_entry(cls, plan: TaskPlan, step: PlanStep) -> JournalEntry | None:
        """The journal entry of ``step``, just accepted; None if it has one."""
        if any(entry.step_id == step.step_id and entry.kind is JournalKind.STEP_ACCEPTED for entry in plan.journal):
            return None
        summary = str((step.result or {}).get("summary") or "")
        for advice in _role_reviews(step.result or {}):
            if advice.get("passed") is True:
                continue
            # Accepted over an advisory review role's verdict: the entry says so.
            note = f"Accepted although the advisory review by role {advice.get('role')} did not pass: " \
                   f"{_verdict_reason(advice, 'findings', 'summary')}"
            summary = f"{_sentence(summary)} {note}" if summary else note
        return cls._step_entry(JournalKind.STEP_ACCEPTED, step, summary)

    @staticmethod
    def _step_entry(kind: JournalKind, step: PlanStep, summary: Any) -> JournalEntry:
        """A journal entry of ``kind`` for ``step``, with the learnings of its last result (its Worker's and,
        after a review, its Reviewer's)."""
        result = step.result or {}
        return JournalEntry(
            step_id=step.step_id, step=step.label, kind=kind,
            summary=journal.clip(summary, journal.SUMMARY_MAX_CHARS),
            learnings=journal.merged_learnings(journal.learnings(result.get("learnings")),
                                               journal.learnings(result.get("verify_learnings")),
                                               *(journal.learnings(r.get("learnings")) for r in _role_reviews(result))),
        )

    @staticmethod
    def _outcome(step: PlanStep) -> str:
        """What ``step``'s last result was, in a line: what a retry or a failure is about."""
        return result_outcome(step.result or {})

    async def _capture_project_learnings(self, plan: TaskPlan, step: PlanStep, value: Any, who: str) -> None:
        """Save the learnings ``who`` (the step's Worker or Reviewer) reported as holding for the whole project as
        a draft of the project's knowledge, as they arrive, when the project distills lessons (``draft`` or
        ``write``; single learnings are unreviewed, so ``write`` drafts them too). With ``off`` they stay the
        task's. A draft that cannot be saved is a warning in the activity."""
        items = [text for text in journal.project_learnings(value) if text not in self._drafted]
        if not items or not plan.project_id or not self._execution.lessons_on:
            return
        self._drafted.update(items)
        where = f"step {step.label} of task {self._task_id}"
        try:
            await project_knowledge.add_draft(
                plan.project_id, task_id=self._task_id,
                title=_clip(f"Learnings from step {step.label}: {plan.requirement}",
                            project_knowledge.TITLE_MAX_CHARS),
                summary=_clip(f"What the {who} of {where} found that holds for the whole project",
                              project_knowledge.SUMMARY_MAX_CHARS),
                content="\n".join([f"# Learnings from step {step.label}", "", *(f"- {text}" for text in items), "",
                                   f"Reported by the {who} of {where} ({_clip(plan.requirement, 200)})."]),
            )
        except Exception as exc:
            logger.warning("planner %s cannot save project learnings as a draft", self.agent_id, exc_info=True)
            await self._activity("error", f"Warning: the project learnings of step {step.index + 1} ({step.label}) "
                                          f"were not saved as a draft: {exc}"[:500])
            return
        await self._activity("phase", f"{len(items)} project learning(s) from the {who} of step {step.index + 1} "
                                      f"({step.label}) saved as a draft to review in project knowledge")

    # --- Resuming after a server restart ---

    async def _resume(self, envelope: MessageEnvelope) -> None:
        """Carry on with the plan a previous Planner of this task saved: no new plan, the same session.

        The server's stop ended every run of the task: a step whose Worker was
        running runs again (the same attempt, its retries and feedback kept), a
        step in review gets a new Reviewer, and a decision that was cut off is
        taken again from the step's saved result.
        """
        assert self._bus is not None
        plan = self._plan
        if plan is None:
            await self._bus.publish(MessageEnvelope.create(
                source=self.agent_address,
                target=TASK_ROUTER_ADDRESS,
                message_type=MessageType.GOAL_STATUS,
                payload={"task_id": self._task_id, "status": "failed",
                         "summary": "Cannot resume after a server restart: the saved plan cannot be read"},
                correlation_id=envelope.correlation_id,
            ))
            return
        if plan.status in ("completed", "failed"):
            return  # it ended before this turn (at its spend cap)
        if plan.status not in _LIVE:
            await self._fail_plan(plan, reason=f"Cannot resume after a server restart: the plan was {plan.status}")
            return
        await self._activity("phase", f"Planner resumed after a server restart ({plan.progress_summary()})")
        cut_off = [(step, step.status) for step in plan.steps if step.status in ("dispatched", "completed", "deciding")]
        for step, status in cut_off:
            if plan.status not in _LIVE:
                return  # it stopped at its spend cap
            if step.status != status:
                continue
            if status == "dispatched":
                await self._dispatch_step(plan, step, same_attempt=True)
            elif status == "completed":
                await self._spawn_reviewer(plan, step, same_attempt=True)
        for step, status in cut_off:
            if plan.status not in _LIVE:
                return
            if status == "deciding" and step.status == status:
                if step.workspace is not None and step.workspace.state is StepWorkspaceState.MERGING:
                    await self._finish_merge(plan, step)  # it was accepted: only its merge was cut off
                else:
                    await self._decide_again(plan, step)
        if plan.status not in _LIVE:
            return
        await self._report_progress(plan)
        if plan.has_failed:
            await self._fail_plan(plan)
        elif plan.is_complete:
            await self._complete_plan(plan)
        else:
            await self._dispatch_ready(plan)

    async def _decide_again(self, plan: TaskPlan, step: PlanStep) -> None:
        """Take the decision on ``step`` that a restart cut off, from the result saved with it."""
        result = step.result or {}
        if role_reviews := _role_reviews(result):  # its review roles' verdicts, after the built-in Reviewer passed
            passed = self._roles_acceptable(step)
            kind = ResultKind.REVIEW
            report = self._review_report(step, role_reviews[-1])
        elif "verify_passed" in result:  # a Reviewer's verdict, added to the Worker's result
            passed = result.get("verify_passed") is True
            kind = ResultKind.REVIEW
            report = self._review_report(step, {"passed": passed, "summary": result.get("verify_summary"),
                                                "findings": result.get("verify_findings"),
                                                "learnings": result.get("verify_learnings")})
        else:
            passed = result.get("status") == "completed"
            kind = ResultKind.WORK
            report = self._work_report(step, result)
        context = DecisionContext(
            step=step.label, result=kind, succeeded=passed,
            review_required=self._needs_review(step), retries=step.retries,
            max_retries=self._execution.step_retries,
        )
        await self._decide(plan, step, context, report)

    # --- Restarting a task that ended: stopped, failed or cancelled ---

    async def _restart_plan(self, envelope: MessageEnvelope) -> None:
        """Run a task that ended again from its saved plan (``RestartMode.CONTINUE``), building on what is done.

        The accepted steps stay done and never run again (one whose merge the end cut off is merged now). Every
        other step that did not finish (failed, running, in review, awaiting a decision) goes back to not
        started, with its retries, feedback, last result and worktree, as a retry keeps them, and a journal
        entry on how its last attempt ended. Then one [RESTART] turn, answered in the CHAT shape: ``none`` runs
        the steps not started as they are, ``add_steps`` and ``replan`` change them first; a turn that fails
        leaves them as they are. A task without a plan to go on from is planned anew.
        """
        self._plan = None
        await self._load_plan(db_plans.STATUSES)
        plan = self._plan
        if plan is None or not plan.steps:
            await self._activity("phase", "Restarted without a saved plan to continue from: planning anew")
            await self._plan_and_dispatch(envelope)
            return
        await self._refresh_project(plan)
        why = self._ended(str(envelope.payload.get("ended") or ""), plan.fail_reason)
        plan.status, plan.fail_reason = "executing", ""
        if not await self._within_budget(plan):
            return  # its spend cap is still spent, so it failed at it again: ``/continue`` raises the cap
        merged: list[PlanStep] = []
        for step in plan.steps:
            if step.status == "deciding" and step.workspace is not None and \
                    step.workspace.state is StepWorkspaceState.MERGING and await self._merged(plan, step, decide=False):
                step.status = "verified"  # it was accepted: only its merge was cut off
                merged.append(step)
        unfinished = [s for s in plan.steps if s.status in ("failed", *_IN_FLIGHT)]
        entries = [entry for step in merged if (entry := self._accepted_entry(plan, step)) is not None]
        entries += [self._restart_entry(step) for step in unfinished]
        # A gate that waits for the user keeps waiting, with its discussion.
        again = [s for s in plan.steps if s.gate is None and s.status not in ("verified", "skipped")
                 and (s.status != "pending" or s.started)]
        for step in again:
            step.status, step.worker_id, step.reviewer_id = "pending", None, None
            step.review_stage, step.review_position = None, None
            step.started_at = None  # its next dispatch starts it again: the time the task was stopped does not count
        waiting = plan.waiting_gates()
        for step in waiting:
            assert step.gate is not None
            # The restart gives the messages kept in it, whatever turns failed on them before.
            step.gate.failures, step.gate.failed_through, step.gate.failed_at = 0, step.gate.delivered_through, None
        await self._save(plan, *merged, *again, *waiting, plan_row=True, entries=entries, gate_messages=[
            self._looper_message(step, MessageKind.RESUMED, "The task was restarted; this gate still waits for you.")
            for step in waiting])
        await update_task_phase(self._task_id, "executing")
        kept = sum(1 for s in plan.steps if s.status == "verified")
        await self._activity("phase", f"Task restarted from its plan: {kept} accepted step(s) kept"
                                      + (f"; back to not started: {', '.join(s.label for s in unfinished)}"
                                         if unfinished else ""))
        if plan.is_complete:
            await self._complete_plan(plan)
            return
        changes = await self._pending_changes()
        await self._read_changes(plan, changes)
        view = plan.view()
        try:
            message = self._restart_message(plan, why, changes, await self._template_lines(plan))
            reply: ChatReply | None = await self._ask(TurnKind.RESTART, message,
                                                      lambda answer: parse_chat(answer, self._checked(view)))
        except PlannerTurnError as exc:
            await self._activity("error", f"Warning: the Planner could not take the restart: {exc}. The steps not "
                                          "started run as they are."[:500])
            reply = None
        cancelled = await self._apply_gate_changes(plan, reply.gates, "the restart") if reply is not None else False
        if reply is not None and reply.final is not None:
            plan.final_summary = reply.final.markdown()
        if reply is None or reply.action is ChatAction.NONE:
            await self._report_progress(plan)
            await self._dispatch_ready(plan)
        else:
            await self._apply_chat(plan, reply, None, at="the restart")
        if reply is not None:
            await self._activity("phase", f"Planner answered the restart ({reply.action.value}): {reply.reply}"[:300])
            # Before the task may end below, which first gives the Planner what came meanwhile.
            await self._mark_delivered(changes)
        if cancelled:
            await self._settle_plan(plan)

    @staticmethod
    def _ended(status: str, fail_reason: str) -> str:
        """How a task being restarted ended (its status before the restart), for the [RESTART] turn."""
        if status == "failed":
            return f"failed: {_clip(fail_reason, 1000) or 'a step failed and the Planner could not go on'}"
        return "was cancelled" if status == "cancelled" else "was stopped"

    @staticmethod
    def _restart_entry(step: PlanStep) -> JournalEntry:
        """The journal entry of a step a restart puts back to not started: how its last attempt ended."""
        ended = _NOT_DONE[step.status]
        if step.result and step.status != "dispatched":  # a running step's result is its previous attempt's
            ended += f" (last result: {result_outcome(step.result)})"
        return JournalEntry(step.step_id, step.label, journal.clip(
            f"attempt {step.retries + 1}: {ended} when the task ended; the restart put it back to not started",
            journal.SUMMARY_MAX_CHARS), kind=JournalKind.STEP_RETRY)

    # --- Chat ---

    async def _handle_cancel(self, envelope: MessageEnvelope) -> None:
        logger.info("planner %s received cancel", self.agent_id)
        if self._plan:
            self._plan.status = "failed"
            await self._save(self._plan, plan_row=True)
        self._running = False

    async def _handle_user_message(self, envelope: MessageEnvelope) -> None:
        """Answer a chat message through the Planner's Claude, and do what it decides."""
        assert self._bus is not None
        plan = self._plan
        text = str(envelope.payload.get("text", "")).strip()
        if plan is None or plan.status == "planning":
            reply = "任务还在规划中，请稍后再发。"
        elif plan.status not in ("executing", "verifying"):
            reply = f"任务已结束（{plan.status}），不能再发消息。"
        else:
            reply = await self._chat(plan, text, envelope)

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=envelope.source,
            message_type=MessageType.AGENT_REPLY,
            payload={"text": reply},
            correlation_id=envelope.correlation_id,
            reply_to=envelope.message_id,
            metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
        ))

    async def _chat(self, plan: TaskPlan, text: str, envelope: MessageEnvelope) -> str:
        """A chat message is a supplement to the task: delivered, with any other change waiting, in a CHAT turn,
        whose reply answers the message."""
        supplement = envelope.payload.get("supplement_id")
        if not isinstance(supplement, int):
            # Stored when it was sent (``projects.store_chat_supplement``); one that was not is stored now.
            try:
                supplement = await db_changes.add_supplement(
                    self._task_id, journal.kept_text(text, journal.SUPPLEMENTS_MAX_CHARS), via="chat")
            except Exception:
                logger.exception("planner %s could not store a chat message as a supplement", self.agent_id)
                supplement = None
        reply = await self._deliver_changes(plan, envelope)
        if reply is not None:
            return reply
        # A turn after another event already gave it to Claude, with any other change waiting.
        answered = self._replies.pop(supplement, None) if supplement is not None else None
        return answered or "收到，已交给规划者处理。"

    async def _pending_changes(self) -> TaskChanges:
        try:
            return await db_changes.pending(self._task_id)
        except Exception:
            logger.exception("planner %s cannot read its task's changes", self.agent_id)
            return TaskChanges()

    async def _read_changes(self, plan: TaskPlan, changes: TaskChanges) -> None:
        """Take ``changes`` into ``plan``: the journal as stored (with the supplements, added from elsewhere too)
        and the latest requirement."""
        if changes.empty:
            return
        try:
            plan.journal = [JournalEntry.from_row(row) for row in await db_journal.list_entries(self._task_id)]
        except Exception:
            logger.exception("planner %s cannot read its task's journal", self.agent_id)
        if changes.requirement is not None:
            plan.requirement = changes.requirement.text

    async def _mark_delivered(self, changes: TaskChanges) -> None:
        if changes.empty:
            return
        try:
            await db_changes.mark_delivered(self._task_id, changes)
        except Exception:
            # Given again at the next turn: more than once, never not at all.
            logger.exception("planner %s could not record its task's changes as delivered", self.agent_id)

    async def _deliver_changes(self, plan: TaskPlan, envelope: MessageEnvelope | None = None) -> str | None:
        """Give Claude the task's changes it has not had (supplements, a requirement edit) in one CHAT turn and do
        what it decides; the reply, or None when there was nothing to give. The changes are marked delivered only
        once that is done: a failed turn leaves them for the next one."""
        if plan.status not in _LIVE:
            return None
        changes = await self._pending_changes()
        if changes.empty:
            return None
        await self._read_changes(plan, changes)
        view = plan.view()
        message = self._change_message(plan, changes)
        try:
            reply = await self._ask(TurnKind.CHAT, message, lambda answer: parse_chat(answer, self._checked(view)))
        except PlannerTurnError as exc:
            await self._activity("error", f"Planner could not take a change to the task: {exc}"[:500])
            return f"Planner 出错，没能处理这次变更（会在下一轮重试）：{exc}"
        cancelled = await self._apply_gate_changes(plan, reply.gates, "a change to the task")
        if reply.final is not None:
            plan.final_summary = reply.final.markdown()
        text = await self._apply_chat(plan, reply, envelope)
        # The answer to a change made on the task page has no chat to go to: the activity keeps it.
        await self._activity("phase", f"Planner answered a change to the task ({reply.action.value}): "
                                      f"{reply.reply}"[:300])
        # Before the task may end below, which first gives the Planner what came meanwhile.
        await self._mark_delivered(changes)
        for supplement in changes.supplements:
            self._replies[supplement.id] = text
        if cancelled:
            await self._settle_plan(plan)
        return text

    async def _apply_chat(self, plan: TaskPlan, reply: ChatReply, envelope: MessageEnvelope | None, *,
                          at: str = "a change to the task") -> str:
        """Do what a CHAT reply decided, ``at`` what the journal and the activity say it answered; the answer for
        the user."""
        if reply.action is ChatAction.FORWARD:
            step = next(s for s in plan.steps if s.label == reply.step and s.status == "dispatched")
            correlation = envelope.correlation_id if envelope is not None else plan.correlation_id
            if await self._forward_to_worker(step, reply.text, correlation):
                return reply.reply
            # The Worker ended first; its result's decision turn gets the text instead.
            step.missed_messages.append(reply.text)
            await self._save(plan, step)
            await self._activity("phase", f"User message for step {step.index + 1} ({step.label}) arrived after "
                                          f"{step.worker_id} finished; kept for the Planner's decision")
            return f"{reply.reply}\n\n（{step.worker_id} 在消息送达前已经结束；处理它的结果时我会考虑这条消息。）"
        if reply.action is ChatAction.ADD_STEPS:
            steps = self._add_steps(plan, reply.steps)
            await self._save(plan, *steps, entries=[self._added_entry(plan, steps, reply, at)])
            await self._activity("phase", f"Steps added at {at}: {self._describe(plan, steps)}")
            await self._report_progress(plan)
            await self._dispatch_ready(plan)
            return reply.reply
        if reply.action is ChatAction.REPLAN:
            await self._chat_replan(plan, reply, at)
        return reply.reply

    def _added_entry(self, plan: TaskPlan, steps: list[PlanStep], reply: ChatReply, at: str) -> JournalEntry:
        """The journal's entry on the ``steps`` a CHAT-shaped ``reply`` added, ``at`` what it answered."""
        return JournalEntry("", "", journal.clip(f"added steps {self._describe(plan, steps)} at {at}: {reply.reply}",
                                                 journal.SUMMARY_MAX_CHARS), kind=JournalKind.DECISION)

    async def _chat_replan(self, plan: TaskPlan, reply: ChatReply, at: str) -> None:
        """Stop and drop the running steps the change makes wrong, replace every step not started, then go on."""
        stopped = [s for s in plan.steps if s.label in reply.stop and s.status == "dispatched"]
        for step in stopped:
            try:
                await self._kill_worker(step)  # a Worker that finished first has its result ignored
            except Exception:
                logger.debug("planner %s could not stop worker %s", self.agent_id, step.worker_id, exc_info=True)
            step.status = "skipped"
            await self._drop_workspace(step)
        keep = {spec.id for spec in reply.steps}
        dropped = [s for s in plan.steps if s.status == "pending" and s.label not in keep]
        for step in dropped:
            self._skip(step)
            await self._drop_workspace(step)
        steps = self._add_steps(plan, reply.steps)
        summary = f"Plan changed: {self._describe(plan, steps) or 'no new steps'}"
        if stopped:
            summary += f"; stopped {', '.join(s.label for s in stopped)}"
        if dropped:
            summary += f"; dropped {', '.join(s.label for s in dropped)}"
        entry = JournalEntry("", "", journal.clip(f"replan at {at}: {reply.reply}. {summary}",
                                                  journal.SUMMARY_MAX_CHARS), kind=JournalKind.DECISION)
        await self._save(plan, *stopped, *dropped, *steps, entries=[entry])
        await self._activity("phase", summary)
        await self._report_progress(plan)
        await self._dispatch_ready(plan)

    async def _forward_to_worker(self, step: PlanStep, text: str, correlation_id: str) -> bool:
        """Ask the worker pool to add a user message as the running worker's next turn."""
        message = (
            "A message from the user, passed on by the Planner while you work on this step. "
            "Take it into account, then continue:\n\n" + text
        )
        if not await self._send_worker_input(step, message, correlation_id):
            return False
        await self._activity(
            "phase", f"User message passed to {step.worker_id} (step {step.index + 1}): {text[:80]}",
        )
        return True

    async def _send_worker_input(self, step: PlanStep, text: str, correlation_id: str) -> bool:
        """Add ``text`` as the running worker's next user turn; True if it landed."""
        assert self._bus is not None and step.worker_id is not None
        try:
            ack = await self._bus.request(MessageEnvelope.create(
                source=self.agent_address,
                target=worker_address(step.worker_id),
                message_type=MessageType.WORKER_INPUT,
                payload={"text": text, "task_id": step.step_id},
                correlation_id=correlation_id,
            ), timeout=WORKER_INPUT_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        return bool(ack.payload.get("delivered"))

    async def _handle_system_event(self, envelope: MessageEnvelope) -> None:
        event_type = envelope.payload.get("type", "")
        if event_type == "pause":
            scope = envelope.payload.get("scope", "")
            logger.info("planner %s pausing work related to %s", self.agent_id, scope)
        elif event_type == "supervise":
            await self._supervise()
        elif event_type == "idle":
            await self._stop_idle_claude()
        # "gate_retry": the event's end gives the gates' messages, the retry among them.

    # --- Supervising the Workers running now ---

    async def _supervise_loop(self) -> None:
        """Wake on a timer and, when Workers are running, post a supervise tick
        onto the Planner's own mailbox so the check runs in the message loop,
        serialized with results and chat (never concurrently with them)."""
        assert self._bus is not None
        while self._running:
            interval = (await load_settings()).supervise_interval_seconds
            project_interval = (await self._read_execution()).supervise_interval_seconds
            if project_interval is not None:
                interval = project_interval
            if interval <= 0:
                return  # the periodic check is off
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            plan = self._plan
            if plan is None or plan.status != "executing":
                continue
            if not any(s.status == "dispatched" for s in plan.steps):
                continue
            # A result/chat/plan turn within the last interval already looked
            # at the plan; skip this tick rather than spend a turn on nothing new.
            if self._last_turn_at and time.monotonic() - self._last_turn_at < interval:
                continue
            try:
                await self._bus.publish(MessageEnvelope.create(
                    source=self.agent_address,
                    target=self.agent_address,
                    message_type=MessageType.SYSTEM_EVENT,
                    payload={"type": "supervise"},
                ))
            except Exception:
                logger.debug("planner %s could not post a supervise tick", self.agent_id, exc_info=True)

    async def _supervise(self) -> None:
        """Look at the Workers running now and steer or stop the ones that need it."""
        plan = self._plan
        if plan is None or plan.status != "executing":
            return
        running = [s for s in plan.steps if s.status == "dispatched"]
        if not running:
            return
        reports = [await self._running_report(plan, s) for s in running]
        view = plan.view()
        try:
            reply = await self._ask(
                TurnKind.SUPERVISE,
                self._supervise_message(plan, running, reports),
                lambda text: parse_supervise(text, view.running),
            )
        except PlannerTurnError as exc:
            await self._activity("error", f"Planner could not supervise the running steps: {exc}"[:500])
            return
        if reply.progress is not None:
            await self._record_progress(plan, reply.progress)
        for review in reply.reviews:
            step = next((s for s in plan.steps if s.label == review.step and s.status == "dispatched"), None)
            if step is None:
                continue  # the step finished between the turn and now
            if review.action is SuperviseAction.NUDGE:
                await self._nudge_worker(plan, step, review.text)
            elif review.action is SuperviseAction.KILL:
                await self._kill_and_decide(plan, step, review.reason)

    async def _record_progress(self, plan: TaskPlan, note: ProgressNote) -> None:
        """Keep a progress note the Planner's reply carried, as a journal entry of the whole task: the task page
        shows the newest first (``journal.progress_notes``)."""
        await self._save(plan, entries=[JournalEntry("", "", note.to_json(), kind=JournalKind.PROGRESS)])

    async def _nudge_worker(self, plan: TaskPlan, step: PlanStep, text: str) -> None:
        message = (
            "A course correction from the Planner while you work on this step. "
            "Take it into account, then continue:\n\n" + text
        )
        if await self._send_worker_input(step, message, plan.correlation_id):
            await self._activity(
                "phase", f"Planner steered step {step.index + 1} ({step.label}): {text[:80]}", worker=step.worker_id,
            )
        else:
            step.missed_messages.append(text)
            await self._save(plan, step)
            await self._activity(
                "phase", f"Planner steer for step {step.index + 1} ({step.label}) arrived after "
                         f"{step.worker_id} finished; kept for its decision",
            )

    async def _kill_and_decide(self, plan: TaskPlan, step: PlanStep, reason: str) -> None:
        """Stop a drifting step's Worker, then decide what happens to the step.

        A killed Worker publishes no result, so the stopped outcome is fed
        through the normal decision path (the Planner picks retry/replan/fail).
        """
        if not await self._kill_worker(step):
            return  # the Worker already finished; its real result will decide
        await self._activity("error", f"Planner stopped step {step.index + 1} ({step.label}): {reason}"[:300],
                             worker=step.worker_id)
        result = {"status": "stopped", "summary": f"The Planner stopped this Worker: {reason}"}
        step.result = result
        context = DecisionContext(
            step=step.label, result=ResultKind.WORK, succeeded=False,
            review_required=self._needs_review(step), retries=step.retries,
            max_retries=self._execution.step_retries,
        )
        await self._decide(plan, step, context, self._work_report(step, result))

    async def _kill_worker(self, step: PlanStep, run_id: str | None = None) -> bool:
        """Ask the worker pool to stop ``step``'s running Worker (or its run ``run_id``, a Reviewer); True if
        it was running."""
        assert self._bus is not None and step.worker_id is not None
        try:
            ack = await self._bus.request(MessageEnvelope.create(
                source=self.agent_address,
                target=worker_address(run_id or step.worker_id),
                message_type=MessageType.KILL_WORK,
                payload={"task_id": step.step_id},
                correlation_id=self._plan.correlation_id if self._plan else "",
            ), timeout=WORKER_INPUT_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        return bool(ack.payload.get("killed"))

    async def _kill_running_workers(self) -> None:
        """Stop every Worker and Reviewer this task still runs (on task stop/cancel, at its spend cap), all at once:
        each stop waits for its run's processes to end."""
        plan = self._plan
        if plan is None or self._bus is None:
            return
        runs: list[tuple[PlanStep, str | None]] = []
        for step in plan.steps:
            if step.status == "dispatched" and step.worker_id:
                runs.append((step, None))
            elif step.status == "completed" and step.reviewer_id:
                runs.append((step, step.reviewer_id))  # in review
        stopped = await asyncio.gather(*(self._kill_worker(step, run_id) for step, run_id in runs),
                                       return_exceptions=True)
        for (step, run_id), outcome in zip(runs, stopped):
            if isinstance(outcome, Exception):
                logger.debug("planner %s could not stop run %s", self.agent_id, run_id or step.worker_id,
                             exc_info=outcome)

    # --- Turns on the Planner's Claude ---

    async def _ask(
        self, kind: TurnKind, message: str, parse: Callable[[str], T],
        run: Callable[[str], Awaitable[str]] | None = None,
    ) -> T:
        """Run one turn (with ``run``, on the running Claude as it is; else ``_turn``) and parse its reply; an
        invalid reply gets one repair turn.

        Raises ``PlannerTurnError`` when Claude fails or times out, or when the
        repaired reply is still invalid.
        """
        run = run or self._turn
        reply = await run(message)
        await self._read_roles()  # a role the turn defined counts for its reply
        try:
            return parse(reply)
        except ProtocolError as first:
            await self._activity("error", f"Planner reply broke the {kind.value} contract ({first}); asking again"[:500])
            reply = await run(
                f"[{kind.value}] Your last reply broke the reply contract: {first}\n"
                f"Answer the same request again. End with exactly one ```json block in the {kind.value} shape."
            )
            await self._read_roles()
            try:
                return parse(reply)
            except ProtocolError as second:
                raise PlannerTurnError(f"invalid reply after a repair turn: {second}") from None

    async def _template_lines(self, plan: TaskPlan) -> list[str]:
        """The project's role templates, one line each, for a turn that plans, where its project lets its Planner
        define roles and has templates; nothing otherwise (or when they cannot be read)."""
        if not self._execution.roles_on or not plan.project_id:
            return []
        try:
            rows = await db_templates.list_templates(plan.project_id)
        except Exception:
            logger.warning("planner %s cannot read its project's role templates", self.agent_id, exc_info=True)
            return []
        if not rows:
            return []
        return ["This project's role templates (a template is not a role yet: when one fits a step, define a role "
                "from it with looper_task_role_set and template before a step names it):",
                *(f"- {row['template_key']} ({row['kind']})" + (f": {_clip(row['description'], 300)}"
                                                               if row["description"] else "") for row in rows)]

    async def _read_roles(self) -> None:
        """Read the task's roles by key (``self._roles``) where its project lets its Planner define them; a failed
        read keeps the last ones read."""
        if not self._execution.roles_on:
            self._roles = None
            return
        try:
            rows = await db_roles.current(self._task_id)
        except Exception:
            logger.warning("planner %s cannot read its task's roles; keeping the last ones read", self.agent_id,
                           exc_info=True)
            self._roles = self._roles or {}
            return
        self._roles = {row["role_key"]: RoleKind(row["kind"]) for row in rows}

    def _checked(self, view: PlanView) -> PlanView:
        """``view`` with the task's roles as last read and whether it may open gates, for checking a reply."""
        return replace(view, roles=self._roles, gate_policy=self._gate_policy)

    @property
    def _gate_policy(self) -> GatePolicy:
        return gate_policy(gates_on=self._execution.gates_on, scheduled=self._scheduled)

    async def _role(self, key: str, version: int | None = None) -> TaskRole | None:
        """Version ``version`` of the task's role ``key`` (its current one without a version); None when it cannot
        be read."""
        try:
            row = await db_roles.get(self._task_id, key, version)
        except Exception:
            logger.warning("planner %s cannot read role %s", self.agent_id, key, exc_info=True)
            return None
        return TaskRole.from_row(row) if row else None

    async def _turn(self, message: str) -> str:
        async with self._turn_lock:
            try:
                return await self._run_turn(message)
            finally:
                self._last_turn_at = time.monotonic()

    def _claude_command(self, prompt_file: Path, settings: PlannerSettings, launch: Launch, work_dir: str) -> list[str]:
        """The Planner's resident process (``RuntimeAdapter.resident_command``), without resuming a session."""
        return launch.runtime.resident_command(launch.executable(self._claude_bin), cwd=work_dir,
                                               prompt_file=prompt_file, launch=launch, max_turns=settings.max_turns)

    async def _run_turn(self, message: str, *, retried: bool = False) -> str:
        """Send ``message`` as one turn and return Claude's final message; raises ``PlannerTurnError``.

        The first turn of a new session for a plan under way starts with the resume brief.
        """
        plan = self._plan
        assert plan is not None
        settings = await load_settings()
        knowledge = await self._load_knowledge(plan)
        library = await self._load_installed(plan)
        if self._ops is not None and self._token is not None:
            library = await self._ops.library_with_server(library, self._runtime_root, self._token)
            self._token_files.add(library.mcp_config)
        agent_settings = await self._load_agent_settings(plan)
        if agent_settings.profile_error:
            # The task fails with the reason rather than run another agent.
            raise PlannerTurnError(agent_settings.profile_error)
        system_prompt = self._system_prompt(plan, settings, knowledge, agent_settings.harness)
        launch = Launch(agent_settings, library, knowledge, role=AgentType.PLANNER)  # never with agent teams
        await self._note_background(plan.project_background)
        try:
            prompt_file = system_prompt.write_file(self._runtime_root)
        except OSError as exc:
            raise PlannerTurnError(f"cannot write the system prompt file: {exc}") from None
        base = self._claude_command(prompt_file, settings, launch, plan.work_dir)
        # The command carries the model, the effort, the installed items' directories (each named by its
        # content's hash) and the knowledge's; the file is named by its content's hash (the knowledge
        # index included), kept beside it all the same; the environment and the installed items are
        # compared by their hashes, the mounted directories as such.
        signature = (tuple(base), system_prompt.sha256, launch.env_sha256, launch.library.sha256,
                     launch.knowledge.add_dirs, launch.binding_sha256)
        switch = session_switch.profile_switch(plan.session_id, plan.session_runtime, plan.session_profile, launch,
                                               session_provider=plan.session_provider)
        if switch is not None:
            # This agent cannot resume the session: a new one starts, after the old one hands over.
            await self._switch_session(plan, switch, settings.timeout_seconds)
        if launch.runtime is not self._claude_runtime:
            # Each runtime type keeps its resident sessions its own way (after the old one's handoff).
            await self._claude.stop()
            self._claude, self._claude_runtime = launch.runtime.resident_session(self.agent_id), launch.runtime
        if self._claude.stale(signature):
            # Claude exited, or the config, the settings, the project or the task
            # changed: the next process resumes the session with the new prompt.
            await self._claude.stop()
        brief = ""
        resuming: str | None = None  # the session this turn's new process resumes, if any
        if self._claude.started and self._process is not None:
            process = self._process
        else:
            plan.session_runtime, plan.session_profile = launch.runtime.type, launch.profile.id
            plan.session_provider = launch.profile.provider.id
            resuming = plan.session_id
            if not resuming and plan.steps:
                # A new session for a plan under way: its first turn starts with the resume brief.
                if self._pending_switch is None:
                    await self._drop_session(plan, session_switch.session_lost(
                        "the Planner has no saved session for this task", runtime=launch.runtime.type,
                        profile=launch.profile.id))
                brief = self._resume_brief(plan, await self._gate_threads(plan))
            command = base + (launch.runtime.resume_args(plan.session_id) if plan.session_id else [])
            process = _Process(command, signature, system_prompt, launch)
            try:
                await self._claude.start(command, cwd=plan.work_dir, signature=signature, env=launch.environment(),
                                         totals=plan.session_totals if plan.session_id else None,
                                         autonomous=self._autonomous_turns(plan, command, system_prompt, launch),
                                         price=launch.price)
            except OSError as exc:
                error = f"cannot start Claude Code: {exc}"
                recorder = self._recorder(plan, f"{brief}\n\n{message}" if brief else message, process)
                await recorder.start()
                await recorder.finish(output_text=None, result_event=None, error=error)
                raise PlannerTurnError(error) from None
            self._process = process
            resumed = f", resuming session {plan.session_id}" if plan.session_id else ""
            await self._activity("phase", f"Planner Claude Code started (pid {self._claude.pid}{resumed})")

        try:
            return await self._exchange(f"{brief}\n\n{message}" if brief else message, process,
                                        settings.timeout_seconds, resuming=None if retried else resuming)
        except _SessionNotResumed as exc:
            # Claude never started the saved session: it no longer exists, and resuming it again would fail
            # every turn. The Planner continues in a new session, which the resume brief starts.
            await self._activity("error", f"Warning: Planner {exc}")
            await self._claude.stop()
            await self._drop_session(plan, session_switch.session_lost(
                f"its saved session {resuming} could not be resumed", runtime=launch.runtime.type,
                profile=launch.profile.id))
            return await self._run_turn(message, retried=True)

    def _recorder(self, plan: TaskPlan, text: str, process: _Process) -> ClaudeRunRecorder:
        return ClaudeRunRecorder(
            agent_id=self.agent_id,
            task_id=self._task_id,
            command=process.command,
            cwd=plan.work_dir,
            input_text=text,
            session_id=plan.session_id,
            pid=self._claude.pid,
            system_prompt=process.system_prompt,
            launch=process.launch,
        )

    async def _exchange(self, text: str, process: _Process, limit: int, *, resuming: str | None = None) -> str:
        """Run ``text`` as one turn on the running Claude (started with ``process``) and return its final
        message, recorded as one run; raises ``PlannerTurnError``, or ``_SessionNotResumed`` when Claude never
        started ``resuming``, the session it was told to resume."""
        plan = self._plan
        assert plan is not None
        recorder = self._recorder(plan, text, process)
        async with recorder:  # however the turn ends, its task's stop cutting it off too, its record ends
            async def on_event(event: dict[str, Any]) -> None:
                await recorder.record(event)
                session_id = event.get("session_id")
                if isinstance(session_id, str) and session_id and session_id != plan.session_id:
                    plan.session_id = session_id
                    await self._save_session(plan)
                    # The new session has its resume brief.
                    self._pending_switch = self._pending_handoff = None

            if self._ops is not None and self._token is not None:
                self._ops.begin_turn(self._token)  # its tools may change things in this turn only
            try:
                turn = await asyncio.wait_for(self._claude.run_turn(text, on_event, stop=lambda: recorder.cap_stop),
                                              limit if limit > 0 else None)
            except TimeoutError:
                # The turn was cancelled, which killed Claude; the next turn resumes the session.
                error = f"the turn ran longer than the {limit} s limit (timeout_seconds)"
                await recorder.finish(output_text=None, result_event=None, error=error)
                raise PlannerTurnError(error) from None
            except Exception as exc:
                error = f"reading Claude failed: {type(exc).__name__}: {exc}"
                await recorder.finish(output_text=None, result_event=None, error=error)
                raise PlannerTurnError(error) from None
            finally:
                if self._ops is not None and self._token is not None:
                    self._ops.end_turn(self._token)

            if resuming and not turn.session_started:
                error = f"session {resuming} could not be resumed{exit_note(turn.exit_code)}; retried in a new session"
                await recorder.finish(output_text=None, result_event=None, error=error)
                raise _SessionNotResumed(error)

            result_event = turn.result_event
            final_text = str((result_event or {}).get("result") or "")
            error: str | None = None
            over_cap = process.launch.runtime.budget_error(result_event)
            if result_event is None:
                error = f"Claude Code exited before finishing the turn (exit code {turn.exit_code})"
            elif over_cap:
                error = over_cap
            elif result_event.get("is_error") or result_event.get("subtype") != "success":
                error = f"Claude Code turn did not succeed ({result_event.get('subtype') or 'error'})"
            elif not final_text.strip():
                error = "Claude Code returned an empty reply"
            # The turn is over: recorded first, so a stop that comes while Claude stops or the session is saved
            # keeps what it spent.
            await recorder.finish(
                output_text=final_text or None,
                result_event=result_event,
                error=error,
                usage=turn.usage,
            )
            if over_cap:
                # The process answers every later turn the same way at once: the next turn starts a new one,
                # with a new cap, that resumes the session.
                await self._claude.stop()
            if result_event is not None:
                plan.session_totals = self._claude.totals
                await self._save_session(plan)
            if error:
                raise PlannerTurnError(error)
            return final_text

    # --- A new session: the handoff before it and the resume brief that starts it ---

    async def _switch_session(self, plan: TaskPlan, switch: SessionSwitch, limit: int) -> None:
        """Move to a new session because the Planner's agent changed. While the old session's Claude still runs,
        it first writes a handoff for the new one (one HANDOFF turn); otherwise the new session starts from the
        task journal alone."""
        process = self._process
        handoff: str | None = None
        if self._claude.started and process is not None and not self._claude.stale(process.signature):
            try:
                handoff = await self._ask(TurnKind.HANDOFF, self._handoff_message(plan, switch), parse_handoff,
                                          run=lambda text: self._exchange(text, process, limit))
            except PlannerTurnError as exc:
                await self._activity("error",
                                     f"Warning: the Planner's previous session wrote no handoff: {exc}"[:500])
        await self._claude.stop()
        await self._drop_session(plan, switch, handoff)

    async def _drop_session(self, plan: TaskPlan, switch: SessionSwitch, handoff: str | None = None) -> None:
        """Forget the saved session, so the next process starts a new one, and journal why (with the handoff
        before it, in one transaction); the new session's first turn gets the resume brief."""
        entries = [JournalEntry("", "", handoff, kind=JournalKind.HANDOFF)] if handoff else []
        entries.append(JournalEntry("", "", journal.clip(switch.describe(), journal.SUMMARY_MAX_CHARS),
                                    kind=JournalKind.SESSION_SWITCH))
        await self._save(plan, entries=entries)
        plan.session_id, plan.session_totals = None, Usage()  # its totals go with it
        await self._save_session(plan)
        self._pending_switch, self._pending_handoff = switch, handoff
        handed = "; its previous session wrote a handoff" if handoff else ""
        await self._activity("phase", f"The Planner starts a new session ({switch.describe()}){handed}"[:500])

    def _handoff_message(self, plan: TaskPlan, switch: SessionSwitch) -> str:
        return "\n".join([
            f"[HANDOFF] This session ends here ({switch.describe()}). The next session for this task starts fresh "
            "and cannot read this conversation; it gets the plan, the user's instructions and the task journal. "
            "Hand over what it needs beyond those: your understanding of the codebase (its layout, how it builds "
            "and tests, its conventions), reasoning you have not finished, open risks, and what you would check "
            "next. Do not repeat the plan or the journal. Do not change any files.",
            "",
            f"Write at most {journal.HANDOFF_MAX_CHARS} characters. End your reply with exactly one ```json block, "
            "and nothing after it:",
            "```json",
            '{"handoff": "..."}',
            "```",
        ])

    async def _gate_threads(self, plan: TaskPlan) -> list[str]:
        """The resume brief's lines on each waiting gate's discussion so far: what the earlier session had of it
        (the user's messages no turn had come in their [GATE] turn)."""
        lines: list[str] = []
        for step in plan.waiting_gates():
            gate = step.gate
            assert gate is not None
            try:
                thread = await db_user_gates.thread(step.step_id)
            except Exception:
                logger.warning("planner %s cannot read the discussion of gate %s", self.agent_id, step.label,
                               exc_info=True)
                continue
            had = [m for m in thread if m.get("role") != MessageRole.USER.value or m["id"] <= gate.delivered_through]
            if any(m.get("role") != MessageRole.LOOPER.value for m in had):
                lines += ["", f"The discussion so far in gate {step.label} \"{_clip(gate.topic, 200)}\" (your earlier "
                              "session had it; oldest first):", *_thread_lines(had, THREAD_MAX_CHARS)]
        return lines

    def _resume_brief(self, plan: TaskPlan, gate_threads: Sequence[str] = ()) -> str:
        """What starts the first turn of a new session for a plan under way: why the session is new, where the
        task is, the handoff the old session wrote and the discussions of the gates that wait
        (``_gate_threads``); the request after it carries the plan, the user's instructions and the task
        journal."""
        switch, handoff = self._pending_switch, self._pending_handoff
        live = [s for s in plan.steps if s.status != "skipped"]
        done = sum(1 for s in live if s.status == "verified")
        under_way = sum(1 for s in live if s.status in ("dispatched", "deciding", "completed"))
        failed = sum(1 for s in live if s.status == "failed")
        why = switch.describe() if switch is not None else "its earlier session is gone"
        lines = [
            f"[RESUME] You continue this task in a new session ({why}); your earlier conversation in it is not "
            f"here. Where the task is: {done} of {len(live)} steps accepted, {under_way} under way, {failed} "
            "failed. The request below carries the plan, the user's instructions and the task journal as they "
            "are now.",
            "",
        ]
        if handoff:
            lines += ["Handoff from your previous session (what it wrote for you about its working context; "
                      "use it as data):", handoff]
        else:
            lines.append("Your previous session wrote no handoff: the task journal is what remains of it.")
        lines += [*gate_threads, "", "The request for this turn follows; answer it in its shape."]
        return "\n".join(lines)

    def _autonomous_turns(
        self, plan: TaskPlan, command: list[str], system_prompt: SystemPrompt, launch: Launch,
    ) -> AutonomousHandler:
        """How the Planner takes a turn its Claude (started with these) runs on its own.

        The turn is a run of its own. What Claude said in it answers none of
        the Planner's requests, so it decides nothing about the plan and is
        only logged.
        """

        async def begin(_after: Any) -> AutonomousRun:
            recorder = ClaudeRunRecorder(
                agent_id=self.agent_id,
                task_id=self._task_id,
                command=command,
                cwd=plan.work_dir,
                input_text=AUTONOMOUS_INPUT,
                session_id=plan.session_id,
                pid=self._claude.pid,
                system_prompt=system_prompt,
                launch=launch,
                autonomous=True,
            )
            await recorder.start()

            async def on_end(text: str, turn: ResidentTurn) -> None:
                if turn.result_event is not None:
                    plan.session_totals = self._claude.totals  # it spent too: a restart resumes from here
                    await self._save_session(plan)
                logger.info("%s: Claude ran a turn on its own after background work ended; its reply decides "
                            "nothing: %s", self.agent_id, _clip(text, 300) or "(nothing)")

            return AutonomousRun(recorder, on_end)

        return begin

    def _system_prompt(self, plan: TaskPlan, settings: PlannerSettings, knowledge: AgentKnowledge,
                       harness: Harness) -> SystemPrompt:
        """The harness, the role prompt, the project background and knowledge index, the task, then the reply
        contract."""
        project = None
        if plan.project_id or plan.project_name or plan.project_background:
            project = {"id": plan.project_id, "name": plan.project_name, "background": plan.project_background,
                       "updated_at": plan.project_updated_at}
        return build_system_prompt(
            harness=harness,
            role_id="planner",
            role=settings.system_prompt,
            recommended=settings.recommended,
            project=project,
            task_kind="plan",
            task_id=self._task_id,
            task=plan_task_section(
                task_id=self._task_id, requirement=plan.requirement, project_name=plan.project_name,
                project_description=_clip(plan.project_description, 300), work_dir=plan.work_dir,
            ),
            contract_name="REPLY_CONTRACT",
            # With its platform tools, how to use them follows the reply contract.
            contract=reply_contract(self._execution.step_retries, roles=self._execution.roles_on,
                                    gates=self._gate_policy is GatePolicy.ALLOWED
                                    or any(s.gate is not None and not s.gate.state.closed for s in plan.steps))
            + (f"\n{self._ops.guide_for(roles=self._execution.roles_on)}" if self._ops else ""),
            knowledge=knowledge,
        )

    # --- Turn messages: only what changed (the task is the system prompt's task section) ---

    def _plan_lines(self, plan: TaskPlan) -> list[str]:
        labels = plan.labels()
        lines = ["Plan:"]
        for step in plan.steps:
            deps = ", ".join(labels.get(d, d) for d in step.depends_on) or "none"
            if step.gate is not None:
                lines += self._gate_lines(step, deps)
                continue
            roles = (f"; role: {step.role}" if step.role else "") + \
                (f"; review roles: {', '.join(item.label for item in step.review_roles)}" if step.review_roles else "")
            lines.append(
                f"- {step.label} [{step.status}] {_clip(step.instruction, 200)} "
                f"(depends on: {deps}; review: {'yes' if step.review else 'no'}; retries: {step.retries}{roles})"
            )
            summary = _clip((step.result or {}).get("summary"), 200) if step.status == "verified" else ""
            if summary:
                lines.append(f"  summary: {summary}")
        return lines

    @staticmethod
    def _gate_lines(step: PlanStep, deps: str) -> list[str]:
        """A gate's plan lines: its state, topic and agenda, and how it closed."""
        gate = step.gate
        assert gate is not None
        state = gate.state.value
        if gate.state.waiting and gate.opened_at is not None:
            state += f", waited {_ago(gate.opened_at)}"
        done, total = resolved(gate.agenda)
        lines = [f"- {step.label} [gate: {state}] \"{_clip(gate.topic, 200)}\" (depends on: {deps}; agenda: {done} of "
                 f"{total} resolved)"]
        if gate.state is GateState.PASSED and gate.summary is not None:
            decided = "; ".join(gate.summary.decisions) or "none"
            lines.append(f"  settled: {_clip(decided, 300)}")
        elif gate.state is GateState.CANCELLED:
            lines.append(f"  cancelled: {_clip(gate.resolution, 300)}")
        return lines

    @staticmethod
    def _progress_lines(plan: TaskPlan) -> list[str]:
        """The task journal's "Progress and findings" section, after a blank line; nothing before an entry."""
        section = journal.planner_section(plan.journal)
        return ["", *section] if section else []

    def _plan_message(self, plan: TaskPlan, templates: Sequence[str] = ()) -> str:
        added = journal.supplements(
            plan.journal, title="Supplements (what the user added to the task, oldest first; each stands unless a "
                                "later one changes it)")
        return "\n".join([
            "[PLAN] Plan the task in the Task section of your instructions"
            + (", with the supplements below." if added else "."),
            *(["", *added] if added else []),
            *(["", *templates] if templates else []),
            "",
            "You may look at the project first, without changing it. Reply with the PLAN JSON: at least 1 step.",
        ])

    def _work_report(self, step: PlanStep, result: dict[str, Any]) -> list[str]:
        role = f" (as role {step.role} v{step.role_version})" if step.role and step.role_version else ""
        lines = [
            f"Result of step {step.label} from {step.worker_id}{role}:",
            f"- status: {result.get('status') or 'unknown'}",
        ]
        if result.get("summary"):
            lines.append(f"- summary: {_clip(result['summary'], 1000)}")
        changed = result.get("changed_files")
        if isinstance(changed, dict) and changed:
            files = [f"{path} ({_clip(note, 80)})" if note else str(path) for path, note in list(changed.items())[:20]]
            more = f" (+{len(changed) - 20} more)" if len(changed) > 20 else ""
            lines.append(f"- changed files: {'; '.join(files)}{more}")
        deleted = result.get("deleted_files")
        if isinstance(deleted, list) and deleted:
            lines.append(f"- deleted files: {', '.join(str(p) for p in deleted[:20])}")
        if result.get("error") and result.get("error") != result.get("summary"):
            lines.append(f"- error: {_clip(result['error'], 500)}")
        if result.get("final_message"):
            lines.append(f"- the Worker's final message: {_clip(result['final_message'], 300)}")
        return lines + _learning_lines(result.get("learnings"))

    def _review_report(self, step: PlanStep, result: Mapping[str, Any]) -> list[str]:
        """The review the decision is about: the built-in Reviewer's ``result``, or, once it passed the step, each
        of its review roles' verdicts in the order they reviewed it (``result`` the last)."""
        role_reviews = _role_reviews(step.result or {}) if step.review_stage is ReviewStage.ROLE else []
        if not role_reviews:
            return self._verdict_lines(f"Review of step {step.label} by {step.reviewer_id}:", result)
        # The review roles review only a step the built-in Reviewer passed.
        summary = _clip((step.result or {}).get("verify_summary"), 300)
        lines = [f"The built-in Reviewer passed step {step.label}" + (f": {summary}" if summary else ".")]
        left = len(step.review_roles) - len(role_reviews)
        for review in role_reviews:
            required = review.get("required") is not False
            what = "it must pass" if required else ("advisory: its verdict does not block the step; weigh its "
                                                    "findings and accept, retry with feedback or replan")
            by = f"review role {review.get('role')}" + (f" v{review['version']}" if review.get("version") else "")
            lines += self._verdict_lines(f"Review of step {step.label} by {by} ({what}):", review)
        if left:
            lines.append(f"Its {left} other review role(s) did not review it: a required one failed first.")
        return lines

    @staticmethod
    def _verdict_lines(title: str, result: Mapping[str, Any]) -> list[str]:
        lines = [title, f"- passed: {'yes' if result.get('passed') is True else 'no'}"]
        if result.get("summary"):
            lines.append(f"- summary: {_clip(result['summary'], 1000)}")
        findings = result.get("findings")
        if isinstance(findings, list):
            lines.extend(f"- finding: {_clip(f, 300)}" for f in findings[:10])
        return lines + _learning_lines(result.get("learnings"))

    async def _running_report(self, plan: TaskPlan, step: PlanStep) -> list[str]:
        """What step ``step``'s Worker has done lately, for a SUPERVISE turn."""
        lines = [f"Step {step.label} ({step.worker_id}): {_clip(step.instruction, 200)}"]
        try:
            rows = await db_activity.list_activity(self._task_id, since_seq=0, limit=400)
        except Exception:
            lines.append("  - (recent activity is unavailable)")
            return lines
        recent = [r for r in rows if r.get("worker") == step.worker_id][-6:]
        if not recent:
            lines.append("  - (no activity yet)")
        for row in recent:
            lines.append(f"  - [{row.get('kind')}] {_clip(row.get('summary'), 160)}")
        return lines

    def _supervise_message(self, plan: TaskPlan, running: list[PlanStep], reports: list[list[str]]) -> str:
        lines = [
            "[SUPERVISE] A periodic check on the Workers running now — no result has arrived.",
            "",
            *self._plan_lines(plan),
            *self._progress_lines(plan),
            "",
            "Workers running now and what they have done lately:",
        ]
        for report in reports:
            lines += report
        lines += [
            "",
            "Review only the steps that need it (a drifting or stalled Worker): nudge one that "
            "is on a workable path, kill one whose direction is wrong. Leave the rest alone.",
            "Reply with the SUPERVISE JSON.",
        ]
        return "\n".join(lines)

    def _decision_message(
        self, plan: TaskPlan, step: PlanStep, context: DecisionContext, report: list[str],
    ) -> str:
        who = "Reviewer" if context.result is ResultKind.REVIEW else "Worker"
        allowed = []
        for decision in context.allowed():
            if decision is Decision.RETRY:
                allowed.append(f"retry ({context.max_retries - context.retries} left)")
            else:
                allowed.append(decision.value)
        lines = [
            f"[DECISION] Step {step.label} (step {step.index + 1}) has its {who}'s result.",
            "",
            *self._plan_lines(plan),
            *self._progress_lines(plan),
            "",
            *report,
        ]
        if step.missed_messages:
            lines += ["", "The user sent this to the step's Worker through you, but it arrived after the Worker finished:"]
            lines += [f"- {_clip(m, 500)}" for m in step.missed_messages]
        if context.result is ResultKind.WORK and context.succeeded and context.review_required:
            lines += ["", "This step needs a passing review before it can be accepted."]
        if context.last_open:
            lines += ["", "This is the task's last open step: a decision that completes the task (accept, or replan "
                          'with no new steps) must carry "final", the task\'s final summary.']
        elif context.open_gates:
            lines += ["", f"The task's other open steps are gates ({', '.join(sorted(context.open_gates))}): a "
                          "decision that cancels all of them and completes the task (accept, or replan with no new "
                          'steps) must carry "final", the task\'s final summary.']
        lines += ["", f"Allowed decisions now: {', '.join(allowed)}.", "Reply with the DECISION JSON."]
        return "\n".join(lines)

    def _gate_message(self, plan: TaskPlan, step: PlanStep, messages: list[dict[str, Any]],
                      context: GateContext) -> str:
        """A GATE turn's message: the gate, its agenda, the user's new messages and the plan as it is (a new
        session's resume brief has the discussion before them)."""
        gate = step.gate
        assert gate is not None
        running = [s for s in plan.steps if s.status == "dispatched"]
        running_text = ", ".join(f"{s.label} ({s.worker_id})" for s in running) or "none"
        actions = ["none", "add_steps", "replan"] + (["forward (to a running step)"] if running else [])
        opened = f", opened {_ago(gate.opened_at)} ago" if gate.opened_at is not None else ""
        lines = [f"[GATE] Gate {step.label} \"{gate.topic}\": {gate.state.value}{opened}, turn {gate.turns + 1}.",
                 "", "Agenda:", *(item.line() for item in gate.agenda)]
        if gate.findings:
            lines += [f"Your findings so far: {gate.findings}"]
        lines += ["", "New messages from the user (oldest first):", *(_message_line(m) for m in messages)]
        if context.forced:
            lines += ["", 'The user ended the discussion and asked you to go on with the current conclusions: reply '
                          'with "gate": "close", and state an assumption for every agenda item still open.']
        lines += ["", *self._plan_lines(plan), f"Running now: {running_text}", *self._progress_lines(plan)]
        if context.last_open:
            lines += ["", 'This gate is the task\'s last open step: closing it without adding steps completes the '
                          'task, and then the reply must carry "final", the task\'s final summary.']
        lines += ["", f"Allowed actions: {', '.join(actions)}.", "Reply with the GATE JSON."]
        return "\n".join(lines)

    def _change_message(self, plan: TaskPlan, changes: TaskChanges) -> str:
        """A CHAT turn's message: the task's changes the Planner has not had, and the plan as it is."""
        running = [s for s in plan.steps if s.status == "dispatched"]
        running_text = ", ".join(f"{s.label} ({s.worker_id})" for s in running) or "none"
        actions = ["none", "add_steps", "replan"] + (["forward (to a running step)"] if running else [])
        lines, section = self._change_lines(plan, changes)
        return "\n".join([
            "[CHAT] The task changed.",
            *lines,
            "",
            *self._plan_lines(plan),
            f"Running now: {running_text}",
            *(["", *section] if section else []),
            "",
            "Decide whether the plan still fits the task as it is now.",
            f"Allowed actions: {', '.join(actions)}.",
            "Reply with the CHAT JSON.",
        ])

    def _restart_message(self, plan: TaskPlan, why: str, changes: TaskChanges, templates: Sequence[str] = ()) -> str:
        """A RESTART turn's message: why the task ended, the task's changes the Planner has not had, the plan as
        it is now and the journal."""
        lines, section = self._change_lines(plan, changes)
        return "\n".join([
            f"[RESTART] {_sentence(f'The task {why}')} The user restarted it, and it goes on from its plan: the "
            "accepted steps stay done and do not run again; the steps that did not finish are back to not started "
            "(the journal below says how their last attempt ended).",
            f"The restart does not reset retries: a step back to not started keeps the retries it used (at most "
            f"{self._execution.step_retries} per step; a replan that keeps its id keeps them too), so one that had "
            "used them all gets this one attempt and no retry; a step under a new id (add_steps or replan) starts "
            "with none used.",
            *lines,
            "",
            *self._plan_lines(plan),
            *(["", *section] if section else []),
            *(["", *templates] if templates else []),
            "",
            "Build on what is done: decide whether the steps not started still fit the task as it is now.",
            "Allowed actions: none (run the steps not started as they are), add_steps, replan.",
            "Reply with the CHAT JSON.",
        ])

    @staticmethod
    def _change_lines(plan: TaskPlan, changes: TaskChanges) -> tuple[list[str], list[str]]:
        """The task's changes the Planner has not had, after a blank line each, and the journal's section without
        them (the supplements given before, and the rest of the journal)."""
        new = {supplement.id for supplement in changes.supplements}
        lines: list[str] = []
        if changes.supplements:
            lines += ["", "The user added to the task (new supplements, oldest first; every message the user sends "
                          "you is one):"]
            lines += ["- " + supplement.text.strip().replace("\n", "\n  ") for supplement in changes.supplements]
        if changes.requirement is not None:
            edit = changes.requirement
            lines += ["", f"The requirement was edited (version {edit.before_version} -> {edit.version}); the Task "
                          "section of your instructions has the new text. What changed:", _requirement_diff(edit)]
        earlier = [entry for entry in plan.journal if entry.id is None or entry.id not in new]
        return lines, journal.planner_section(earlier)

    # --- Endings ---

    async def _late_messages_taken(self, plan: TaskPlan) -> bool:
        """Before a task with gates completes: what the user wrote in a gate as it closed (a ``gate_late``
        supplement) reaches the Planner in a CHAT turn first; whether the task goes on, as the reply added steps."""
        if not any(s.gate is not None for s in plan.steps):
            return False
        if not any(s.via == db_user_gates.LATE_VIA for s in (await self._pending_changes()).supplements):
            return False
        await self._deliver_changes(plan)
        return plan.status in _LIVE and not plan.is_complete

    async def _complete_plan(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        if await self._late_messages_taken(plan):
            return  # its steps run, and their decisions complete the task
        plan.status = "completed"
        await self._save(plan, plan_row=True)
        await update_task_phase(self._task_id, "completed")
        await self._activity("phase", "All steps verified — task completed")
        await self._remove_workspaces()
        if self._execution.lessons_on:
            # Before the final status: the TaskRouter ends the Planner when it gets it.
            await self._distill_lessons(plan)

        summary = plan.final_summary
        if not summary:
            parts = [f"任务完成: {plan.requirement[:80]}"]
            for step in plan.steps:
                if step.status == "skipped":
                    continue
                changed = step.result.get("changed_files", {}) if step.result else {}
                if changed:
                    parts.append(f"  - {step.instruction[:50]}: 修改了 {len(changed)} 个文件")
                else:
                    parts.append(f"  - {step.instruction[:50]}: 完成")
            summary = "\n".join(parts)

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                "status": "completed",
                "progress": plan.progress_summary(),
                "summary": summary,
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _distill_lessons(self, plan: TaskPlan) -> None:
        """One more Planner turn turns the task journal into a lessons document, saved in the project's
        knowledge or, in ``draft`` mode, as a draft the user confirms or discards (``distill_lessons``). Any
        problem is a warning in the task's activity: the task completed."""
        if not plan.journal:
            await self._activity("phase", "No lessons written: the task journal is empty")
            return
        cap = self._execution.task_budget_usd
        if cap is not None:
            try:
                spent = float((await db_llm_records.task_costs([self._task_id]))[self._task_id]["cost_usd"])
            except Exception:
                logger.warning("planner %s cannot read its task's cost", self.agent_id, exc_info=True)
                spent = 0.0
            if spent >= cap:
                await self._activity("error", f"Warning: no lessons written: the task has spent its "
                                              f"${format_usd(cap)} cap (task_budget_usd)")
                return
        try:
            lessons = await self._ask(TurnKind.LESSONS, self._lessons_message(plan), parse_lessons)
        except PlannerTurnError as exc:
            await self._activity("error", f"Warning: the Planner could not write the task's lessons: {exc}"[:500])
            return
        title = f"Lessons: {_clip(plan.requirement, project_knowledge.TITLE_MAX_CHARS - 12)}"
        summary = f"What task {self._task_id} found and learned, written by its Planner when it completed"
        draft = self._execution.distill_lessons is LessonsMode.DRAFT
        try:
            if draft:
                await project_knowledge.add_draft(plan.project_id, task_id=self._task_id, title=title,
                                                  summary=summary, content=lessons)
            else:
                await project_knowledge.add_document(plan.project_id, {
                    "source_kind": "text", "title": title, "summary": summary, "content": lessons,
                }, self._runtime_root)
        except Exception as exc:
            logger.warning("planner %s cannot save the task's lessons", self.agent_id, exc_info=True)
            await self._activity("error", f"Warning: the task's lessons were not saved to project knowledge: "
                                          f"{exc}"[:500])
            return
        where = "as a draft to review in project knowledge" if draft else "to project knowledge"
        await self._activity("phase", f"Lessons saved {where}: {title}"[:300])

    def _lessons_message(self, plan: TaskPlan) -> str:
        return "\n".join([
            "[LESSONS] The task is complete. Turn its journal below into a short lessons document for the "
            "project's knowledge, which later tasks of the project read: the findings, pitfalls, commands and "
            "conventions that will still hold. Leave out what mattered to this task only. Do not change any files.",
            "",
            *journal.lessons_journal(plan.journal),
            "",
            f"Write the document in Markdown, at most {journal.LESSONS_MAX_CHARS} characters. End your reply with "
            "exactly one ```json block, and nothing after it:",
            "```json",
            '{"lessons": "# Lessons\\n\\n- ..."}',
            "```",
        ])

    async def _fail_plan(self, plan: TaskPlan, *, reason: str = "", fail_kind: FailKind | None = None) -> None:
        assert self._bus is not None
        plan.status = "failed"
        plan.fail_reason = reason or plan.fail_reason
        await self._save(plan, plan_row=True)
        await update_task_phase(self._task_id, "failed")

        # The closing note: why the task failed, what its steps delivered and what is not done.
        summary = closing_note(plan.fail_reason or "A step failed and the Planner could not go on.",
                               ((s.label, s.status, s.result) for s in plan.steps))
        await self._activity("error", f"Task failed: {plan.fail_reason or 'see step details'}"[:500])
        kept = [s.workspace.branch for s in plan.steps if s.isolated and s.workspace is not None]
        if kept:
            await self._activity("phase", f"Kept {len(kept)} step worktree(s) with work not merged, for a restart, "
                                          f"until the task is started over or deleted: {', '.join(kept)}"[:500])

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                "status": "failed",
                "progress": plan.progress_summary(),
                "summary": summary,
                "fail_kind": fail_kind.value if fail_kind else None,
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _report_progress(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        if plan.status in ("completed", "failed"):
            return  # its final status is out (a task stopped at its spend cap mid-decision)
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                # While a gate waits for the user the task waits for them, whatever else runs meanwhile.
                "status": WAITING_USER if plan.status in _LIVE and plan.waiting_gates() else plan.status,
                "progress": plan.progress_summary(),
                "summary": f"进度更新: {plan.progress_summary()}",
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    # --- Persistence ---

    async def _activity(self, kind: str, summary: str, *, worker: str | None = None) -> None:
        try:
            await db_activity.insert_activity(
                task_id=self._task_id, worker=worker or self.agent_id, kind=kind, summary=summary,
            )
        except Exception:
            logger.exception("failed to record planner activity for task %s", self._task_id)

    async def _refresh_project(self, plan: TaskPlan) -> None:
        """Read the task's project as it is now (the task may have moved).

        A failed read keeps what the plan last saw: the background is
        guidance, so the task goes on without the newest one.
        """
        try:
            project = await db_projects.get_task_project(self._task_id)
        except Exception:
            logger.warning("planner %s cannot read its task's project; keeping the last one read",
                           self.agent_id, exc_info=True)
            return
        if project is None:
            logger.warning("planner %s: task %s has no project to read; keeping the last one read",
                           self.agent_id, self._task_id)
            return
        plan.project_id = str(project.get("id") or plan.project_id)
        plan.project_name = str(project.get("name") or "")
        plan.project_description = str(project.get("description") or "")
        plan.project_background = str(project.get("background") or "")
        plan.project_work_dir = str(project.get("work_dir") or "")
        self._execution = ProjectExecution.from_row(project)
        self._scheduled = project.get("task_schedule_id") is not None
        updated_at = project.get("updated_at")
        plan.project_updated_at = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at or "")

    async def _read_execution(self) -> ProjectExecution:
        """The project's execution settings, read now; the last ones read when the project cannot be read."""
        try:
            project = await db_projects.get_task_project(self._task_id)
        except Exception:
            logger.warning("planner %s cannot read its task's project; keeping its last settings", self.agent_id,
                           exc_info=True)
            return self._execution
        if project is not None:
            self._execution = ProjectExecution.from_row(project)
        return self._execution

    # --- The task's spend cap ---

    async def _within_budget(self, plan: TaskPlan) -> bool:
        """Whether the task may start another run: False once it has spent its project's task cap.

        At the cap the task stops for good (``_stop_at_budget``). Without a
        cap nothing is read. A cost that cannot be read lets the task go on.
        """
        cap = self._execution.task_budget_usd
        if cap is None:
            return True
        if plan.status not in _LIVE:
            return False  # stopping or ended already
        try:
            spent = float((await db_llm_records.task_costs([self._task_id]))[self._task_id]["cost_usd"])
        except Exception as exc:
            logger.warning("planner %s cannot read its task's cost", self.agent_id, exc_info=True)
            await self._activity("error", f"Warning: the task's cost is unavailable ({exc}); its spend cap "
                                          "is not checked this time"[:500])
            return True
        if spent < cap:
            return True
        await self._stop_at_budget(plan, spent, cap)
        return False

    async def _stop_at_budget(self, plan: TaskPlan, spent: float, cap: float) -> None:
        """Stop the runs going on, let the Planner sum up when the project asks for it, then fail the task."""
        reason = f"Task spend cap reached: ${format_usd(spent)} spent of the ${format_usd(cap)} cap (task_budget_usd)"
        plan.status = "stopping"  # no result is taken and nothing starts while it winds down
        await self._save(plan, plan_row=True)
        await self._activity("error", reason)
        await self._kill_running_workers()
        if self._execution.on_task_budget is TaskBudgetAction.WRAP_UP:
            try:
                summary = await self._ask(TurnKind.DECISION, self._wrap_up_message(plan, reason), parse_wrap_up)
            except PlannerTurnError as exc:
                summary = f"(the Planner could not sum up: {exc})"
            reason = f"{reason}. Planner's wrap-up: {summary}"
        plan.fail_reason = reason
        await self._save(plan, entries=[JournalEntry(
            "", "", journal.clip(f"stopped at the spend cap: {reason}", journal.SUMMARY_MAX_CHARS),
            kind=JournalKind.DECISION)])
        await self._fail_plan(plan, fail_kind=FailKind.TASK_BUDGET)

    async def _continue_plan(self, envelope: MessageEnvelope) -> None:
        """Continue a task its spend cap stopped, from its saved plan: no new plan, and the Planner's Claude
        resumes its session.

        The steps the cap cut short run again: a Worker's step from the start (its Worker was stopped, or
        its result came after the cap), a step in review with a new Reviewer. The plan, its steps with
        what they reported, and the session are read back from the database.
        """
        self._plan = None
        await self._load_plan(("failed",))
        plan = self._plan
        if plan is None:
            payload = envelope.payload
            plan = self._plan = TaskPlan(
                plan_id=f"plan-{uuid.uuid4().hex[:12]}", goal_id=payload.get("goal_id", ""),
                requirement=payload.get("requirement", ""), work_dir=payload.get("work_dir", ""),
                correlation_id=envelope.correlation_id, project_id=payload.get("project_id", "") or "",
            )
            await self._fail_plan(plan, reason="Cannot continue: the task has no saved plan; start it to plan anew")
            return
        await self._refresh_project(plan)
        again: list[PlanStep] = []
        review: list[PlanStep] = []
        merging: list[PlanStep] = []
        for step in plan.steps:
            if step.status == "deciding" and step.workspace is not None and \
                    step.workspace.state is StepWorkspaceState.MERGING:
                merging.append(step)  # accepted: only its merge was cut off
            elif step.status in ("dispatched", "deciding"):
                step.status, step.started_at = "pending", None  # started again when dispatched again
                again.append(step)
            elif step.status == "completed":
                review.append(step)
        plan.status = "executing"
        plan.fail_reason = ""
        await self._save(plan, *again, plan_row=True)
        await update_task_phase(self._task_id, "executing")
        cap = self._execution.task_budget_usd
        cut_short = [f"{len(again)} step(s) run again"] if again else []
        cut_short += [f"{len(review)} step(s) go back to review"] if review else []
        await self._activity("phase", f"Task continued from its plan with "
                                      f"{f'a ${format_usd(cap)}' if cap is not None else 'no'} spend cap"
                                      + (f": {', '.join(cut_short)}" if cut_short else ""))
        for step in review:
            await self._send_to_review(plan, step)
        for step in merging:
            await self._finish_merge(plan, step)
            if plan.status not in _LIVE:
                return
        if plan.is_complete:
            await self._complete_plan(plan)
            return
        await self._report_progress(plan)
        await self._dispatch_ready(plan)

    def _wrap_up_message(self, plan: TaskPlan, reason: str) -> str:
        return "\n".join([
            f"[DECISION] {reason}. No more steps run: the running ones were stopped, and the task now fails.",
            "",
            *self._plan_lines(plan),
            *self._progress_lines(plan),
            "",
            'Allowed decisions now: fail. In "reason", tell the user what the task delivered and what remains.',
            "Reply with the DECISION JSON.",
        ])

    async def _load_knowledge(self, plan: TaskPlan) -> AgentKnowledge:
        """The project's knowledge for this turn, read now; the last one read when it cannot be read.

        Notes in the task's activity when the knowledge the turn runs with
        starts, changes or ends.
        """
        project = {"id": plan.project_id, "work_dir": plan.project_work_dir} if plan.project_id else None
        knowledge = await load_knowledge(self._runtime_root, project, plan.work_dir, notes=self._execution.notes_on)
        previous = self._knowledge
        if knowledge.error is not None:
            await self._activity("error", f"Warning: {knowledge.error}"[:500])
            if previous is not None:
                return previous
        self._knowledge = knowledge
        before = previous.sha256 if previous is not None else None
        if knowledge.sha256 != before:
            if knowledge.sha256 is None:
                await self._activity("phase", "Project knowledge removed")
            else:
                verb = "changed" if before else "applied"
                await self._activity("phase", f"Project knowledge {verb} ({documents_label(knowledge.documents)})")
        notes_before = previous.notes.sha256 if previous is not None and previous.notes is not None else None
        notes_now = knowledge.notes.sha256 if knowledge.notes is not None else None
        if knowledge.notes is not None and knowledge.notes.error:
            await self._activity("error", notes_note(knowledge.notes))
        elif notes_now != notes_before:
            await self._activity("phase", notes_note(knowledge.notes) if knowledge.notes is not None and notes_now
                                 else "The operator's notes for this directory removed")
        return knowledge

    async def _load_agent_settings(self, plan: TaskPlan) -> AgentSettings:
        """The Planner's agent settings in the task's project, with the agent the task chose for its Planner,
        read now; the last ones read when they cannot be read (the built-in defaults before any)."""
        agent_settings = await load_agent_settings(AgentType.PLANNER, plan.project_id or None, self._task_id)
        if agent_settings.error is not None:
            await self._activity("error", f"Warning: {agent_settings.error}"[:500])
            if self._agent_settings is not None:
                return self._agent_settings
            return agent_settings
        self._agent_settings = agent_settings
        return agent_settings

    async def _load_installed(self, plan: TaskPlan) -> AgentLibrary:
        """The catalog items installed for the Planner, globally or in the project, read now; the last ones
        read when they cannot be read.

        Notes in the task's activity when the items the turn runs with start,
        change or end.
        """
        library = await load_installed(self._runtime_root, AgentType.PLANNER.value, plan.project_id or None)
        previous = self._library
        if library.error is not None:
            await self._activity("error", f"Warning: {library.error}"[:500])
            if previous is not None:
                return previous
        self._library = library
        before = previous.sha256 if previous is not None else None
        if library.sha256 != before:
            if library.sha256 is None:
                await self._activity("phase", "Installed plugins and skills removed")
            else:
                verb = "changed" if before else "applied"
                await self._activity("phase", f"Installed plugins and skills {verb} ({items_label(library.items)})")
        return library

    async def _note_background(self, background: str) -> None:
        """Note in the task's activity when the system prompt starts carrying, changes or drops the background."""
        previous, self._prompt_background = self._prompt_background, background
        if background == (previous or ""):
            return
        if not background:
            await self._activity("phase", "Project background removed")
        elif previous:
            await self._activity("phase", f"Project background changed ({len(background)} chars)")
        else:
            await self._activity("phase", f"Project background applied ({len(background)} chars)")

    async def _save(
        self, plan: TaskPlan, *steps: PlanStep, plan_row: bool = False, entries: Sequence[JournalEntry] = (),
        gate_messages: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Save ``steps`` (a gate step with its gate), with ``plan_row`` the plan's own state, the journal
        ``entries`` (added to the plan's journal first) and the messages a gate's discussion gets, in one
        transaction.

        A failed write is logged and the task goes on from memory; only a
        restart would miss it.
        """
        plan.journal.extend(entries)
        unique = list({id(step): step for step in steps}.values())
        gates = [step.gate.row(step.step_id) for step in unique if step.gate is not None]
        try:
            await db_plans.save(self._task_id, plan=plan.row() if plan_row else None,
                                steps=[_step_row(step) for step in unique],
                                journal=[entry.row() for entry in entries],
                                **({"gates": gates} if gates else {}),
                                **({"gate_messages": list(gate_messages)} if gate_messages else {}))
        except Exception:
            logger.exception("failed to save the plan of task %s", self._task_id)

    async def _save_session(self, plan: TaskPlan) -> None:
        """Save the Planner's Claude session and its running totals."""
        try:
            await db_agent_sessions.save_session(self.agent_id, plan.session_id, plan.session_totals.to_dict(),
                                                 runtime_type=plan.session_runtime, provider_id=plan.session_provider,
                                                 profile_id=plan.session_profile)
        except Exception:
            logger.exception("failed to save the session of %s", self.agent_id)

    @staticmethod
    def _stored_gate(step: Mapping[str, Any], gates: Mapping[str, Mapping[str, Any]]) -> Gate | None:
        """A stored gate step's gate (one whose row is missing is a gate that has not opened); None for a work
        step."""
        if step.get("kind") != "gate":
            return None
        row = gates.get(step["id"])
        return Gate.from_row(row) if row else Gate(str(step.get("instruction") or ""), ())

    async def _load_plan(self, statuses: tuple[str, ...] = ("planning", *db_plans.LIVE_STATUSES)) -> None:
        """Read back the task's plan in one of ``statuses`` (a live one by default) and the Planner's session."""
        try:
            stored = await db_plans.load(self._task_id)
            if stored is None or stored["plan"]["status"] not in statuses:
                return
            session = await db_agent_sessions.get_session(self.agent_id) or {}
        except Exception:
            logger.exception("planner %s cannot read its saved plan", self.agent_id)
            return
        row = stored["plan"]
        plan = TaskPlan(
            plan_id=row["plan_id"],
            goal_id=row.get("goal_id") or "",
            requirement=row.get("requirement") or "",
            work_dir=row.get("work_dir") or "",
            status=row["status"],
            correlation_id=row.get("correlation_id") or "",
            origin_channel_id=row.get("origin_channel_id") or "",
            project_id=row.get("project_id") or "",
            session_id=session.get("session_id"),
            session_totals=Usage.from_dict(session.get("session_totals")),
            session_runtime=session.get("runtime_type"),
            session_profile=session.get("profile_id"),
            session_provider=session.get("provider_id"),
            final_summary=row.get("final_summary") or "",
            fail_reason=row.get("fail_reason") or "",
            start_branch=row.get("start_branch") or "",
            start_commit=row.get("start_commit") or "",
        )
        for sd in stored["steps"]:
            plan.steps.append(PlanStep(
                step_id=sd["id"],
                index=int(sd.get("step_index") or 0),
                instruction=sd.get("instruction") or "",
                key=sd.get("step_key") or "",
                target_files=list(sd.get("target_files") or []),
                validation_commands=list(sd.get("validation_commands") or []),
                acceptance_criteria=list(sd.get("acceptance_criteria") or []),
                depends_on=list(sd.get("depends_on") or []),
                review=bool(sd.get("review")),
                status=sd.get("status") or "pending",
                result=sd.get("result"),
                worker_id=sd.get("worker_id"),
                reviewer_id=sd.get("reviewer_id"),
                retries=int(sd.get("retries") or 0),
                feedback=list(sd.get("feedback") or []),
                missed_messages=list(sd.get("missed_messages") or []),
                role=sd.get("role_key"),
                role_version=sd.get("role_version"),
                review_roles=[StepReviewRole.from_row(item) for item in sd.get("review_roles") or []],
                review_stage=ReviewStage(sd["review_stage"]) if sd.get("review_stage") else None,
                review_position=sd.get("review_position"),
                gate=self._stored_gate(sd, stored.get("gates") or {}),
                started_at=sd.get("started_at"),
            ))
        plan.journal = [JournalEntry.from_row(row) for row in stored.get("journal") or []]
        try:
            rows = await db_step_workspaces.list_for_task(self._task_id)
        except Exception:
            logger.exception("planner %s cannot read its step workspaces", self.agent_id)
            rows = []
        latest = {row["step_id"]: row for row in rows}  # oldest first: a step's newest wins
        for step in plan.steps:
            if step.step_id in latest:
                step.workspace = StepWorkspace.from_row(latest[step.step_id])
        self._plan = plan
