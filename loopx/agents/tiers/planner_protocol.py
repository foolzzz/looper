"""The reply contract between a task's Planner and its Claude.

Every Planner turn names the reply it needs ([PLAN], [DECISION] or [CHAT]),
and Claude ends its reply with exactly one fenced JSON object in that shape.
This module owns the shapes as typed values, validates a reply strictly
against the plan it applies to, and holds the contract text that code
appends to the editable system prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

MAX_STEPS = 8
MAX_RETRIES = 2  # per step

_STEP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
# A ```json fence opening a line. JSON strings cannot hold a raw newline, so a
# fence inside a string value (an instruction quoting code) never matches.
_OPENING = re.compile(r"^```json[ \t]*\n", re.MULTILINE)
_CLOSING = re.compile(r"\s*```")
_STEP_KEYS = frozenset({"id", "instruction", "acceptance_criteria", "depends_on", "review"})


class TurnKind(str, Enum):
    PLAN = "PLAN"
    DECISION = "DECISION"
    CHAT = "CHAT"
    SUPERVISE = "SUPERVISE"


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


@dataclass(frozen=True)
class PlanReply:
    steps: tuple[StepSpec, ...]


@dataclass(frozen=True)
class DecisionReply:
    decision: Decision
    reason: str
    feedback: str  # retry only
    steps: tuple[StepSpec, ...]  # replan only
    summary: str


@dataclass(frozen=True)
class ChatReply:
    reply: str
    action: ChatAction
    step: str  # forward only: the running step whose Worker gets ``text``
    text: str  # forward only
    steps: tuple[StepSpec, ...]  # add_steps only


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


@dataclass(frozen=True)
class PlanView:
    """What validation needs to know about the current plan, by step label.

    ``started`` maps each live step that has run (or runs) to its
    dependencies and ``pending`` each step not started yet. ``retired``
    holds the labels of dropped steps, which cannot be reused or depended
    on; ``spent`` counts the dropped steps that had run, which still count
    toward ``MAX_STEPS``. ``running`` holds the steps a Worker runs now.
    """

    started: Mapping[str, tuple[str, ...]]
    pending: Mapping[str, tuple[str, ...]]
    retired: frozenset[str] = frozenset()
    spent: int = 0
    running: frozenset[str] = frozenset()

    def dropping(self, step: str) -> PlanView:
        """This plan once ``step``, a started step, is dropped."""
        started = {k: v for k, v in self.started.items() if k != step}
        return PlanView(started, self.pending, self.retired | {step}, self.spent + 1, self.running - {step})


@dataclass(frozen=True)
class DecisionContext:
    """The step result a decision is about."""

    step: str
    result: ResultKind
    succeeded: bool  # the Worker reported completed / the Reviewer passed
    review_required: bool  # the step needs a passing review before it is accepted
    retries: int

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
        if self.retries < MAX_RETRIES:
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


def parse_plan(text: str) -> PlanReply:
    data = extract_reply(text)
    _only_keys(data, {"steps"}, "the PLAN reply")
    steps = _step_list(data.get("steps"), "steps")
    if not steps:
        raise ProtocolError("steps must list at least one step")
    _check_new_steps(steps, PlanView(started={}, pending={}), replace_pending=True)
    return PlanReply(steps)


def parse_decision(text: str, context: DecisionContext, plan: PlanView) -> DecisionReply:
    data = extract_reply(text)
    _only_keys(data, {"decision", "reason", "feedback", "steps", "summary"}, "the DECISION reply")
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
    return DecisionReply(decision, reason, feedback, steps, summary)


def parse_chat(text: str, plan: PlanView) -> ChatReply:
    data = extract_reply(text)
    _only_keys(data, {"reply", "action", "step", "text", "steps"}, "the CHAT reply")
    reply = _required_text(data, "reply")
    raw = data.get("action")
    try:
        action = ChatAction(raw)
    except ValueError:
        choices = ", ".join(a.value for a in ChatAction)
        raise ProtocolError(f"action must be one of {choices}; got {raw!r}") from None
    if action is not ChatAction.FORWARD and ("step" in data or "text" in data):
        raise ProtocolError("step and text are only for action forward")
    if action is not ChatAction.ADD_STEPS and "steps" in data:
        raise ProtocolError("steps is only for action add_steps")

    step = text_out = ""
    steps: tuple[StepSpec, ...] = ()
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
    return ChatReply(reply, action, step, text_out, steps)


def parse_supervise(text: str, running: frozenset[str]) -> SuperviseReply:
    """A SUPERVISE reply: a review for each running step the Planner names.

    ``running`` is the set of step labels a Worker runs now; a review may name
    any subset of them, each at most once. An empty list is allowed (leave
    everything running).
    """
    data = extract_reply(text)
    _only_keys(data, {"reviews"}, "the SUPERVISE reply")
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
    return SuperviseReply(tuple(reviews))


def _refusal(decision: Decision, context: DecisionContext) -> str:
    allowed = ", ".join(d.value for d in context.allowed())
    if decision is Decision.RETRY:
        why = f"step {context.step} has used its {MAX_RETRIES} retries"
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
    graph = {**kept, **{s.id: s.depends_on for s in steps}}
    for step in steps:
        for dependency in step.depends_on:
            if dependency == step.id:
                raise ProtocolError(f"step {step.id} depends on itself")
            if dependency in plan.retired:
                raise ProtocolError(f"step {step.id} depends on {dependency}, which is dropped")
            if dependency not in graph:
                raise ProtocolError(f"step {step.id} depends on unknown step {dependency!r}")
    total = len(graph) + plan.spent
    if total > MAX_STEPS:
        raise ProtocolError(
            f"the task would have {total} steps (dropped steps that ran count too); at most {MAX_STEPS} are allowed"
        )
    cycle = _find_cycle(graph)
    if cycle:
        raise ProtocolError("depends_on has a cycle: " + " -> ".join(cycle))


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
    _only_keys(value, _STEP_KEYS, where)
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
    return StepSpec(step_id, instruction.strip(), tuple(c.strip() for c in criteria), tuple(depends_on), review)


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
# are parsed by code, so their shape is owned by code too.
REPLY_CONTRACT = f"""\
## Reply contract (required; code parses it)
Each message you get starts with [PLAN], [DECISION] or [CHAT]. End every reply
with exactly one ```json fenced block in that message's shape, and nothing
after it. Use only the keys shown. Text before the block is kept in the run's
history only.

[PLAN]: split the task into steps. Each step runs in its own Worker, a fresh
Claude Code session with full tools in the working directory that sees only
its step's instruction and acceptance criteria.
```json
{{"steps": [{{"id": "s1", "instruction": "...", "acceptance_criteria": ["..."], "depends_on": [], "review": false}}]}}
```
- 1 to {MAX_STEPS} steps. id: a short unique label (letters, digits, - _ .).
- depends_on: ids of the steps that must be done before this one starts; no
  cycles. Steps without a dependency between them may run at the same time in
  the same directory, so a step that touches another step's files must depend
  on it.
- review: true when an independent Reviewer must pass the step before it counts
  as done.

[DECISION]: decide what happens to a step after its Worker's or Reviewer's
result. The message lists the decisions allowed now.
```json
{{"decision": "accept", "reason": "...", "summary": "..."}}
```
- accept: the step is done. summary: what it delivered; when this accept
  finishes the task (every other step is done), summarize the whole task for
  the user.
- review: an independent Reviewer checks the step's work; the step then needs
  a passing review before it can be accepted, also after a retry.
- retry: the step runs again in a new Worker; add "feedback" (required): what
  to fix. At most {MAX_RETRIES} retries per step.
- replan: "steps" (the PLAN step shape) replaces every step that has not
  started; started steps cannot change. The current step counts as done when
  its result can be accepted, and is dropped otherwise. "steps" may be empty
  only when the current step can be accepted.
- fail: the task cannot be done; "reason" (required) says why.
"reason" is optional for the other decisions.

[CHAT]: answer a message from the user about the task.
```json
{{"reply": "...", "action": "none"}}
```
- reply: your answer to the user, in their language.
- action "forward": also pass "text" to the Worker running step "step" (a step
  the message lists as running); it reads the text at its next turn.
- action "add_steps": add "steps" (the PLAN step shape, with new ids) to the
  plan.

[SUPERVISE]: a periodic check on the Workers running now (no result arrived).
The message lists each running step and its recent activity. Review only the
ones that need it; leaving a step out is the same as "leave".
```json
{{"reviews": [{{"step": "s1", "action": "leave"}}]}}
```
- leave: let the step keep running (the default; omit the step instead).
- nudge: pass "text" (required) to the step's Worker as its next turn, to
  steer it without interrupting its progress. Use it for a step that drifts or
  stalls but is still on a workable path.
- kill: stop the step's Worker now; "reason" (required) says why. Use it when
  the direction is wrong. You then get a DECISION turn for the stopped step
  (retry it with feedback, replan, or fail the task).
"""
