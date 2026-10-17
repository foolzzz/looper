"""The reply contract between a task's Planner and its Claude.

Every Planner turn names the reply it needs ([PLAN], [DECISION] or [CHAT]),
and Claude ends its reply with exactly one fenced JSON object in that shape.
This module owns the shapes as typed values, validates a reply strictly
against the plan it applies to, and holds the contract text that code
appends to the editable system prompt.

A DECISION or SUPERVISE reply may carry a progress note (``ProgressNote``),
which code keeps in the task journal for the task page; the decision that
completes a task must carry its final summary (``FinalSummary``), which
becomes the task's result.

In a project that lets its Planners define roles (``planner_roles`` ``on``), a
step may name the work role its Worker runs as (``role``) and the review role
that reviews it after the built-in Reviewer (``review_role``); ``PlanView.roles``
holds the task's roles, and the contract says so (``reply_contract(roles=True)``).
Elsewhere both keys are refused, and the contract is as before.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from ..project_execution import DEFAULT_STEP_RETRIES
from ..task_roles import RoleKind
from .journal import HANDOFF_MAX_CHARS, LESSONS_MAX_CHARS, kept_text

# A progress note's fields and a final summary's, in the order they are shown, and what each keeps of a field.
PROGRESS_FIELDS = ("done", "now", "next", "risks", "estimate")
PROGRESS_FIELD_MAX_CHARS = 500
FINAL_FIELDS = ("delivered", "how_to_use", "verification", "open_issues")
FINAL_FIELD_MAX_CHARS = 4_000
# How a final summary shows each field: one heading each, in the task's result.
FINAL_HEADINGS = {"delivered": "Delivered", "how_to_use": "How to use", "verification": "Verification",
                  "open_issues": "Open issues"}

_STEP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
# A ```json fence opening a line. JSON strings cannot hold a raw newline, so a
# fence inside a string value (an instruction quoting code) never matches.
_OPENING = re.compile(r"^```json[ \t]*\n", re.MULTILINE)
_CLOSING = re.compile(r"\s*```")
_STEP_KEYS = frozenset({"id", "instruction", "acceptance_criteria", "depends_on", "review"})
# A step's optional keys: the task roles it runs as and is reviewed by.
_ROLE_KEYS = frozenset({"role", "review_role"})


class TurnKind(str, Enum):
    PLAN = "PLAN"
    DECISION = "DECISION"
    CHAT = "CHAT"
    SUPERVISE = "SUPERVISE"
    # A task that ended is restarted from its plan: how it goes on, answered in the CHAT shape.
    RESTART = "RESTART"
    # The extra turn of a completed task whose project distills lessons; its message gives its own shape.
    LESSONS = "LESSONS"
    # The last turn of a session the next agent cannot resume: what it hands over; its message gives its shape.
    HANDOFF = "HANDOFF"


class SuperviseAction(str, Enum):
    LEAVE = "leave"
    NUDGE = "nudge"
    KILL = "kill"


class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    RETRY = "retry"
    REPLAN = "replan"
    FAIL = "fail"


class ChatAction(str, Enum):
    NONE = "none"
    FORWARD = "forward"
    ADD_STEPS = "add_steps"
    REPLAN = "replan"


class ResultKind(str, Enum):
    """Whose result a decision is about."""

    WORK = "work"
    REVIEW = "review"


class ProtocolError(ValueError):
    """A reply that breaks the contract; the message says how, for the repair turn."""


@dataclass(frozen=True)
class StepSpec:
    """One step as the Planner's Claude writes it; ``id`` is its label within the task."""

    id: str
    instruction: str
    acceptance_criteria: tuple[str, ...]
    depends_on: tuple[str, ...]
    review: bool
    role: str | None = None  # the work role its Worker runs as; None: the built-in Worker
    review_role: str | None = None  # the review role that reviews it after the built-in Reviewer


@dataclass(frozen=True)
class PlanReply:
    steps: tuple[StepSpec, ...]


@dataclass(frozen=True)
class ProgressNote:
    """Where the task stands, as a DECISION or SUPERVISE reply says it: each field a short text."""

    done: str  # what the task has delivered so far
    now: str  # what runs now
    next: str  # what comes after
    risks: str  # what may delay or break it
    estimate: str  # how much longer, roughly

    def to_json(self) -> str:
        """The note as its journal entry keeps it."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ProgressNote | None:
        """The note a journal entry keeps; None for an entry that does not hold one."""
        try:
            value = json.loads(text)
        except ValueError:
            return None
        if not isinstance(value, dict) or not all(isinstance(value.get(name), str) for name in PROGRESS_FIELDS):
            return None
        return cls(**{name: value[name] for name in PROGRESS_FIELDS})


@dataclass(frozen=True)
class FinalSummary:
    """A completed task's summary for the user, as the decision that completes it says it."""

    delivered: str  # what the task delivered
    how_to_use: str  # how to use it
    verification: str  # the evidence that it works
    open_issues: str  # what is left open; "none" when nothing

    def markdown(self) -> str:
        """The summary as the task's result: one heading per field (``FINAL_HEADINGS``)."""
        return "\n\n".join(f"## {FINAL_HEADINGS[name]}\n{getattr(self, name)}" for name in FINAL_FIELDS)


@dataclass(frozen=True)
class DecisionReply:
    decision: Decision
    reason: str
    feedback: str  # retry only
    steps: tuple[StepSpec, ...]  # replan only
    summary: str
    progress: ProgressNote | None = None
    final: FinalSummary | None = None  # only on the decision that completes the task


@dataclass(frozen=True)
class ChatReply:
    reply: str
    action: ChatAction
    step: str  # forward only: the running step whose Worker gets ``text``
    text: str  # forward only
    steps: tuple[StepSpec, ...]  # add_steps, and replan (the steps that replace every step not started)
    stop: tuple[str, ...] = ()  # replan only: running steps to stop and drop


@dataclass(frozen=True)
class SuperviseReview:
    """What to do with one running step, as the Planner's Claude decides."""

    step: str
    action: SuperviseAction
    text: str  # nudge only: passed to the running Worker as its next turn
    reason: str  # kill only: why the Worker is being stopped


@dataclass(frozen=True)
class SuperviseReply:
    reviews: tuple[SuperviseReview, ...]
    progress: ProgressNote | None = None


@dataclass(frozen=True)
class PlanView:
    """What validation needs to know about the current plan, by step label.

    ``started`` maps each live step that has run (or runs) to its
    dependencies and ``pending`` each step not started yet. ``retired``
    holds the labels of dropped steps, which cannot be reused or depended
    on. ``running`` holds the steps a Worker runs now.
    """

    started: Mapping[str, tuple[str, ...]]
    pending: Mapping[str, tuple[str, ...]]
    retired: frozenset[str] = frozenset()
    running: frozenset[str] = frozenset()
    done: frozenset[str] = frozenset()  # the started steps that are verified
    # The task's roles by key, as defined when the reply is checked; None: its project lets its Planner define none.
    roles: Mapping[str, RoleKind] | None = None

    def dropping(self, step: str) -> PlanView:
        """This plan once ``step``, a started step, is dropped."""
        started = {k: v for k, v in self.started.items() if k != step}
        return PlanView(started, self.pending, self.retired | {step}, self.running - {step}, self.done, self.roles)


@dataclass(frozen=True)
class DecisionContext:
    """The step result a decision is about."""

    step: str
    result: ResultKind
    succeeded: bool  # the Worker reported completed / the Reviewer passed
    review_required: bool  # the step needs a passing review before it is accepted
    retries: int
    max_retries: int = DEFAULT_STEP_RETRIES  # its project's max_step_retries
    # No other step is open: accepting this one, or a replan with no new steps, completes the task.
    last_open: bool = False

    @property
    def acceptable(self) -> bool:
        if self.result is ResultKind.REVIEW:
            return self.succeeded
        return self.succeeded and not self.review_required

    def allowed(self) -> tuple[Decision, ...]:
        allowed = []
        if self.acceptable:
            allowed.append(Decision.ACCEPT)
        if self.result is ResultKind.WORK and self.succeeded:
            allowed.append(Decision.REVIEW)
        if self.retries < self.max_retries:
            allowed.append(Decision.RETRY)
        allowed += [Decision.REPLAN, Decision.FAIL]
        return tuple(allowed)


def extract_reply(text: str) -> dict[str, Any]:
    """The JSON object in the one ```json block that ends ``text``.

    The block's end is found by decoding the JSON, so string values may quote
    code fences of their own.
    """
    openings = list(_OPENING.finditer(text))
    if not openings:
        raise ProtocolError("the reply has no ```json fenced block")
    if len(openings) > 1:
        raise ProtocolError(f"the reply has {len(openings)} ```json blocks; it must have exactly one")
    start = len(text) - len(text[openings[0].end():].lstrip())  # the JSON's first character
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"the ```json block is not valid JSON: {exc}") from None
    closing = _CLOSING.match(text, end)
    if closing is None:
        raise ProtocolError("the ```json block must hold one JSON value and close with ```")
    if text[closing.end():].strip():
        raise ProtocolError("the ```json block must end the reply; there is text after it")
    if not isinstance(value, dict):
        raise ProtocolError("the ```json block must hold one JSON object")
    return value


def parse_plan(text: str, roles: Mapping[str, RoleKind] | None = None) -> PlanReply:
    data = extract_reply(text)
    _only_keys(data, {"steps"}, "the PLAN reply")
    steps = _step_list(data.get("steps"), "steps")
    if not steps:
        raise ProtocolError("steps must list at least one step")
    _check_new_steps(steps, PlanView(started={}, pending={}, roles=roles), replace_pending=True)
    return PlanReply(steps)


def parse_decision(text: str, context: DecisionContext, plan: PlanView) -> DecisionReply:
    data = extract_reply(text)
    _only_keys(data, {"decision", "reason", "feedback", "steps", "summary", "progress", "final"}, "the DECISION reply")
    raw = data.get("decision")
    try:
        decision = Decision(raw)
    except ValueError:
        choices = ", ".join(d.value for d in Decision)
        raise ProtocolError(f"decision must be one of {choices}; got {raw!r}") from None
    reason = _optional_text(data, "reason")
    summary = _optional_text(data, "summary")
    if "feedback" in data and decision is not Decision.RETRY:
        raise ProtocolError("feedback is only for a retry")
    if "steps" in data and decision is not Decision.REPLAN:
        raise ProtocolError("steps is only for a replan")

    if decision not in context.allowed():
        raise ProtocolError(_refusal(decision, context))
    feedback = ""
    steps: tuple[StepSpec, ...] = ()
    if decision is Decision.RETRY:
        feedback = _required_text(data, "feedback")
    elif decision is Decision.FAIL:
        reason = _required_text(data, "reason")
    elif decision is Decision.REPLAN:
        steps = _step_list(data.get("steps"), "steps")
        if not steps and not context.acceptable:
            raise ProtocolError(
                f"step {context.step} cannot be accepted, so a replan must list the steps that replace it"
            )
        _check_new_steps(steps, plan if context.acceptable else plan.dropping(context.step), replace_pending=True)
    progress = _progress(data)
    final = FinalSummary(**_fields(data["final"], FINAL_FIELDS, "final", FINAL_FIELD_MAX_CHARS)) \
        if "final" in data else None
    completes = context.last_open and (decision is Decision.ACCEPT or (decision is Decision.REPLAN and not steps))
    if completes and final is None:
        raise ProtocolError('this decision completes the task, so it must carry "final": '
                            + ", ".join(FINAL_FIELDS))
    # Only the decision that completes the task keeps a final summary.
    return DecisionReply(decision, reason, feedback, steps, summary, progress, final if completes else None)


def parse_wrap_up(text: str) -> str:
    """The reason of a DECISION reply that may only fail the task (it reached its spend cap)."""
    data = extract_reply(text)
    _only_keys(data, {"decision", "reason", "progress"}, "the DECISION reply")
    if data.get("decision") != Decision.FAIL.value:
        raise ProtocolError(f"the only decision allowed now is fail; got {data.get('decision')!r}")
    return _required_text(data, "reason")


def parse_lessons(text: str) -> str:
    """The Markdown document of a LESSONS reply, clipped to ``LESSONS_MAX_CHARS``."""
    data = extract_reply(text)
    _only_keys(data, {"lessons"}, "the LESSONS reply")
    lessons = _required_text(data, "lessons")
    return lessons if len(lessons) <= LESSONS_MAX_CHARS else lessons[: LESSONS_MAX_CHARS - 1].rstrip() + "…"


def parse_handoff(text: str) -> str:
    """The working context a HANDOFF reply hands the next session, its lines kept, clipped to
    ``HANDOFF_MAX_CHARS``."""
    data = extract_reply(text)
    _only_keys(data, {"handoff"}, "the HANDOFF reply")
    return kept_text(_required_text(data, "handoff"), HANDOFF_MAX_CHARS)


def parse_chat(text: str, plan: PlanView) -> ChatReply:
    data = extract_reply(text)
    _only_keys(data, {"reply", "action", "step", "text", "steps", "stop"}, "the CHAT reply")
    reply = _required_text(data, "reply")
    raw = data.get("action")
    try:
        action = ChatAction(raw)
    except ValueError:
        choices = ", ".join(a.value for a in ChatAction)
        raise ProtocolError(f"action must be one of {choices}; got {raw!r}") from None
    if action is not ChatAction.FORWARD and ("step" in data or "text" in data):
        raise ProtocolError("step and text are only for action forward")
    if action not in (ChatAction.ADD_STEPS, ChatAction.REPLAN) and "steps" in data:
        raise ProtocolError("steps is only for actions add_steps and replan")
    if action is not ChatAction.REPLAN and "stop" in data:
        raise ProtocolError("stop is only for action replan")

    step = text_out = ""
    steps: tuple[StepSpec, ...] = ()
    stop: tuple[str, ...] = ()
    if action is ChatAction.FORWARD:
        step = _required_text(data, "step")
        if step not in plan.running:
            running = ", ".join(sorted(plan.running)) or "none"
            raise ProtocolError(f"step {step!r} has no running Worker (running: {running})")
        text_out = _required_text(data, "text")
    elif action is ChatAction.ADD_STEPS:
        steps = _step_list(data.get("steps"), "steps")
        if not steps:
            raise ProtocolError("add_steps must list at least one step")
        _check_new_steps(steps, plan, replace_pending=False)
    elif action is ChatAction.REPLAN:
        steps = _step_list(data.get("steps", []), "steps")
        stop = _stop_list(data.get("stop", []), plan)
        after = plan
        for label in stop:
            after = after.dropping(label)
        _check_new_steps(steps, after, replace_pending=True)
        if not steps and not set(after.started) - after.done:
            raise ProtocolError("after this replan no step would be left to run; list the steps that replace the "
                                "ones not started (or stop fewer running steps)")
    return ChatReply(reply, action, step, text_out, steps, stop)


def _stop_list(value: Any, plan: PlanView) -> tuple[str, ...]:
    """A replan's ``stop``: running steps, each once."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError("stop must be a list of running step ids")
    if len(set(value)) != len(value):
        raise ProtocolError("stop lists a step twice")
    for label in value:
        if label not in plan.running:
            running = ", ".join(sorted(plan.running)) or "none"
            raise ProtocolError(f"stop: step {label!r} has no running Worker (running: {running})")
    return tuple(value)


def parse_supervise(text: str, running: frozenset[str]) -> SuperviseReply:
    """A SUPERVISE reply: a review for each running step the Planner names.

    ``running`` is the set of step labels a Worker runs now; a review may name
    any subset of them, each at most once. An empty list is allowed (leave
    everything running).
    """
    data = extract_reply(text)
    _only_keys(data, {"reviews", "progress"}, "the SUPERVISE reply")
    raw = data.get("reviews")
    if not isinstance(raw, list):
        raise ProtocolError("reviews must be a list")
    reviews: list[SuperviseReview] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"reviews[{i}]"
        if not isinstance(item, dict):
            raise ProtocolError(f"{where} must be an object")
        _only_keys(item, {"step", "action", "text", "reason"}, where)
        step = item.get("step")
        if not isinstance(step, str) or not step:
            raise ProtocolError(f"{where}.step must be a step label")
        if step not in running:
            allowed = ", ".join(sorted(running)) or "none"
            raise ProtocolError(f"{where}.step {step!r} has no running Worker (running: {allowed})")
        if step in seen:
            raise ProtocolError(f"{where}.step {step!r} is reviewed twice")
        seen.add(step)
        try:
            action = SuperviseAction(item.get("action"))
        except ValueError:
            choices = ", ".join(a.value for a in SuperviseAction)
            raise ProtocolError(f"{where}.action must be one of {choices}; got {item.get('action')!r}") from None
        if action is not SuperviseAction.NUDGE and "text" in item:
            raise ProtocolError(f"{where}.text is only for action nudge")
        if action is not SuperviseAction.KILL and "reason" in item:
            raise ProtocolError(f"{where}.reason is only for action kill")
        text_out = reason = ""
        if action is SuperviseAction.NUDGE:
            text_out = _required_text(item, "text")
        elif action is SuperviseAction.KILL:
            reason = _required_text(item, "reason")
        reviews.append(SuperviseReview(step, action, text_out, reason))
    return SuperviseReply(tuple(reviews), _progress(data))


def _fields(value: Any, names: tuple[str, ...], where: str, limit: int) -> dict[str, str]:
    """``value``, an object with exactly ``names``, each a non-empty string, kept up to ``limit`` characters."""
    if not isinstance(value, dict):
        raise ProtocolError(f"{where} must be an object with {', '.join(names)}")
    _only_keys(value, names, where)
    missing = [name for name in names if not (isinstance(value.get(name), str) and value[name].strip())]
    if missing:
        raise ProtocolError(f"{where} needs {', '.join(missing)}: each a non-empty string (\"none\" when there is "
                            "nothing to say)")
    return {name: kept_text(value[name], limit) for name in names}


def _progress(data: Mapping[str, Any]) -> ProgressNote | None:
    """A reply's progress note; None without one."""
    if "progress" not in data:
        return None
    return ProgressNote(**_fields(data["progress"], PROGRESS_FIELDS, "progress", PROGRESS_FIELD_MAX_CHARS))


def _refusal(decision: Decision, context: DecisionContext) -> str:
    allowed = ", ".join(d.value for d in context.allowed())
    if decision is Decision.RETRY:
        why = f"step {context.step} has used its {context.max_retries} retries"
    elif decision is Decision.ACCEPT and context.result is ResultKind.WORK and context.succeeded:
        why = f"step {context.step} needs a passing review before it can be accepted"
    elif decision is Decision.ACCEPT:
        why = f"step {context.step} did not succeed, so it cannot be accepted"
    elif context.result is ResultKind.REVIEW:
        why = f"step {context.step} has just been reviewed"
    else:
        why = f"step {context.step} did not succeed, so there is nothing to review"
    return f"{decision.value} is not allowed: {why}; allowed now: {allowed}"


def _check_new_steps(steps: tuple[StepSpec, ...], plan: PlanView, *, replace_pending: bool) -> None:
    """Check steps that join ``plan``: replacing its pending steps, or added to them."""
    kept = dict(plan.started) if replace_pending else {**plan.started, **plan.pending}
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ProtocolError(f"step id {step.id!r} is used twice")
        if step.id in plan.started:
            raise ProtocolError(f"step {step.id} has started; started steps cannot change")
        if step.id in plan.retired:
            raise ProtocolError(f"step id {step.id!r} belongs to a dropped step; use a new id")
        if step.id in kept:
            raise ProtocolError(f"step id {step.id!r} is already used in this task")
        seen.add(step.id)
        _check_roles(step, plan.roles)
    graph = {**kept, **{s.id: s.depends_on for s in steps}}
    for step in steps:
        for dependency in step.depends_on:
            if dependency == step.id:
                raise ProtocolError(f"step {step.id} depends on itself")
            if dependency in plan.retired:
                raise ProtocolError(f"step {step.id} depends on {dependency}, which is dropped")
            if dependency not in graph:
                raise ProtocolError(f"step {step.id} depends on unknown step {dependency!r}")
    cycle = _find_cycle(graph)
    if cycle:
        raise ProtocolError("depends_on has a cycle: " + " -> ".join(cycle))


def _check_roles(step: StepSpec, roles: Mapping[str, RoleKind] | None) -> None:
    """A step's ``role`` names a work role of the task and its ``review_role`` a review role, on a reviewed step;
    neither is allowed where the project lets its Planner define no role."""
    if step.role is None and step.review_role is None:
        return
    if roles is None:
        raise ProtocolError(f"step {step.id}: this project does not let you define roles (planner_roles is off): "
                            "leave role and review_role out")
    defined = ", ".join(f"{key} ({kind.value})" for key, kind in roles.items()) or "none"
    for key, kind in ((step.role, RoleKind.WORK), (step.review_role, RoleKind.REVIEW)):
        field = "role" if kind is RoleKind.WORK else "review_role"
        if key is None:
            continue
        if key not in roles:
            raise ProtocolError(f"step {step.id}: {field} {key!r} is not a role of this task (defined: {defined}); "
                                "define it with looper_task_role_set first, or leave it out")
        if roles[key] is not kind:
            raise ProtocolError(f"step {step.id}: {field} must name a {kind.noun}; {key} is a {roles[key].noun}")
    if step.review_role is not None and not step.review:
        raise ProtocolError(f"step {step.id}: review_role needs \"review\": true; the built-in Reviewer reviews the "
                            "step first, and the review role after it")


def _find_cycle(graph: Mapping[str, Iterable[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for nxt in graph.get(node, ()):
            if state.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt]
            if nxt not in state and (found := visit(nxt)):
                return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return None


def _step_list(value: Any, field: str) -> tuple[StepSpec, ...]:
    if not isinstance(value, list):
        raise ProtocolError(f"{field} must be a list of steps")
    return tuple(_step(item, i) for i, item in enumerate(value))


def _step(value: Any, index: int) -> StepSpec:
    where = f"steps[{index}]"
    if not isinstance(value, dict):
        raise ProtocolError(f"{where} must be an object")
    missing = sorted(_STEP_KEYS - value.keys())
    if missing:
        raise ProtocolError(f"{where} is missing {', '.join(missing)}")
    _only_keys(value, _STEP_KEYS | _ROLE_KEYS, where)
    step_id = value["id"]
    if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
        raise ProtocolError(f"{where}.id must be a short label of letters, digits, '-', '_' or '.'; got {step_id!r}")
    instruction = value["instruction"]
    if not isinstance(instruction, str) or not instruction.strip():
        raise ProtocolError(f"{where}.instruction must be a non-empty string")
    criteria = value["acceptance_criteria"]
    if not isinstance(criteria, list) or not all(isinstance(c, str) and c.strip() for c in criteria):
        raise ProtocolError(f"{where}.acceptance_criteria must be a list of non-empty strings")
    depends_on = value["depends_on"]
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        raise ProtocolError(f"{where}.depends_on must be a list of step ids")
    if len(set(depends_on)) != len(depends_on):
        raise ProtocolError(f"{where}.depends_on lists a step twice")
    review = value["review"]
    if not isinstance(review, bool):
        raise ProtocolError(f"{where}.review must be true or false")
    roles = {}
    for key in ("role", "review_role"):
        raw = value.get(key)
        if raw is not None and (not isinstance(raw, str) or not raw):
            raise ProtocolError(f"{where}.{key} must be a role's id")
        roles[key] = raw
    return StepSpec(step_id, instruction.strip(), tuple(c.strip() for c in criteria), tuple(depends_on), review,
                    **roles)


def _only_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ProtocolError(f"{where} has unknown keys: {', '.join(unknown)}")


def _optional_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ProtocolError(f"{key} must be a string")
    return value.strip()


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = _optional_text(data, key)
    if not value:
        raise ProtocolError(f"{key} is required and must be a non-empty string")
    return value


# Appended to the (editable) system prompt on every Planner turn: the replies
# are parsed by code, so their shape is owned by code too. ``reply_contract`` fills in the retries a step has.
_REPLY_CONTRACT = """\
## Reply contract (required; code parses it)
Each message you get starts with [PLAN], [DECISION], [CHAT] or [RESTART]. End
every reply with exactly one ```json fenced block in that message's shape, and
nothing after it. Use only the keys shown. Text before the block is kept in the
run's history only. The first message of a new session for a task under way
starts with a [RESUME] brief on what your earlier session left; the request
follows it, and you answer the request.

[PLAN]: split the task into steps. Each step runs in its own Worker, a fresh
Claude Code session with full tools in the working directory that sees the
task's requirement, its own step's instruction and acceptance criteria, what
the steps it depends on reported, and the project background you see when
the project has one.
```json
{{"steps": [{{"id": "s1", "instruction": "...", "acceptance_criteria": ["..."], "depends_on": [], "review": false}}]}}
```
- At least 1 step. id: a short unique label (letters, digits, - _ .).
- depends_on: ids of the steps whose results this one needs before it starts,
  and only those; no cycles. Steps without a dependency between them run at
  the same time in the same directory, so a step that touches another step's
  files must depend on it.
- review: true when an independent Reviewer must pass the step before it counts
  as done.
{roles}
[DECISION]: decide what happens to a step after its Worker's or Reviewer's
result. The message lists the decisions allowed now.
```json
{{"decision": "accept", "reason": "...", "summary": "...", "progress": {{"done": "...", "now": "...", "next": "...", "risks": "...", "estimate": "..."}}}}
```
- accept: the step is done. summary: what it delivered.
- review: an independent Reviewer checks the step's work; the step then needs
  a passing review before it can be accepted, also after a retry.
- retry: the step runs again in a new Worker; add "feedback" (required): what
  to fix. At most {max_retries} retries per step.
- replan: "steps" (the PLAN step shape) replaces every step that has not
  started; started steps cannot change, and the new steps may depend on them.
  The current step counts as done when its result can be accepted, and is
  dropped otherwise. "steps" may be empty only when the current step can be
  accepted.
- fail: the task cannot be done; "reason" (required) says why.
"reason" is optional for the other decisions.
- progress (in every DECISION and SUPERVISE reply): where the task stands, for
  the user's task page, each field a short text ("none" when there is
  nothing): done (delivered so far), now (running now), next (what comes
  after), risks (what may delay or break it), estimate (how much longer,
  roughly).
- final (required when the message says the step is the task's last open
  one and your decision completes the task: an accept, or a replan with no
  new steps): the task's final summary for the user, each field a non-empty
  text: {{"delivered": "...", "how_to_use": "...", "verification": "...",
  "open_issues": "..."}}: what the task delivered, how to use it, the evidence
  that it works (commands run and what they showed), and what is left open
  ("none" when nothing).

[CHAT]: the task changed: the user added a supplement to it (every message
the user sends you is one) or edited its requirement. Answer, and decide
whether the plan still fits the task as it is now.
```json
{{"reply": "...", "action": "none"}}
```
- reply: your answer to the user, in their language.
- action "none": the plan fits as it is.
- action "forward": also pass "text" to the Worker running step "step" (a step
  the message lists as running); it reads the text at its next turn.
- action "add_steps": add "steps" (the PLAN step shape, with new ids) to the
  plan.
- action "replan": "steps" (the PLAN step shape) replaces every step that has
  not started; started steps cannot change, and the new steps may depend on
  them. "stop" (optional): running steps the change makes wrong; their
  Workers are stopped and the steps dropped. At least one step must be left
  to run.

[RESTART]: the task ended before it was done (it failed, or the user stopped
or cancelled it) and the user restarted it. It goes on from its plan:
accepted steps stay done and never run again; the steps that did not finish
are back to not started, and the journal says how their last attempt ended.
Reply in the [CHAT] shape. "reply" tells the user how the task goes on;
action "none" runs the steps not started as they are; "add_steps" and
"replan" change them first. No step runs yet, so there is no "forward" and
no "stop".

[SUPERVISE]: a periodic check on the Workers running now (no result arrived).
The message lists each running step and its recent activity. Review only the
ones that need it; leaving a step out is the same as "leave".
```json
{{"reviews": [{{"step": "s1", "action": "leave"}}], "progress": {{"done": "...", "now": "...", "next": "...", "risks": "...", "estimate": "..."}}}}
```
- leave: let the step keep running (the default; omit the step instead).
- nudge: pass "text" (required) to the step's Worker as its next turn, to
  steer it without interrupting its progress. Use it for a step that drifts or
  stalls but is still on a workable path.
- kill: stop the step's Worker now; "reason" (required) says why. Use it when
  the direction is wrong. You then get a DECISION turn for the stopped step
  (retry it with feedback, replan, or fail the task).
"""


# The PLAN step's role keys, in a project that lets its Planner define roles.
_ROLE_RULES = """\
- role (optional, like review_role; every key above stays required): the id
  of a work role you defined with looper_task_role_set; the step's Worker runs
  as that role. Leave it out for the built-in Worker.
- review_role (optional, with "review": true): the id of a review role you
  defined; once the built-in Reviewer passes the step, a Reviewer of that role
  reviews it too, and both must pass.
"""


def reply_contract(max_retries: int = DEFAULT_STEP_RETRIES, *, roles: bool = False) -> str:
    """The reply contract for a project whose steps may be retried ``max_retries`` times; with ``roles``, whose
    Planners may define the roles their steps run as."""
    return _REPLY_CONTRACT.format(max_retries=max_retries, roles=_ROLE_RULES if roles else "")


REPLY_CONTRACT = reply_contract()
