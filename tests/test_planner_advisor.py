"""The Planner with the advisor on (the advisor RFC, sections 11 and 12): a critical step's checkpoint, advise where
the Planner chooses it (after a success, a failure, or retries used up; before it plans), a request sent back, the
advisor's questions in an [ADVICE] turn, the decision that must say what it takes of the answer, a consult that
gives none, the new-evidence rule, and only the consulting step waiting. The consults themselves are a stand-in
here (``tests/test_advisor_db.py`` covers them on PostgreSQL and a Claude Code stand-in)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from looper.agents.advice import AdviceResponse, AdviceStatus
from looper.db.agent_spawns import SpawnState
from tests.test_advice import _request
from tests.test_planner_agent import WORK_OK, Harness, _project, decide, drive, plan, reply, step

WORK_FAILED = {"status": "failed", "error": "the loop still reads twice", "summary": "the loop still reads twice"}
ADVICE = {"status": "answered",
          "answers": [{"question_id": "q1", "conclusion": "Read each key once and keep it.", "basis": ["it repeats"],
                       "confidence": "high", "assumptions": []}],
          "options": [], "next_steps": [{"id": "s1", "action": "cache the reads", "verify": "one read per key"}],
          "risks": [], "needs_info": []}
NEEDS = {"status": "needs_info", "answers": [], "options": [], "next_steps": [], "risks": [],
         "needs_info": [{"id": "n1", "question": "How many keys repeat?", "why": "sizing", "blocking": True}]}
ADOPTION = {"adoption": "adopted", "items": [{"ref": "q1", "use": "adopted", "steps": ["s1"], "why": "it fits"},
                                             {"ref": "s1", "use": "rejected", "why": "not yet"}]}


class FakeConsults:
    """The consult service: each submitted request gets the next scripted outcome, once ``gate`` (when given) is
    set; ``asked`` is what the evidence check says was asked already (kinds), ``failed`` whether the scope's last
    consult failed with the same evidence."""

    def __init__(self, *outcomes: Any, gate: asyncio.Event | None = None) -> None:
        self.outcomes = list(outcomes)
        self.gate = gate
        self.opened: list[Any] = []
        self.submitted: list[Any] = []
        self.withdrawn: list[str] = []
        self.expired: list[str] = []
        self.adopted: list[Any] = []
        self.unlocked: list[tuple[str, int, bool]] = []
        self.asked: set[str] = set()
        self.failed = False
        self.rounds = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.agents import advisor, advisor_alerts
        from looper.db import advisor_advice, agent_spawns

        async def open_consult(brief: Any, *, budget_usd: float) -> int:
            self.opened.append((brief, budget_usd))
            return len(self.opened)

        async def submit(spawn_id: int, raw: Any, bindings: Any = None, **kwargs: Any) -> Any:
            self.submitted.append(raw)
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, advisor.Ended) and outcome.state in (SpawnState.NEEDS_INFO, SpawnState.ANSWERED):
                self.rounds += 1
            return outcome

        async def withdraw(spawn_id: int, reason: str) -> Any:
            self.withdrawn.append(reason)
            return advisor.Ended(SpawnState.FAILED, failure="withdrawn")

        async def expire(spawn_id: int, *, failure: str = "timeout", detail: str = "") -> Any:
            self.expired.append(failure)
            return advisor.Ended(SpawnState.FAILED, failure=failure, detail=detail)

        async def get(spawn_id: int) -> dict[str, Any]:
            return {"rounds": self.rounds, "inputs": {"request": _request(), "bindings": []},
                    "result": {"needs_info": NEEDS["needs_info"]}}

        async def usage(task_id: str, step_id: str | None) -> tuple[int, int, float]:
            return len(self.opened), len(self.opened), 0.0

        async def asked(task_id: str, step_id: str | None, *, kind: str | None = None, evidence: str,
                        states: tuple[str, ...] = ()) -> bool:
            return self.failed if states else kind in self.asked

        async def none(*args: Any, **kwargs: Any) -> Any:
            return None

        async def adopt(spawn_id: int, adoption: Any) -> None:
            self.adopted.append(adoption)

        async def unlock(task_id: str, step_id: str, attempt: int, passed: bool) -> int:
            self.unlocked.append((step_id, attempt, passed))
            return 1

        async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        async def summary_line(task_id: str) -> str:
            return f"请教 advisor {len(self.opened)} 次，花费 0.30 美元，采纳 1 条，其中 1 条帮步骤通过"

        async def for_task(task_id: str) -> list[dict[str, Any]]:
            return [{"id": n, "step_id": brief.step_id, "state": "failed"} for n, (brief, _) in enumerate(self.opened, 1)]

        monkeypatch.setattr(advisor, "open_consult", open_consult)
        monkeypatch.setattr(advisor, "submit", submit)
        monkeypatch.setattr(advisor, "withdraw", withdraw)
        monkeypatch.setattr(advisor, "expire", expire)
        monkeypatch.setattr(advisor_alerts, "consult_ended", none)
        monkeypatch.setattr(agent_spawns, "get", get)
        monkeypatch.setattr(agent_spawns, "usage", usage)
        monkeypatch.setattr(agent_spawns, "asked", asked)
        monkeypatch.setattr(agent_spawns, "has_source", lambda *a, **k: none())
        monkeypatch.setattr(agent_spawns, "for_task", for_task)
        monkeypatch.setattr(advisor_advice, "adopt", adopt)
        monkeypatch.setattr(advisor_advice, "unlock", unlock)
        monkeypatch.setattr(advisor_advice, "for_step", nothing)
        monkeypatch.setattr(advisor_advice, "summary_line", summary_line)


def _answered(fields: dict[str, Any] = ADVICE) -> Any:
    from looper.agents import advisor

    return advisor.Ended(SpawnState.ANSWERED, AdviceResponse(AdviceStatus(fields["status"]), fields))


def _asked() -> Any:
    from looper.agents import advisor

    return advisor.Ended(SpawnState.NEEDS_INFO, AdviceResponse(AdviceStatus.NEEDS_INFO, NEEDS))


def _ended(failure: str, state: SpawnState = SpawnState.FAILED) -> Any:
    from looper.agents import advisor

    return advisor.Ended(state, failure=failure, detail="")


@pytest.fixture
def advisor_on(agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "advisor": "on"}


def _critical(step_id: str) -> dict[str, Any]:
    return {**step(step_id), "difficulty": "critical"}


def _texts(claude: Any) -> list[str]:
    return [turn["text"] for turn in claude.turns()]


def test_a_critical_step_starts_after_the_advisor_reviewed_its_plan_with_the_planners_note(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import attempts
    from unittest.mock import AsyncMock

    monkeypatch.setattr(attempts, "dispatch_attempt", AsyncMock(return_value=41))
    consults = FakeConsults(_answered())
    consults.install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")),
                            decide("advise", advice_request=_request("plan_review")),
                            decide("proceed", feedback="read each key once", advice_adoption=ADOPTION),
                            decide("accept"))
    h = drive(claude, lambda envelope: WORK_OK)
    texts = _texts(claude)
    assert texts[1].startswith("[DECISION] Step s1 (step 1) is critical: before its Worker starts")
    assert "Allowed decisions now: advise." in texts[1] and "Local material for your request" in texts[1]
    assert "Read each key once and keep it." in texts[2] and '"advice_adoption"' in texts[2]
    assert "the Worker never sees the advisor's words" in texts[2]
    assert "Allowed decisions now: proceed, replan, fail." in texts[2]
    assert h.dispatched_at_turn == [3]  # its Worker started after the checkpoint's decision
    payload = h.work_items[0].payload
    assert payload["advisor_consult"] == 1 and "advice" not in payload
    assert "The Planner's note before this step starts: read each key once" in payload["instruction"]
    assert "cache the reads" not in str(payload)  # the advisor's words never reach the Worker
    brief, spend = consults.opened[0]
    assert brief.attempt_id is None and payload["attempt_id"] == 41
    assert (brief.source.value, brief.attempt, brief.asked, spend) == ("checkpoint", 1, "plan_review", 1.0)
    assert brief.evidence and consults.adopted[0]["adoption"] == "adopted"
    assert consults.unlocked == [(payload["task_id"], 1, True)]
    assert h.final().payload["status"] == "completed"
    # the task's summary has a line for its consults
    assert "## Advisor\n请教 advisor 1 次，花费 0.30 美元" in h.final().payload["summary"]


def test_a_checkpoint_without_an_answer_starts_the_step_as_planned(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeConsults(_ended("blocked", SpawnState.BLOCKED)).install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")), decide("advise", advice_request=_request("plan_review")),
                            decide("accept"))
    h = drive(claude, lambda envelope: WORK_OK)
    assert len(claude.turns()) == 3 and h.dispatched_at_turn == [2]
    assert "advisor_consult" not in h.work_items[0].payload
    phases = [call.kwargs.get("summary", "") for call in agent_db.insert_activity.await_args_list]
    assert any("its checkpoint consult did not finish; it starts as planned" in summary for summary in phases)


def test_after_a_failure_the_planner_asks_with_returns_and_questions_then_retries_in_its_own_words(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor, attempts
    from unittest.mock import AsyncMock

    monkeypatch.setattr(attempts, "dispatch_attempt", AsyncMock(side_effect=[41, 42]))

    consults = FakeConsults(advisor.Returned(("problem (characters 3–20) looks like a local path",), 1),
                            _asked(), _answered())
    consults.install(monkeypatch)
    claude = planner_claude(
        plan(step("s1")),
        decide("advise", advice_request=_request()),
        decide("advise", advice_request=_request()),
        reply({"advice": "supplement", "answers": [{"needs_info_id": "n1", "answer": "Most of them."}]}),
        decide("retry", feedback="read once"),  # no advice_adoption: refused, and repaired
        decide("retry", feedback="read each key once and cache it", advice_adoption=ADOPTION),
        decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    h = drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert "Allowed decisions now: retry (2 left), advise, replan, fail." in texts[1]
    assert "You may ask the advisor first (advise)" in texts[1] and "Local material for a request" in texts[1]
    assert texts[2].startswith("[DECISION] Your advice_request about step s1 did not pass the checks")
    assert "local path" in texts[2]
    assert texts[3].startswith("[ADVICE] The advisor needs more") and "n1: How many keys repeat?" in texts[3]
    assert "the advisor answered your request" in texts[4]
    assert "must carry \"advice_adoption\"" in texts[5]  # the repair turn
    assert "Allowed decisions now: retry (2 left), replan, fail." in texts[4]
    assert len(consults.submitted) == 3 and consults.submitted[2]["prior_rounds"] == [
        {"needs_info_id": "n1", "answer": "Most of them."}]
    assert (consults.opened[0][0].source.value, consults.opened[0][0].attempt) == ("optional", 2)
    first, second = h.work_items
    assert consults.opened[0][0].attempt_id == first.payload["attempt_id"] == 41
    assert second.payload["attempt_id"] == 42
    assert "advisor_consult" not in first.payload and second.payload["advisor_consult"] == 1
    assert second.payload["feedback"] == ["read each key once and cache it"]
    assert "Read each key once and keep it." not in str(second.payload)
    step_id = first.payload["task_id"]
    assert consults.unlocked == [(step_id, 1, False), (step_id, 2, True)]
    assert h.final().payload["status"] == "completed"


def test_advise_is_offered_after_a_success_and_when_the_retries_are_used_up(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeConsults().install(monkeypatch)
    agent_db.get_task_project.return_value = {**_project(), "advisor": "on", "max_step_retries": 0}
    claude = planner_claude(plan(step("s1"), step("s2")), decide("accept", final=None), decide("fail", reason="no"))
    outcomes = iter([WORK_OK, WORK_FAILED])
    drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert "advise" in texts[1].split("Allowed decisions now:")[1].split("\n")[0]  # after a success
    assert texts[2].split("Allowed decisions now:")[1].startswith(" advise, replan, fail.")  # no retries left


def test_only_the_consulting_step_waits_while_the_others_are_decided(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    consults = FakeConsults(_answered(), gate=gate)
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1"), step("s2")),
                            decide("advise", advice_request=_request()),
                            decide("accept", final=None),
                            decide("retry", feedback="read each key once", advice_adoption=ADOPTION),
                            decide("accept"))
    outcomes = {"do s1": iter([WORK_FAILED, WORK_OK]), "do s2": iter([None])}  # s2's Worker is held

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: consults.submitted)  # s1 consults
        s1 = h.planner._plan.step(h.work_items[0].payload["task_id"])
        assert s1.status == "consulting"
        await h.release(WORK_OK)  # s2's result comes meanwhile, and is decided
        await h.until(lambda: len(h.claude.turns()) >= 3)
        assert s1.status == "consulting"
        gate.set()
        await h.finished()

    h = drive(claude, lambda envelope: next(outcomes[envelope.payload["instruction"].split("\n")[0]]), scenario)
    texts = _texts(claude)
    assert "Step s1" in texts[1] and "Step s2" in texts[2] and "the advisor answered" in texts[3]
    assert [w.payload["instruction"].split("\n")[0] for w in h.work_items] == ["do s1", "do s2", "do s1"]
    assert h.final().payload["status"] == "completed"


def test_a_consult_that_ends_after_the_other_steps_were_accepted_completes_the_task_with_its_final_summary(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    consults = FakeConsults(_answered(), gate=gate)
    consults.install(monkeypatch)
    final = {"delivered": "the loop reads each key once", "how_to_use": "run it", "verification": "tests pass",
             "open_issues": "none"}
    claude = planner_claude(plan(step("s1"), step("s2")),
                            decide("advise", advice_request=_request()),  # s1 succeeded: s2 is still open
                            decide("accept", final=None),  # s2: s1 is still open
                            decide("accept", final=final, advice_adoption=ADOPTION))  # s1, now the last open step
    outcomes = {"do s1": iter([WORK_OK]), "do s2": iter([None])}

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: consults.submitted)
        await h.release(WORK_OK)
        await h.until(lambda: len(h.claude.turns()) >= 3)
        gate.set()
        await h.finished()

    h = drive(claude, lambda envelope: next(outcomes[envelope.payload["instruction"].split("\n")[0]]), scenario)
    texts = _texts(claude)
    assert "the advisor answered" in texts[3] and "This is the task's last open step" in texts[3]
    result = h.final().payload  # type: ignore[union-attr]
    assert result["status"] == "completed" and "the loop reads each key once" in result["summary"]


def test_a_consult_before_a_replan_lets_the_running_step_finish_while_only_the_replanned_one_waits(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Looper replans in a DECISION (or a CHAT) turn, never in a later PLAN: a consult before a replan is advise in
    # the DECISION, and only the step it is about waits (the RFC, D16).
    gate = asyncio.Event()
    consults = FakeConsults(_answered(), gate=gate)
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1"), step("s2")),
                            decide("advise", advice_request=_request()),
                            decide("replan", reason="read each key once instead", steps=[step("s3")],
                                   advice_adoption=ADOPTION),
                            decide("accept", final=None),
                            decide("accept"))
    outcomes = {"do s1": iter([WORK_FAILED]), "do s2": iter([None]), "do s3": iter([WORK_OK])}  # s2 is held

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: consults.submitted)
        s1, s2 = (h.planner._plan.step(w.payload["task_id"]) for w in h.work_items[:2])
        assert (s1.status, s2.status) == ("consulting", "dispatched")
        gate.set()
        await h.until(lambda: len(h.work_items) == 3 and len(h.claude.turns()) >= 4)  # s3 ran and was accepted
        assert s1.status == "skipped" and s2.status == "dispatched"  # the running step went on through the replan
        await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, lambda envelope: next(outcomes[envelope.payload["instruction"].split("\n")[0]]), scenario)
    texts = _texts(claude)
    assert "the advisor answered your request" in texts[2] and "replan" in texts[2].split("Allowed decisions now:")[1]
    assert [w.payload["instruction"].split("\n")[0] for w in h.work_items] == ["do s1", "do s2", "do s3"]
    assert consults.adopted and h.final().payload["status"] == "completed"


def test_a_consult_that_gives_no_answer_says_why_and_advise_waits_for_new_evidence(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults(_ended("rate_limited"))
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1")), decide("advise", advice_request=_request()),
                            decide("retry", feedback="read once"), decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert "The consult gave no answer: rate_limited, the advisor's model is rate-limited or out of quota" in texts[2]
    assert "Allowed decisions now: retry (2 left), replan, fail." in texts[2]


def test_a_repeat_of_the_same_question_with_nothing_new_is_refused_and_a_failed_scope_offers_no_advise(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults(_answered())
    consults.asked = {"diagnose"}
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1")), decide("advise", advice_request=_request()),
                            decide("advise", advice_request=_request("unblock")),
                            decide("retry", feedback="read once", advice_adoption=ADOPTION), decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert "you asked the advisor a diagnose about this already, and nothing new is known since" in texts[2]
    assert [r["kind"] for r in consults.submitted] == ["unblock"]

    consults = FakeConsults()
    consults.failed = True
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1")), decide("retry", feedback="again"), decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    drive(claude, lambda envelope: next(outcomes))
    assert "advise" not in _texts(claude)[1].split("Allowed decisions now:")[1].split("\n")[0]


def test_the_planner_may_ask_before_it_plans_and_its_plan_says_what_it_takes(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults(_answered())
    consults.install(monkeypatch)
    claude = planner_claude(reply({"advise": {"advice_request": _request("design_choice", constraints=["c"],
                                                                        options=[{"id": "o1", "summary": "a"},
                                                                                 {"id": "o2", "summary": "b"}])}}),
                            plan(step("s1")),  # no advice_adoption: refused, and repaired
                            reply({"steps": [step("s1")], "advice_adoption": ADOPTION}),
                            decide("accept"))
    h = drive(claude, lambda envelope: WORK_OK)
    texts = _texts(claude)
    assert "You may ask the advisor before you plan" in texts[0]
    assert texts[1].startswith("[PLAN]") and "The advisor answered your request about the plan" in texts[1]
    assert 'must carry "advice_adoption"' in texts[2]
    brief = consults.opened[0][0]
    assert (brief.step_id, brief.source.value, brief.attempt) == (None, "optional", 1)
    assert consults.adopted and h.final().payload["status"] == "completed"


def test_a_restart_during_the_plans_consult_plans_again_told_it_was_interrupted(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
    from looper.agents.tiers.planner import PlannerAgent

    consults = FakeConsults(_answered(), gate=asyncio.Event())  # the consult never answers before the stop
    consults.install(monkeypatch)
    claude = planner_claude(reply({"advise": {"advice_request": _request("design_choice", constraints=["c"],
                                                                        options=[{"id": "o1", "summary": "a"},
                                                                                 {"id": "o2", "summary": "b"}])}}),
                            plan(step("s1")), decide("accept"))

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: len(consults.submitted) == 1)
        await h.planner.stop()  # the server stops while the plan's consult runs
        assert agent_db.store.plans["t1"]["status"] == "planning" and consults.expired == ["interrupted"]
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
            payload={"task_id": "t1"}, correlation_id="corr-1"))
        await h.finished()

    h = drive(claude, lambda envelope: WORK_OK, scenario)
    texts = _texts(claude)
    assert texts[1].startswith("[PLAN]") and "The consult gave no answer: interrupted" in texts[1]
    assert "You may ask the advisor" not in texts[1]  # advise is not offered again
    assert h.final().payload["status"] == "completed"
    assert len(consults.opened) == 1


def test_a_consult_is_stopped_at_its_deadline_and_the_planner_decides_without_it(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor

    monkeypatch.setattr(advisor, "CONSULT_MAX_SEC", 0.5)
    consults = FakeConsults(advisor.Returned(("restate the path",), 1))
    consults.install(monkeypatch)
    submit = advisor.submit

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3)  # longer than the consult's whole time
        return await submit(*args, **kwargs)

    monkeypatch.setattr(advisor, "submit", slow)
    claude = planner_claude(plan(step("s1")), decide("advise", advice_request=_request()),
                            decide("retry", feedback="read once"), decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    h = drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert consults.expired == ["timeout"] and not consults.submitted  # stopped before its late outcome
    assert "The consult gave no answer: timeout" in texts[2]
    assert not any("restate the path" in text for text in texts)
    assert h.final().payload["status"] == "completed"


def test_a_supplement_or_a_requirement_edit_is_new_evidence(agent_db: Any, tmp_path: Any) -> None:
    """The user's supplements and requirement edits change what is known (the RFC, section 12.2), though a
    supplement leaves the requirement's text as it was."""
    from looper.db import task_changes
    from looper.db.task_changes import RequirementEdit
    from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan

    async def main() -> None:
        planner = PlannerAgent(task_id="t1", runtime_root=tmp_path)
        plan_ = TaskPlan("plan-1", "goal-1", "build the calc package", str(tmp_path))
        step_ = PlanStep("step-1", 0, "add the parser", key="s1")
        plan_.steps.append(step_)
        before = {"step": (await planner._evidence(plan_, step_)).fingerprint(),
                  "plan": (await planner._evidence(plan_, None)).fingerprint()}
        await task_changes.add_supplement("t1", "The input may be empty.", via="dashboard")
        supplemented = {"step": (await planner._evidence(plan_, step_)).fingerprint(),
                        "plan": (await planner._evidence(plan_, None)).fingerprint()}
        assert supplemented["step"] != before["step"] and supplemented["plan"] != before["plan"]
        agent_db.store.requirements["t1"] = RequirementEdit(2, "build the calc package, in Rust", 1,
                                                             "build the calc package")
        assert (await planner._evidence(plan_, step_)).fingerprint() != supplemented["step"]

    asyncio.run(main())


def test_with_the_advisor_off_nothing_offers_advise_and_a_critical_step_is_refused(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults()
    consults.install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")), plan(step("s1")), decide("retry", feedback="again"),
                            decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    h = drive(claude, lambda envelope: next(outcomes))
    texts = _texts(claude)
    assert "You may ask the advisor" not in texts[0] and "unknown keys: difficulty" in texts[1]
    assert "Allowed decisions now: retry (2 left), replan, fail." in texts[2] and "advise" not in texts[2]
    assert consults.opened == [] and consults.unlocked == []
    assert "## Advisor" not in h.final().payload.get("summary", "")
