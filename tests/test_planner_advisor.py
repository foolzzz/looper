"""The Planner with the advisor on (the advisor RFC, section 11): a critical step's checkpoint, advise where a retry
is allowed after a failure, a request sent back, the advisor's questions in an [ADVICE] turn, the decision that must
say what it takes of the answer, and the advice the next Worker gets. The consults themselves are a stand-in here
(``tests/test_advisor_db.py`` covers them on PostgreSQL and a Claude Code stand-in)."""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.advice import AdviceResponse, AdviceStatus
from looper.db.agent_spawns import SpawnState
from tests.test_advice import _request
from tests.test_planner_agent import FINAL, WORK_OK, _project, decide, drive, plan, reply, step

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
    """The consult service, answering each submitted request with the next scripted outcome."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.opened: list[Any] = []
        self.submitted: list[Any] = []
        self.withdrawn: list[str] = []
        self.adopted: list[Any] = []
        self.unlocked: list[tuple[str, int, bool]] = []
        self.rounds = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.agents import advisor
        from looper.db import advisor_advice, agent_spawns

        async def open_consult(brief: Any, *, budget_usd: float) -> int:
            self.opened.append((brief, budget_usd))
            return len(self.opened)

        async def submit(spawn_id: int, raw: Any, bindings: Any = None, **kwargs: Any) -> Any:
            self.submitted.append(raw)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, advisor.Ended) and outcome.state in (SpawnState.NEEDS_INFO, SpawnState.ANSWERED):
                self.rounds += 1
            return outcome

        async def withdraw(spawn_id: int, reason: str) -> Any:
            self.withdrawn.append(reason)
            return advisor.Ended(SpawnState.FAILED, failure="withdrawn")

        async def get(spawn_id: int) -> dict[str, Any]:
            return {"rounds": self.rounds, "inputs": {"request": _request(), "bindings": []},
                    "result": {"needs_info": NEEDS["needs_info"]}}

        async def usage(task_id: str, step_id: str | None) -> tuple[int, int, float]:
            return len(self.opened), len(self.opened), 0.0

        async def none(*args: Any, **kwargs: Any) -> Any:
            return None

        async def adopt(spawn_id: int, adoption: Any) -> None:
            self.adopted.append(adoption)

        async def unlock(task_id: str, step_id: str, attempt: int, passed: bool) -> int:
            self.unlocked.append((step_id, attempt, passed))
            return 1

        async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        monkeypatch.setattr(advisor, "open_consult", open_consult)
        monkeypatch.setattr(advisor, "submit", submit)
        monkeypatch.setattr(advisor, "withdraw", withdraw)
        monkeypatch.setattr(agent_spawns, "get", get)
        monkeypatch.setattr(agent_spawns, "usage", usage)
        monkeypatch.setattr(agent_spawns, "has_source", lambda *a, **k: none())
        monkeypatch.setattr(advisor_advice, "adopt", adopt)
        monkeypatch.setattr(advisor_advice, "unlock", unlock)
        monkeypatch.setattr(advisor_advice, "for_step", nothing)


def _answered(fields: dict[str, Any] = ADVICE) -> Any:
    from looper.agents import advisor

    return advisor.Ended(SpawnState.ANSWERED, AdviceResponse(AdviceStatus(fields["status"]), fields))


def _asked() -> Any:
    from looper.agents import advisor

    return advisor.Ended(SpawnState.NEEDS_INFO, AdviceResponse(AdviceStatus.NEEDS_INFO, NEEDS))


@pytest.fixture
def advisor_on(agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "advisor": "on"}


def _critical(step_id: str) -> dict[str, Any]:
    return {**step(step_id), "difficulty": "critical"}


def test_a_critical_step_starts_after_the_advisor_reviewed_its_plan_with_the_advice_taken(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults(_answered())
    consults.install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")),
                            decide("advise", advice_request=_request("plan_review")),
                            decide("proceed", feedback="keep the old path", advice_adoption=ADOPTION),
                            decide("accept"))
    h = drive(claude, lambda envelope: WORK_OK)
    texts = [turn["text"] for turn in claude.turns()]
    assert texts[1].startswith("[DECISION] Step s1 (step 1) is critical: before its Worker starts")
    assert "Allowed decisions now: advise." in texts[1] and "Local material for your request" in texts[1]
    assert "Read each key once and keep it." in texts[2] and '"advice_adoption"' in texts[2]
    assert "Allowed decisions now: proceed, replan, fail." in texts[2]
    assert h.dispatched_at_turn == [3]  # its Worker started after the checkpoint's decision
    advice = h.work_items[0].payload["advice"]
    assert "q1 (adopted): Read each key once and keep it." in advice and "cache the reads" not in advice
    assert "The Planner's note: keep the old path" in advice
    brief, spend = consults.opened[0]
    assert (brief.source.value, brief.attempt, spend) == ("checkpoint", 1, 1.0)
    assert consults.adopted == [{"adoption": "adopted", "items": [
        {"ref": "q1", "use": "adopted", "steps": ["s1"], "why": "it fits"},
        {"ref": "s1", "use": "rejected", "steps": [], "why": "not yet"}]}]
    assert consults.unlocked == [(h.work_items[0].payload["task_id"], 1, True)]
    assert h.final().payload["status"] == "completed"


def test_a_checkpoint_without_an_answer_starts_the_step_as_planned(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor

    FakeConsults(advisor.Ended(SpawnState.BLOCKED, failure="blocked")).install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")), decide("advise", advice_request=_request("plan_review")),
                            decide("accept"))
    h = drive(claude, lambda envelope: WORK_OK)
    assert len(claude.turns()) == 3 and h.dispatched_at_turn == [2]
    assert "advice" not in h.work_items[0].payload


def test_after_a_failure_the_planner_may_ask_with_returns_and_questions_then_retries_with_the_advice(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor

    consults = FakeConsults(advisor.Returned(("problem (characters 3–20) looks like a local path",), 1),
                            _asked(), _answered())
    consults.install(monkeypatch)
    claude = planner_claude(
        plan(step("s1")),
        decide("advise", advice_request=_request()),
        decide("advise", advice_request=_request()),
        reply({"advice": "supplement", "answers": [{"needs_info_id": "n1", "answer": "Most of them."}]}),
        decide("retry", feedback="read once"),  # no advice_adoption: refused, and repaired
        decide("retry", feedback="read once", advice_adoption=ADOPTION),
        decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    h = drive(claude, lambda envelope: next(outcomes))
    texts = [turn["text"] for turn in claude.turns()]
    assert "Allowed decisions now: retry (2 left), advise, replan, fail." in texts[1]
    assert "You may ask the advisor first (advise)" in texts[1]
    assert texts[2].startswith("[DECISION] Your advice_request did not pass the checks") and "local path" in texts[2]
    assert texts[3].startswith("[ADVICE] The advisor needs more") and "n1: How many keys repeat?" in texts[3]
    assert "the advisor answered your request" in texts[4]
    assert "must carry \"advice_adoption\"" in texts[5]  # the repair turn
    assert "Allowed decisions now: retry (2 left), replan, fail." in texts[4]
    assert len(consults.submitted) == 3 and consults.submitted[2]["prior_rounds"] == [
        {"needs_info_id": "n1", "answer": "Most of them."}]
    assert (consults.opened[0][0].source.value, consults.opened[0][0].attempt) == ("optional", 2)
    first, second = h.work_items
    assert "advice" not in first.payload and "Read each key once" in second.payload["advice"]
    step_id = first.payload["task_id"]
    assert consults.unlocked == [(step_id, 1, False), (step_id, 2, True)]
    assert h.final().payload["status"] == "completed"


def test_a_planner_that_decides_without_the_advisor_after_a_return_withdraws_the_consult(
    planner_claude: Any, agent_db: Any, advisor_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor

    consults = FakeConsults(advisor.Returned(("the request does not stand on its own",), 1))
    consults.install(monkeypatch)
    claude = planner_claude(plan(step("s1")), decide("advise", advice_request=_request()),
                            decide("retry", feedback="read once"), decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    h = drive(claude, lambda envelope: next(outcomes))
    assert consults.withdrawn == ["the Planner decided without the advisor"]
    assert len(h.work_items) == 2 and "advice" not in h.work_items[1].payload


def test_with_the_advisor_off_a_failure_offers_no_advice_and_a_critical_step_is_refused(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    consults = FakeConsults()
    consults.install(monkeypatch)
    claude = planner_claude(plan(_critical("s1")), plan(step("s1")), decide("retry", feedback="again"),
                            decide("accept"))
    outcomes = iter([WORK_FAILED, WORK_OK])
    drive(claude, lambda envelope: next(outcomes))
    texts = [turn["text"] for turn in claude.turns()]
    assert "unknown keys: difficulty" in texts[1]
    assert "Allowed decisions now: retry (2 left), replan, fail." in texts[2] and "advise" not in texts[2]
    assert consults.opened == [] and consults.unlocked == []
    assert FINAL  # the scripted accepts carry it
