"""Consults of the advisor (the advisor RFC, sections 5.2, 9, 10 and 12): a request's checks, the advisor's rounds,
and the consult's ``agent_spawns`` row through them.

A consult opens in ``drafting`` (``open_consult``) for one step's attempt, with what it may spend. Each request the
Planner writes (``submit``: the first one, one it rewrote, or one that answers what the advisor asked) goes through
three checks in order, the consult ``linting``:

1. **format** (``advice.parse_request``; a checkpoint's must be ``plan_review``);
2. **code** (``advice_lint.lint``): hits by category and position, never what they matched;
3. **the judge** (the ``advice_check`` role, one run with no tools over the request alone): standalone, no leak,
   answerable.

A request that fails one goes back to the Planner with what to fix (``Returned``), at most ``RETURNS_MAX`` times;
after that the consult is ``blocked``. A judge that cannot judge fails the consult: no request goes out unjudged.
A run that ends in error (``ChildFailure.RUNTIME``) is run once more; a rate-limited one is not, and fails the
consult (``rate_limited``). ``expire`` ends a consult that ran past its time.
A request that passes is the consult's question (``advisor_advice``) and starts a round of the advisor (the
``advisor`` role, ``spawn.run_child``, a fresh process each round): its answer (``advice.parse_response``) makes the
consult ``answered``; a question back makes it ``needs_info`` while rounds are left (``ROUNDS_MAX`` in all), else
it fails. Each round's cap is what the consult has left; a consult with less than ``spawn.MIN_RUN_USD`` left is
``over_budget``, as is one whose run reached its cap. Every run's record names the Planner's turn that asked as its
parent, and its cost is the task's.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from ..db import advisor_advice, agent_spawns
from ..db.agent_spawns import SpawnState
from .advice import (
    ADVISOR_CONTRACT, ADVISOR_ROLE_PROMPT, JUDGE_CONTRACT, JUDGE_ROLE_PROMPT, AdviceError, AdviceKind, AdviceRequest,
    AdviceResponse, AdviceStatus, advisor_turn, judge_turn, parse_bindings, parse_request, parse_response,
    parse_verdict,
)
from .advice_lint import describe, lint
from .agent_settings import AgentSettings, AgentType, load_agent_settings
from .model_call import CallFailed, CallFailure, CallResult, ChildCall, Execution, call
from .run_usage import CostStatus, cost_status
from .spawn import MIN_RUN_USD, ChildRun, SpawnKind, SpawnParent

logger = logging.getLogger(__name__)

RETURNS_MAX = 2
ROUNDS_MAX = 3  # the first round and two that answer what the advisor asked
ADVISOR_TIMEOUT_SEC = 300  # a round's
CONSULT_MAX_SEC = 900  # a whole consult's, its rounds and the Planner's turns about it included
JUDGE_TIMEOUT_SEC = 120
T = TypeVar("T")


class Source(str, Enum):
    """What started a consult (``advisor_advice.source``)."""

    CHECKPOINT = "checkpoint"  # code, before a critical step's Worker starts
    OPTIONAL = "optional"  # the Planner, where its decision allowed advise
    LADDER = "ladder"  # code, on a repeated failure (not in this phase)


@dataclass(frozen=True)
class Brief:
    """What a consult is about: its task's step (None: the plan), the attempt its advice is for, what started it,
    the run that asked (the Planner's turn), the kind of request the Planner first wrote and what was known when
    it asked (``planner_advice.Evidence.fingerprint``)."""

    task_id: str
    step_id: str | None
    attempt: int
    source: Source
    parent_record_id: int | None = None
    asked: str | None = None
    evidence: str | None = None
    attempt_id: int | None = None

    @property
    def kind(self) -> AdviceKind | None:
        """The only kind of request it takes: a checkpoint reviews a plan."""
        return AdviceKind.PLAN_REVIEW if self.source is Source.CHECKPOINT else None

    def row(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "step_id": self.step_id, "attempt": self.attempt,
                "source": self.source.value, **({"asked": self.asked} if self.asked else {}),
                **({"attempt_id": self.attempt_id} if self.attempt_id is not None else {}),
                **({"evidence": self.evidence} if self.evidence else {})}


@dataclass(frozen=True)
class Context:
    """What the code checks compare a request with: the local material the Planner had, this machine's directories,
    the task's ids and the project's internal domains."""

    material: str = ""
    local_paths: Sequence[str] = ()
    ids: Sequence[str] = ()
    domains: Sequence[str] = ()  # the project's internal domains


@dataclass(frozen=True)
class Returned:
    """A request sent back to the Planner: what to fix, and how many more times one may come back."""

    problems: tuple[str, ...]
    returns_left: int


@dataclass(frozen=True)
class Ended:
    """Where a consult got to: ``needs_info`` (``response`` holds its questions) or finished, with its answer when
    ``answered``, else why not (``failure``, ``detail``)."""

    state: SpawnState
    response: AdviceResponse | None = None
    failure: str | None = None
    detail: str = ""
    row: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Role:
    prompt: str
    recommended: bool
    timeout_seconds: int
    enabled: bool


async def _role(role: AgentType, default: str) -> _Role:
    from ..db import role_configs
    from .system_prompt import is_recommended

    config = await role_configs.get_config(role.value) or {}
    prompt = str(config.get("system_prompt") or "").strip() or default.strip()
    return _Role(prompt, is_recommended(prompt, str(config.get("default_prompt") or "") or default),
                 int(config.get("timeout_seconds") or 0), bool(config.get("enabled", True)))


def _system_prompt(role: AgentType, settings: AgentSettings, config: _Role, contract: str, name: str) -> Any:
    from .system_prompt import SystemPrompt, compose_system_prompt, prompt_layers

    custom = settings.profile.custom_prompt
    text = compose_system_prompt(config.prompt, custom_prompt=custom, contract=contract)
    return SystemPrompt(text, prompt_layers(role_id=role.value, role=config.prompt, recommended=config.recommended,
                                            contract_name=name, contract=contract, custom_prompt=custom))


async def open_consult(brief: Brief, *, budget_usd: float) -> int:
    """A new consult for ``brief``, waiting for its request (``drafting``); its id."""
    row = await agent_spawns.create(kind=SpawnKind.ADVISOR.value, task_id=brief.task_id, step_id=brief.step_id,
                                    parent_record_id=brief.parent_record_id, brief=brief.row(), inputs={},
                                    budget_usd=budget_usd)
    return int(row["id"])


def _brief(row: Mapping[str, Any]) -> Brief:
    data = row["brief"]
    return Brief(row["task_id"], row["step_id"], int(data["attempt"]), Source(data["source"]), row["parent_record_id"],
                 data.get("asked"), data.get("evidence"), data.get("attempt_id"))


def _left(row: Mapping[str, Any]) -> float:
    return round(float(row["budget_usd"]) - float(row["spent_usd"]), 6)


def _failure_state(failure: CallFailure) -> SpawnState:
    return SpawnState.OVER_BUDGET if failure is CallFailure.BUDGET else SpawnState.FAILED


async def _run(child: ChildRun, *, spawn_id: int, brief: Brief, settings: AgentSettings, cap_usd: float,
               runtime_root: Path, claude_bin: str, parse: Callable[[str], T],
               deadline: float | None = None) -> tuple[CallResult[T], float | None]:
    """Run ``child``, and once more when it ended in error (not when it was refused, rate-limited, over budget or
    out of time), within ``deadline``; the last run's result, and what both spent. Each run is noted on the consult
    when it starts and its spend booked when it ends, so a consult stopped meanwhile keeps what its runs spent
    (``reconcile``)."""
    async def started(record_id: int) -> None:
        await agent_spawns.note_run(spawn_id, record_id)

    async def booked(done: Execution) -> None:
        await _book(spawn_id, done)

    def unknown_cost(result: CallResult[T]) -> CallFailed | None:
        if result.record_id is None or cost_status(result.cost_usd) is CostStatus.KNOWN:
            return None
        models = ", ".join(result.execution.models) or "unknown"
        logger.warning("models %s: consult budget cannot be verified: cost unknown; no retry or answer", models)
        reason = f"its cost is unknown, so its cap of {cap_usd:g} USD cannot be checked"
        if isinstance(result, CallFailed) and result.failure is not CallFailure.RUNTIME:
            return result  # Keep timeout/refusal classification; these failures never retry or return advice.
        return CallFailed(execution=replace(result.execution, failure=CallFailure.BUDGET, reason=reason),
                          failure=CallFailure.BUDGET, reason=reason)

    from .cost_guard import Held

    try:
        done = await call(ChildCall(child, _parent(brief), settings, cap_usd, runtime_root, claude_bin,
                                    on_record=started, on_result=booked), parse=parse)
    except Held as held:
        # A usage window is not a runtime error: do not start the run again, and return no advice.
        failed = CallFailed(execution=Execution(None, failure=CallFailure.UNAVAILABLE, reason=str(held)),
                            failure=CallFailure.UNAVAILABLE, reason=str(held))
        return failed, 0.0
    if failed := unknown_cost(done):
        return failed, None
    spent = done.cost_usd or 0.0
    left = _time_left(deadline)
    if (isinstance(done, CallFailed) and done.failure is CallFailure.RUNTIME
            and round(cap_usd - spent, 6) >= MIN_RUN_USD and (left is None or left >= 1)):
        logger.info("%s run %s ended in error (%s); running it once more", child.record_kind, done.record_id,
                    done.reason)
        again = replace(child, run_id=uuid.uuid4().hex[:12],
                        timeout_s=child.timeout_s if left is None else min(child.timeout_s, left))
        try:
            done = await call(ChildCall(again, _parent(brief), settings, round(cap_usd - spent, 6), runtime_root,
                                        claude_bin, on_record=started, on_result=booked), parse=parse)
        except Held as held:
            # The retry is the same usage window, not another runtime error. Keep the first run's spend.
            failed = CallFailed(execution=Execution(None, failure=CallFailure.UNAVAILABLE, reason=str(held)),
                                failure=CallFailure.UNAVAILABLE, reason=str(held))
            return failed, spent
        if failed := unknown_cost(done):
            return failed, None
        spent = round(spent + (done.cost_usd or 0.0), 6)
    return done, spent


async def _book(spawn_id: int, done: Execution) -> None:
    if done.record_id is not None:
        await agent_spawns.book(spawn_id, done.record_id, done.cost_usd)


async def reconcile(spawn_id: int) -> None:
    """Book what each run the consult started spent and is not booked yet (one stopped while it ran): what its
    record says, once per run, so it can be called again."""
    from ..db import llm_records

    row = await agent_spawns.get(spawn_id)
    if row is None:
        return
    booked = set(row["result"].get("booked", ()))
    for record_id in row["result"].get("runs", ()):
        if record_id in booked:
            continue
        record = await llm_records.get_record(record_id)
        if record and record["status"] != "running":
            await agent_spawns.book(spawn_id, record_id,
                                    float(record["cost_usd"]) if record["cost_usd"] is not None else None)


async def submit(spawn_id: int, raw_request: Any, raw_bindings: Any = None, *, context: Context = Context(),
                 runtime_root: Path, claude_bin: str = "claude", timeout_s: float = ADVISOR_TIMEOUT_SEC,
                 deadline: float | None = None) -> Returned | Ended:
    """Check the request a consult waits for (``drafting``, or ``needs_info`` for one that answers the advisor) and,
    once it passes, run a round of the advisor. ``deadline`` (``time.monotonic``) is the end of the consult's whole
    time: no run goes past it, and none starts after it. Raises ``ValueError`` for a consult that waits for no
    request."""
    row = await agent_spawns.get(spawn_id)
    if row is None or row["state"] not in (SpawnState.DRAFTING.value, SpawnState.NEEDS_INFO.value):
        raise ValueError(f"consult {spawn_id} waits for no request")
    row = await agent_spawns.move(spawn_id, SpawnState.LINTING, expect=SpawnState(row["state"]))
    if row is None:
        raise ValueError(f"consult {spawn_id} moved meanwhile")
    brief = _brief(row)
    try:
        request = parse_request(raw_request, kind=brief.kind)
        bindings = parse_bindings(raw_bindings)
    except AdviceError as exc:
        return await _return(row, list(exc.problems), [{"stage": "format", "category": "format"}])
    hits = lint(request, bindings=bindings, material=context.material, local_paths=context.local_paths,
                ids=context.ids, domains=context.domains)
    if hits:
        return await _return(row, describe(hits), [hit.view() for hit in hits])
    if row.get("cost_status") == CostStatus.UNKNOWN.value:
        return await _end(row, SpawnState.OVER_BUDGET, failure="budget",
                          detail=f"its cost is unknown, so its cap of {float(row['budget_usd']):g} USD cannot be checked")
    if _left(row) < MIN_RUN_USD:
        return await _end(row, SpawnState.OVER_BUDGET, failure="budget",
                          detail=f"{_left(row):.2f} USD is left, less than a run needs")
    if (left := _time_left(deadline)) is not None and left <= 0:
        return await _end(row, SpawnState.FAILED, failure="timeout", detail=_PAST)
    judged = await _judge(row, brief, request, runtime_root=runtime_root, claude_bin=claude_bin, deadline=deadline)
    if isinstance(judged, Ended):
        return judged
    verdict, spent, judge = judged
    if not verdict.passed:
        return await _return(row, verdict.problems(),
                             [{"stage": "judge", **leak} for leak in verdict.view()["leaks"]
                              if (leak["category"], leak["field"]) in verdict.blocking]
                             or [{"stage": "judge", "category": "not_standalone" if not verdict.standalone
                                  else "unanswerable"}], append={"judges": judge})
    inputs = {"request": dict(request.fields), "bindings": [{"alias": b.alias, "refers_to": b.refers_to}
                                                            for b in bindings]}
    await advisor_advice.record_question(spawn_id=spawn_id, task_id=brief.task_id, step_id=brief.step_id,
                                         attempt=brief.attempt, source=brief.source.value, kind=request.kind.value,
                                         question=request.fields)
    from . import attempts
    await attempts.link_advice(brief.attempt_id, spawn_id)
    if round(_left(row) - spent, 6) < MIN_RUN_USD:
        return await _end(row, SpawnState.OVER_BUDGET, failure="budget",
                          detail="too little is left for a round of the advisor after the judge",
                          append={"judges": judge})
    if (left := _time_left(deadline)) is not None and left <= 0:
        return await _end(row, SpawnState.FAILED, failure="timeout", detail=_PAST, append={"judges": judge})
    row = await agent_spawns.move(spawn_id, SpawnState.ASKING, expect=SpawnState.LINTING,
                                  inputs=inputs, append={"judges": judge})
    return await _ask(row, brief, request, runtime_root=runtime_root, claude_bin=claude_bin,
                      timeout_s=timeout_s if left is None else min(timeout_s, left), deadline=deadline)


_PAST = f"the consult ran past {CONSULT_MAX_SEC // 60} minutes"


def _time_left(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


async def _judge(row: Mapping[str, Any], brief: Brief, request: AdviceRequest, *, runtime_root: Path,
                 claude_bin: str, deadline: float | None = None) -> Ended | tuple[Any, float, dict[str, Any]]:
    """The judge's verdict on ``request``, what it cost and its entry in the consult's result; else how the consult
    ended."""
    settings = await load_agent_settings(AgentType.ADVICE_CHECK, wait=True)
    config = await _role(AgentType.ADVICE_CHECK, JUDGE_ROLE_PROMPT)
    if not config.enabled:
        return await _end(row, SpawnState.FAILED, failure="judge",
                          detail="the advice_check role is turned off (Settings → Roles): no request goes out unjudged")
    child = ChildRun(SpawnKind.ADVISOR, _system_prompt(AgentType.ADVICE_CHECK, settings, config, JUDGE_CONTRACT,
                                                       "JUDGE_CONTRACT"),
                     judge_turn(request), timeout_s=min(JUDGE_TIMEOUT_SEC, config.timeout_seconds or JUDGE_TIMEOUT_SEC,
                                                        JUDGE_TIMEOUT_SEC if deadline is None else _time_left(deadline)),
                     max_turns=1, role=AgentType.ADVICE_CHECK, run_kind="advice_check", private=True)
    called, spent = await _run(child, spawn_id=row["id"], brief=brief, settings=settings, cap_usd=_left(row),
                               runtime_root=runtime_root, claude_bin=claude_bin, parse=parse_verdict, deadline=deadline)
    done = called.execution
    judged = {"record_id": done.record_id, "cost_usd": spent, "round": row["rounds"] + 1}
    if isinstance(called, CallFailed):
        if called.error is None:
            return await _end(row, _failure_state(called.failure),
                              failure="rate_limited" if called.failure is CallFailure.RATE_LIMITED else "judge",
                              detail=f"the judge could not judge the request: {called.reason}",
                              append={"judges": {**judged, "failure": called.failure.value}})
        if isinstance(called.error, AdviceError):
            return await _end(row, SpawnState.FAILED, failure="judge", detail=str(called.error),
                              append={"judges": {**judged, "failure": "parse"}})
        exc = called.error  # the run's spend is already booked
        logger.warning("cannot read the judge's verdict of consult %s", row["id"],
                       exc_info=(type(exc), exc, exc.__traceback__))
        return await _end(row, SpawnState.FAILED, failure="judge",
                          detail="the judge's reply could not be read",
                          append={"judges": {**judged, "failure": "parse"}})
    verdict = called.value
    return verdict, spent, {**judged, **verdict.view(), "passed": verdict.passed}


def _parent(brief: Brief) -> SpawnParent:
    return SpawnParent(brief.task_id, brief.step_id, brief.parent_record_id)


async def _ask(row: Mapping[str, Any], brief: Brief, request: AdviceRequest, *, runtime_root: Path, claude_bin: str,
               timeout_s: float, deadline: float | None = None) -> Ended:
    """One round of the advisor over ``request``, the consult ``asking``."""
    settings = await load_agent_settings(AgentType.ADVISOR, wait=True)
    config = await _role(AgentType.ADVISOR, ADVISOR_ROLE_PROMPT)
    if not config.enabled:
        return await _end(row, SpawnState.FAILED, failure="unavailable",
                          detail="the advisor role is turned off (Settings → Roles)")
    child = ChildRun(SpawnKind.ADVISOR, _system_prompt(AgentType.ADVISOR, settings, config, ADVISOR_CONTRACT,
                                                       "ADVISOR_CONTRACT"),
                     advisor_turn(request), timeout_s=min(timeout_s, config.timeout_seconds or timeout_s),
                     max_turns=2, role=AgentType.ADVISOR)
    called, spent = await _run(child, spawn_id=row["id"], brief=brief, settings=settings, cap_usd=_left(row),
                               runtime_root=runtime_root, claude_bin=claude_bin,
                               parse=lambda text: parse_response(text, request), deadline=deadline)
    done = called.execution
    rounds = {"rounds": {**done.round(), "cost_usd": spent}}
    if isinstance(called, CallFailed):
        if called.error is None:
            return await _end(row, _failure_state(called.failure), child=done,
                              failure=called.failure.value, detail=called.reason, append=rounds)
        if isinstance(called.error, AdviceError):
            return await _end(row, SpawnState.FAILED, child=done, failure="parse",
                              detail=f"the advisor's answer does not fit AdviceResponse v1: {called.error}", append=rounds)
        exc = called.error  # the run's spend is already booked
        logger.warning("cannot read the advisor's answer of consult %s", row["id"],
                       exc_info=(type(exc), exc, exc.__traceback__))
        return await _end(row, SpawnState.FAILED, child=done, failure="parse",
                          detail="the advisor's answer could not be read", append=rounds)
    response = called.value
    if response.status is AdviceStatus.NEEDS_INFO:
        if row["rounds"] >= ROUNDS_MAX:
            return await _end(row, SpawnState.FAILED, child=done,
                              failure="rounds_exhausted", detail="the advisor still needs more after its last round",
                              append=rounds, response=response)
        moved = await agent_spawns.move(row["id"], SpawnState.NEEDS_INFO, expect=SpawnState.ASKING,
                                        child_record_id=done.record_id,
                                        append=rounds, result={"status": response.status.value,
                                                               "needs_info": list(response.needs_info)})
        return Ended(SpawnState.NEEDS_INFO, response, row=moved or {})
    await advisor_advice.record_advice(row["id"], response.fields)
    return await _end(row, SpawnState.ANSWERED, child=done, append=rounds,
                      response=response)


async def _end(row: Mapping[str, Any], state: SpawnState, *, spent_usd: float = 0.0, child: Execution | None = None,
               failure: str | None = None, detail: str = "", append: Mapping[str, Mapping[str, Any]] | None = None,
               response: AdviceResponse | None = None) -> Ended:
    """Finish the consult in ``state`` from where it is now."""
    result: dict[str, Any] = {"failure": failure}
    if response is not None:
        result["status"] = response.status.value
    moved = await agent_spawns.move(row["id"], state, expect=SpawnState(row["state"]), spent_usd=spent_usd,
                                    child_record_id=child.record_id if child is not None else None,
                                    result=result, append=append, detail=detail or None)
    if state is SpawnState.OVER_BUDGET and (moved or row).get("cost_status") == CostStatus.UNKNOWN.value:
        # Keep the unknown-cost diagnosis in the record; the Planner gets the existing budget failure text.
        failure, detail = "budget", ""
    return Ended(state, response if state is SpawnState.ANSWERED else None, failure, detail, moved or {})


async def _return(row: Mapping[str, Any], problems: list[str], checks: list[dict[str, Any]], *,
                  spent_usd: float = 0.0, append: Mapping[str, Mapping[str, Any]] | None = None) -> Returned | Ended:
    """Send the request back to the Planner, or block the consult after its last return; the checks are recorded
    one by one, by category and position."""
    number = row["returns"] + 1
    marked = [{**check, "return": number, "round": row["rounds"] + 1} for check in checks]
    # Appended to the result as it is now: ``row`` was read before this round's runs were noted and booked.
    appended = {**(append or {}), "checks": marked}
    if row["returns"] >= RETURNS_MAX:
        moved = await agent_spawns.move(row["id"], SpawnState.BLOCKED, expect=SpawnState.LINTING,
                                        spent_usd=spent_usd, result={"failure": "blocked"}, append=appended,
                                        detail=f"its request failed the checks after {RETURNS_MAX} returns")
        return Ended(SpawnState.BLOCKED, failure="blocked", detail="the request failed the checks again",
                     row=moved or {})
    await agent_spawns.move(row["id"], SpawnState.DRAFTING, expect=SpawnState.LINTING, returned=True,
                            spent_usd=spent_usd, result=None, append=appended)
    return Returned(tuple(problems), RETURNS_MAX - number)


async def withdraw(spawn_id: int, reason: str) -> Ended | None:
    """The Planner withdrew a consult that waited for its request; None when it waited for none."""
    row = await agent_spawns.get(spawn_id)
    if row is None or row["state"] not in (SpawnState.DRAFTING.value, SpawnState.NEEDS_INFO.value):
        return None
    failure = "withdrawn" if row["state"] == SpawnState.NEEDS_INFO.value else "no_request"
    return await _end(row, SpawnState.FAILED, failure=failure, detail=reason[:1000])


async def expire(spawn_id: int, *, failure: str = "timeout", detail: str = "") -> Ended | None:
    """End a consult wherever it is, as ``failure``: by default one that ran past ``CONSULT_MAX_SEC``; None when it
    had ended."""
    await reconcile(spawn_id)
    row = await agent_spawns.get(spawn_id)
    if row is None or row["state"] in {s.value for s in agent_spawns.FINISHED}:
        return None
    return await _end(row, SpawnState.FAILED, failure=failure, detail=detail or _PAST)


async def fail_interrupted() -> int:
    """At the server's start: every consult a stopped server left unfinished failed (its runs are gone, and the
    Planner's turn that was writing its request with them; the Planner takes its decision again); how many."""
    count = 0
    for state in (SpawnState.DRAFTING, SpawnState.LINTING, SpawnState.ASKING, SpawnState.NEEDS_INFO):
        for spawn_id in await agent_spawns.ids_in(state):
            await reconcile(spawn_id)
            if await agent_spawns.move(spawn_id, SpawnState.FAILED, expect=state, result={"failure": "interrupted"},
                                       detail="interrupted by a restart") is not None:
                count += 1
    return count
