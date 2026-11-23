"""Decision snapshots (the decision snapshot RFC): what a task's engine saw when it decided, and an offline replay of
what code decided.

``Dispatch`` captures one pass of the Planner's dispatch: every step as readiness reads it (``TaskPlan.ready_steps``),
the step workspace round's inputs when the pass runs one (``step_workspaces.plan_round``), and the decision: the
ready steps by kind and the work steps that start. ``step_decision`` records the Planner's decision on a step's
result as Looper applies it: the ``DecisionContext`` code computed for the turn, the decisions it allowed, the turn's
run record in digest (``turn_digest``), and the decision with its reason. ``replay`` runs the same pure functions on
a snapshot's inputs and says whether they give the recorded decision; a model's choice is not run again.

Snapshots are written only while ``decision_snapshots.enabled`` is on. Recording reads what the Planner already holds
and feeds nothing back; a failure to read the switch or to write is logged, and the decision goes on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING, Any

from ...db import decision_snapshots as db_snapshots
from ..acceptance_gate import Gate
from ..step_workspaces import Repo, Round, Unusable, plan_round
from .planner_protocol import Decision, DecisionContext, DecisionReply, ResultKind

if TYPE_CHECKING:
    from .planner import PlanStep, TaskPlan

logger = logging.getLogger(__name__)

VERSION = 1
# The most a snapshot's database work (the switch, the turn's record, the write; connection waits included) may take
# before it is dropped with a warning: the decision it records never waits longer.
TIMEOUT_SECONDS = 1.0
# What the activity line keeps of a decision's reason.
REASON_MAX_CHARS = 500
# The command-line flags that name a run's tools, on the runtimes that take them.
TOOL_FLAGS = ("--tools", "--allowedTools", "--disallowedTools")
# Everything ``StepOutcome.from_step`` reads of a step's result.
_RESULT_FIELDS = ("status", "passed")
# ``plan_round`` only asks which of these the task directory is.
_REPOS: Mapping[str, Repo | Unusable] = {
    "repo": Repo("", "", "", ""),
    "unusable": Unusable("", repository=True),
    "outside_git": Unusable("", repository=False),
}


async def enabled() -> bool:
    """Whether snapshots are written; off when the switch cannot be read within ``TIMEOUT_SECONDS``."""
    try:
        return await asyncio.wait_for(db_snapshots.enabled(), TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("reading %s timed out after %s s, decision snapshots stay off", db_snapshots.ENABLED_KEY,
                       TIMEOUT_SECONDS)
        return False
    except Exception as exc:
        logger.warning("could not read %s, decision snapshots stay off: %s", db_snapshots.ENABLED_KEY, exc)
        return False


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _obj(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --- Dispatch ---


def _step(step: PlanStep) -> dict[str, Any]:
    """``step`` as readiness reads it, and nothing more."""
    result = _obj(step.result)
    return {
        "id": step.step_id, "key": step.label, "kind": step.kind, "status": step.status,
        "depends_on": list(step.depends_on), "ready_when": step.ready_when, "reviews": step.reviews,
        "result": {key: result[key] for key in _RESULT_FIELDS if key in result},
        "gate": None if step.gate is None else _value(step.gate.state),
        "wait": None if step.wait is None else {"state": _value(step.wait.state),
                                                "outcome": _value(step.wait.outcome)},
    }


@dataclass(frozen=True)
class _Gate:
    state: str


@dataclass(frozen=True)
class _Wait:
    state: str
    outcome: str | None


@dataclass(frozen=True)
class _Step:
    """A step rebuilt from a snapshot: the fields ``TaskPlan.ready_steps`` and ``StepOutcome.from_step`` read."""

    step_id: str
    label: str
    kind: str
    status: str
    depends_on: list[str]
    ready_when: Any
    reviews: str | None
    result: dict[str, Any]
    gate: _Gate | None
    wait: _Wait | None

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> _Step:
        wait = raw["wait"]
        return cls(raw["id"], raw["key"], raw["kind"], raw["status"], list(raw["depends_on"]), raw["ready_when"],
                   raw["reviews"], dict(raw["result"]), None if raw["gate"] is None else _Gate(raw["gate"]),
                   None if wait is None else _Wait(wait["state"], wait["outcome"]))


def _repo_kind(repo: Repo | Unusable) -> str:
    return "repo" if isinstance(repo, Repo) else "unusable" if repo.repository else "outside_git"


def _dispatch_decision(ready: Sequence[Any], start: Sequence[Any], round_: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"ready": [{"step": s.step_id, "kind": s.kind} for s in ready], "start": [s.step_id for s in start],
            **({"round": dict(round_)} if round_ is not None else {})}


class Dispatch:
    """One dispatch pass's snapshot: captured where the pass reads which steps are ready, given the step workspace
    round when the pass runs one, and saved before its work steps start."""

    def __init__(self, plan: TaskPlan, *, isolate_steps: bool) -> None:
        self.inputs: dict[str, Any] = {"version": VERSION, "isolate_steps": isolate_steps,
                                       "steps": [_step(s) for s in plan.steps]}
        self._round: dict[str, Any] | None = None

    def round(self, in_flight: Sequence[bool], ready: Sequence[bool], repo: Repo | Unusable, decided: Round) -> None:
        """The inputs ``plan_round`` was called with and what it decided."""
        self.inputs["round"] = {"in_flight": list(in_flight), "ready": list(ready), "repo": _repo_kind(repo)}
        self._round = {"start": decided.start, "isolate": decided.isolate}

    def decision(self, ready: Sequence[PlanStep], start: Sequence[PlanStep]) -> dict[str, Any]:
        return _dispatch_decision(ready, start, self._round)

    async def save(self, task_id: str, ready: Sequence[PlanStep], start: Sequence[PlanStep]) -> None:
        try:
            await asyncio.wait_for(db_snapshots.add(task_id, "dispatch", inputs=self.inputs,
                                                    decision=self.decision(ready, start)), TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("could not record a decision snapshot (dispatch) for task %s: timed out after %s s",
                           task_id, TIMEOUT_SECONDS)
        except Exception:
            logger.warning("could not record a decision snapshot (dispatch) for task %s", task_id, exc_info=True)


def _replay_dispatch(inputs: Mapping[str, Any]) -> dict[str, Any]:
    from .planner import TaskPlan

    plan = TaskPlan(plan_id="", goal_id="", requirement="", work_dir="",
                    steps=[_Step.of(raw) for raw in inputs["steps"]])  # type: ignore[misc]
    ready = plan.ready_steps(isolate_steps=inputs["isolate_steps"])
    start = [s for s in ready if s.kind == "work"]
    round_ = None
    if start and inputs["isolate_steps"] and (seen := inputs.get("round")) is not None:
        decided = plan_round(in_flight=seen["in_flight"], ready=seen["ready"], repo=_REPOS[seen["repo"]])
        round_ = {"start": decided.start, "isolate": decided.isolate}
        start = start[:decided.start]
    return _dispatch_decision(ready, start, round_)


# --- The Planner's decision on a step's result ---


def _context(context: DecisionContext) -> dict[str, Any]:
    """Every field of ``context``, as JSON."""
    out: dict[str, Any] = {}
    for item in fields(DecisionContext):
        value = getattr(context, item.name)
        if item.name == "evidence" and value is None:
            continue
        if isinstance(value, Gate):
            value = {"passed": value.passed, "failed": list(value.failed), "errored": list(value.errored),
                     "stale": list(value.stale), "rechecks_used": value.rechecks_used,
                     "stale_reruns_used": value.stale_reruns_used}
        elif isinstance(value, Enum):
            value = value.value
        elif isinstance(value, frozenset):
            value = sorted(value)
        elif isinstance(value, tuple):
            value = [list(entry) if isinstance(entry, tuple) else entry for entry in value]
        out[item.name] = value
    return out


def _context_of(raw: Mapping[str, Any]) -> DecisionContext:
    values = dict(raw)
    values["result"] = ResultKind(values["result"])
    if values.get("open_gates") is not None:
        values["open_gates"] = frozenset(values["open_gates"])
    values["adjudication_findings"] = tuple(tuple(entry) for entry in values.get("adjudication_findings") or ())
    if (evidence := values.get("evidence")) is not None:
        values["evidence"] = Gate(**{**evidence, "failed": tuple(evidence["failed"]),
                                    "errored": tuple(evidence["errored"]), "stale": tuple(evidence["stale"])})
    return DecisionContext(**values)


def turn_digest(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a snapshot keeps of a turn's run record (``db.decision_snapshots.turn``): ids, hashes, tool flags and
    sizes, never a prompt's or a message's text."""
    if row is None:
        return None
    meta = _obj(row.get("metadata"))
    command = [str(arg) for arg in meta.get("command") or []]
    profile, choice = _obj(meta.get("profile")), _obj(meta.get("model_choice"))
    return {
        "record_id": row["id"],
        "profile": profile.get("id"), "runtime": _obj(profile.get("runtime")).get("type"),
        "model": choice.get("model"), "effort": choice.get("effort"),
        "system_prompt_sha256": meta.get("system_prompt_sha256"),
        # Each layer's hashes only: its sources and documents stay in the run record.
        "prompt_layers": {name: {key: value for key, value in layer.items() if key.endswith("sha256")}
                          for name, layer in _obj(meta.get("prompt_layers")).items() if isinstance(layer, Mapping)},
        "tools": {flag: command[at + 1] for at, flag in enumerate(command[:-1]) if flag in TOOL_FLAGS},
        "read_only": meta.get("read_only"),
        "library_sha256": _obj(meta.get("library")).get("sha256"),
        "sizes": {key: row.get(key) for key in ("input_chars", "input_tokens", "context_tokens", "cache_read_tokens")},
    }


async def step_decision(task_id: str, step: PlanStep, context: DecisionContext, reply: DecisionReply,
                        record_id: int | None, *, adjudicated_accept: bool = False) -> None:
    """Record the Planner's decision on ``step``'s result, made in the turn of run record ``record_id`` (offered an
    adjudicated accept or not), before Looper applies it, while snapshots are on; all of it within
    ``TIMEOUT_SECONDS``."""
    try:
        await asyncio.wait_for(_step_decision(task_id, step, context, reply, record_id, adjudicated_accept),
                               TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("could not record a decision snapshot (step_decision) for task %s step %s: timed out after "
                       "%s s", task_id, step.step_id, TIMEOUT_SECONDS)
    except Exception:
        logger.warning("could not record a decision snapshot (step_decision) for task %s step %s", task_id,
                       step.step_id, exc_info=True)


async def _step_decision(task_id: str, step: PlanStep, context: DecisionContext, reply: DecisionReply,
                         record_id: int | None, adjudicated_accept: bool) -> None:
    if not await db_snapshots.enabled():
        return
    turn = turn_digest(await db_snapshots.turn(record_id)) if record_id is not None else None
    inputs = {"version": VERSION, "step": step.step_id, "key": step.label, "attempt": step.retries + 1,
              "context": _context(context), "adjudicated_accept": adjudicated_accept, "turn": turn}
    reason = reply.feedback if reply.decision is Decision.RETRY else reply.reason
    decision = {
        "allowed": [allowed.value for allowed in context.offered(adjudicated_accept)],
        "decision": reply.decision.value,
        "reason": (reason or "")[:REASON_MAX_CHARS],
        "gates": [{"id": change.id, "action": _value(change.action)} for change in reply.gates],
        "waits": [{"id": change.id, "action": _value(change.action)} for change in reply.waits],
        "steps": [spec.id for spec in reply.steps],
    }
    await db_snapshots.add(task_id, "step_decision", step_id=step.step_id, record_id=record_id,
                           inputs=inputs, decision=decision)


# --- Replay ---


@dataclass(frozen=True)
class Replayed:
    """One snapshot replayed: whether its inputs give its recorded decision again, and what they give."""

    id: int
    kind: str
    same: bool
    recorded: Mapping[str, Any]
    replayed: Mapping[str, Any] | None
    note: str = ""


def replay(row: Mapping[str, Any]) -> Replayed:
    """Run the decision code of ``row``'s kind on its inputs: a dispatch is the same when ready, round and start all
    are; a step decision when the allowed decisions are and the recorded one is among them."""
    inputs, recorded = row["inputs"], row["decision"]
    if inputs.get("version") != VERSION:
        return Replayed(row["id"], row["kind"], False, recorded, None,
                        f"recorded in snapshot format {inputs.get('version')}; this version replays {VERSION}")
    try:
        if row["kind"] == "dispatch":
            replayed = _replay_dispatch(inputs)
            return Replayed(row["id"], row["kind"], replayed == recorded, recorded, replayed)
        offered = _context_of(inputs["context"]).offered(bool(inputs.get("adjudicated_accept", False)))
        allowed = [decision.value for decision in offered]
        same = allowed == recorded.get("allowed") and recorded.get("decision") in allowed
        return Replayed(row["id"], row["kind"], same, recorded, {"allowed": allowed})
    except Exception as exc:
        return Replayed(row["id"], row["kind"], False, recorded, None, f"cannot replay: {type(exc).__name__}: {exc}")
