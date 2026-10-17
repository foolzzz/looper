"""A task's Planner runs the steps that name roles as those roles: a step's Worker as its work role (the version
current when the attempt starts), and a reviewed step's review role once the built-in Reviewer passed it, both
reviews needed before the step is accepted.

The Planner's Claude is a stand-in that answers each turn with a scripted reply (``planner_claude``); the Workers
and Reviewers are a stand-in pool (``tests.test_planner_agent.Harness``); the task's roles are kept in memory.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.tiers.planner import PlanStep, PlannerAgent, ReviewStage, TaskPlan
from tests.test_planner_agent import (
    REVIEW_FAIL,
    REVIEW_PASS,
    WORK_OK,
    _project,
    activity,
    decide,
    drive,
    plan,
    step,
)


class _Roles:
    """The task's roles as ``db.task_roles`` keeps them: every version, by key."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.db import task_roles

        self.rows: list[dict[str, Any]] = []
        monkeypatch.setattr(task_roles, "current", self.current)
        monkeypatch.setattr(task_roles, "get", self.get)

    def add(self, key: str, kind: str, prompt: str, *, profile_id: str | None = None) -> None:
        version = 1 + max((r["version"] for r in self.rows if r["role_key"] == key), default=0)
        self.rows.append({"task_id": "t1", "role_key": key, "version": version, "kind": kind, "role_prompt": prompt,
                          "harness": None, "profile_id": profile_id, "reason": "", "profile_name": None,
                          "profile_allowed": False})

    async def current(self, task_id: str, **_: Any) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.rows:
            latest[row["role_key"]] = row
        return list(latest.values())

    async def get(self, task_id: str, key: str, version: int | None = None, **_: Any) -> dict[str, Any] | None:
        rows = [r for r in self.rows if r["role_key"] == key and version in (None, r["version"])]
        return rows[-1] if rows else None


@pytest.fixture
def roles(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> _Roles:
    """The task's project lets its Planner define roles, and it defined ``tester`` and ``security``."""
    agent_db.get_task_project.return_value = {**_project(), "planner_roles": "on"}
    store = _Roles(monkeypatch)
    store.add("tester", "work", "You write the tests.")
    store.add("security", "review", "You check for security problems.")
    return store


def _role(envelope: MessageEnvelope) -> tuple[str, int] | None:
    role = envelope.payload.get("task_role")
    return None if role is None else (role["id"], role["version"])


def _flow(h: Any) -> list[tuple[str, tuple[str, int] | None]]:
    return [("work" if e.message_type == MessageType.WORK_ITEM else "review", _role(e)) for e in h.dispatched]


ROLE_STEP = {**step("s1", review=True), "role": "tester", "review_role": "security"}


def test_a_step_runs_as_its_role_and_is_accepted_only_once_both_reviews_pass(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    reviews: list[str] = []

    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.WORK_ITEM:
            return WORK_OK
        reviews.append("role" if _role(envelope) else "builtin")
        if reviews == ["builtin", "role"]:
            # The review role fails the first attempt; meanwhile the Planner rewrote the work role.
            roles.add("tester", "work", "You write the tests first, then the code.")
            return REVIEW_FAIL
        return REVIEW_PASS

    claude = planner_claude(
        plan(ROLE_STEP),
        decide("review"),
        decide("accept"),  # refused: the review role failed the step
        decide("retry", feedback="cover the injection case"),
        decide("review"),
        decide("accept", summary="done"),
    )
    h = drive(claude, outcome)

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    # The review role's review follows the built-in one with no decision between; a retry runs the new version.
    assert _flow(h) == [("work", ("tester", 1)), ("review", None), ("review", ("security", 1)),
                        ("work", ("tester", 2)), ("review", None), ("review", ("security", 1))]
    texts = [turn["text"] for turn in claude.turns()]
    assert len(texts) == 6  # plan, review, the role review's decision, its repair, review, accept
    assert "The built-in Reviewer passed step s1" in texts[2] and "review role security v1" in texts[2]
    assert "accept is not allowed: step s1 did not succeed" in texts[3]
    assert "(as role tester v1)" in texts[1] and "role: tester; review role: security" in texts[1]
    assert "Reviewer dispatched for step 1 (s1) as security v1" in activity(agent_db)
    assert any(line.startswith("Worker dispatched as tester v2:") for line in activity(agent_db))
    # Its contract says how to name roles; the steps' saved rows keep the roles and the versions they ran.
    assert "\"review_role\", a review role you defined" in claude.prompts()[0]
    saved = agent_db.store.steps["t1"]
    (row,) = saved.values()
    assert (row["role_key"], row["review_role_key"], row["role_version"], row["review_role_version"]) == (
        "tester", "security", 2, 1)


def test_the_review_role_does_not_review_a_step_the_built_in_reviewer_failed(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    claude = planner_claude(plan(ROLE_STEP), decide("review"), decide("fail", reason="the review failed"))
    h = drive(claude, lambda e: WORK_OK if e.message_type == MessageType.WORK_ITEM else REVIEW_FAIL)

    assert h.final().payload["status"] == "failed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", ("tester", 1)), ("review", None)]


@pytest.mark.parametrize(("role_review", "status"), [(REVIEW_FAIL, "failed"), (REVIEW_PASS, "completed")])
def test_without_a_decision_the_review_chains_verdict_decides(
    planner_claude: Any, agent_db: Any, roles: _Roles, role_review: dict[str, Any], status: str,
) -> None:
    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.WORK_ITEM:
            return WORK_OK
        return role_review if _role(envelope) else REVIEW_PASS

    claude = planner_claude(plan(ROLE_STEP), decide("review"), "no json", "still no json")
    h = drive(claude, outcome)

    assert h.final().payload["status"] == status  # type: ignore[union-attr]
    assert _flow(h)[-1] == ("review", ("security", 1))
    assert any(f"The Reviewer's verdict decides: {'verified' if status == 'completed' else 'failed'}" in line
               for line in activity(agent_db))


def test_a_step_that_names_an_unknown_role_gets_a_repair_turn(planner_claude: Any, agent_db: Any,
                                                             roles: _Roles) -> None:
    claude = planner_claude(plan({**step("s1"), "role": "doc"}), plan(step("s1")), decide("accept"))
    h = drive(claude, lambda e: WORK_OK)

    assert _flow(h) == [("work", None)]
    repair = claude.turns()[1]["text"]
    assert "role 'doc' is not a role of this task (defined: tester (work), security (review))" in repair


def test_where_the_project_lets_its_planner_define_no_role_a_step_naming_one_is_refused(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Roles(monkeypatch).add("tester", "work", "You write the tests.")
    claude = planner_claude(plan({**step("s1"), "role": "tester"}), plan(step("s1")), decide("accept"))
    h = drive(claude, lambda e: WORK_OK)

    assert _flow(h) == [("work", None)]
    assert "planner_roles is off" in claude.turns()[1]["text"]
    assert "review_role" not in claude.prompts()[0]


def test_a_restarted_attempt_runs_the_version_it_started_with_and_a_new_attempt_the_current_one(
    agent_db: Any, roles: _Roles,
) -> None:
    async def run() -> list[MessageEnvelope]:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(runtime_root=Path(tmp))
            sent: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.WORK_ITEM, lambda e: sent.append(e) or True, consumes=True)
            bus.subscribe_topic(MessageType.VERIFY_REQUEST, lambda e: sent.append(e) or True, consumes=True)
            planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            planner._bus = bus
            task = TaskPlan(plan_id="p1", goal_id="g1", requirement="r", work_dir=tmp, status="executing")
            work = PlanStep(step_id="step-1", index=0, instruction="do it", key="s1", role="tester", role_version=1)
            review = PlanStep(step_id="step-2", index=1, instruction="do it", key="s2", review=True,
                              review_role="security", review_role_version=1, review_stage=ReviewStage.ROLE,
                              status="completed")
            task.steps = [work, review]
            roles.add("tester", "work", "v2 of the tester")
            roles.add("security", "review", "v2 of the security review")
            await planner._dispatch_step(task, work, same_attempt=True)
            await planner._spawn_reviewer(task, review, same_attempt=True)
            await planner._dispatch_step(task, work)
            await planner._spawn_reviewer(task, review)
            await asyncio.sleep(0.05)
            await bus.shutdown()
            return sent

    sent = asyncio.run(run())
    assert [_role(e) for e in sent] == [("tester", 1), ("security", 1), ("tester", 2), ("security", 2)]
    assert sent[2].payload["task_role"]["role_prompt"] == "v2 of the tester"
