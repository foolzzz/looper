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
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, RestartMode
from looper.agents.tiers.planner import PlanStep, PlannerAgent, ReviewStage, StepReviewRole, TaskPlan
from tests.test_planner_agent import (
    REVIEW_FAIL,
    REVIEW_PASS,
    WORK_OK,
    _project,
    activity,
    decide,
    drive,
    plan,
    reply,
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


ROLE_STEP = {**step("s1", review=True), "role": "tester", "review_roles": ["security"]}


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
    assert "(as role tester v1)" in texts[1] and "role: tester; review roles: security" in texts[1]
    assert "Reviewer dispatched for step 1 (s1) as security v1" in activity(agent_db)
    assert any(line.startswith("Worker dispatched as tester v2:") for line in activity(agent_db))
    # Its contract says how to name roles; the steps' saved rows keep the roles and the versions they ran.
    assert "\"review_roles\", the review roles you defined" in claude.prompts()[0]
    saved = agent_db.store.steps["t1"]
    (row,) = saved.values()
    assert (row["role_key"], row["role_version"], row["review_roles"]) == (
        "tester", 2, [{"role": "security", "required": True, "version": 1}])


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


ADVISORY_STEP = {**ROLE_STEP, "review_roles": [{"role": "security", "required": False}]}


def _role_review(role_verdict: dict[str, Any]) -> Any:
    """Workers succeed, the built-in Reviewer passes, the review role gives ``role_verdict``."""
    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.WORK_ITEM:
            return WORK_OK
        return role_verdict if _role(envelope) else REVIEW_PASS

    return outcome


def test_an_advisory_review_role_that_fails_leaves_the_step_to_the_planner_who_may_accept_it(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    claude = planner_claude(plan(ADVISORY_STEP), decide("review"), decide("accept", summary="done"))
    h = drive(claude, _role_review(REVIEW_FAIL))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", ("tester", 1)), ("review", None), ("review", ("security", 1))]
    decision = claude.turns()[2]["text"]
    assert "advisory: its verdict does not block the step" in decision and "- passed: no" in decision
    assert "Allowed decisions now: accept, retry (2 left), replan, fail." in decision
    assert "review roles: security (advisory)" in claude.turns()[1]["text"]
    accepted = [e for e in agent_db.store.journal["t1"] if e["kind"] == "step_accepted"]
    assert "Accepted although the advisory review by role security did not pass: no tests" in accepted[0]["summary"]
    (row,) = agent_db.store.steps["t1"].values()
    assert row["review_roles"] == [{"role": "security", "required": False, "version": 1}]


@pytest.mark.parametrize(("step_spec", "status"), [(ADVISORY_STEP, "completed"), (ROLE_STEP, "failed")])
def test_without_a_decision_a_failed_review_role_blocks_only_where_it_is_required(
    planner_claude: Any, agent_db: Any, roles: _Roles, step_spec: dict[str, Any], status: str,
) -> None:
    claude = planner_claude(plan(step_spec), decide("review"), "no json", "still no json")
    h = drive(claude, _role_review(REVIEW_FAIL))

    assert h.final().payload["status"] == status  # type: ignore[union-attr]


def test_an_advisory_review_role_does_not_spare_the_step_the_built_in_reviewer(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    claude = planner_claude(plan(ADVISORY_STEP), decide("review"), decide("accept"), decide("fail", reason="no"))
    h = drive(claude, lambda e: WORK_OK if e.message_type == MessageType.WORK_ITEM else REVIEW_FAIL)

    assert h.final().payload["status"] == "failed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", ("tester", 1)), ("review", None)]
    assert "accept is not allowed" in claude.turns()[3]["text"]


TWO_REVIEWS = {**step("s1", review=True), "review_roles": ["security", "style"]}


def _reviews_by(verdicts: dict[str | None, dict[str, Any]]) -> Any:
    """Workers succeed; each review gives the verdict of its role (None: the built-in Reviewer)."""
    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.WORK_ITEM:
            return WORK_OK
        role = _role(envelope)
        return verdicts[role[0] if role else None]

    return outcome


def test_review_roles_review_one_after_another_and_the_planner_decides_once_all_passed(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    roles.add("style", "review", "You check the style.")
    claude = planner_claude(plan(TWO_REVIEWS), decide("review"), decide("accept", summary="done"))
    h = drive(claude, _reviews_by({None: REVIEW_PASS, "security": REVIEW_PASS, "style": REVIEW_PASS}))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", None), ("review", None), ("review", ("security", 1)), ("review", ("style", 1))]
    texts = [turn["text"] for turn in claude.turns()]
    assert len(texts) == 3  # plan, review, then one decision after the three reviews
    assert "review roles: security, style" in texts[1]
    decision = texts[2]
    assert decision.index("The built-in Reviewer passed step s1") < decision.index(
        "Review of step s1 by review role security v1 (it must pass):") < decision.index(
        "Review of step s1 by review role style v1 (it must pass):")
    assert "Allowed decisions now: accept," in decision
    (row,) = agent_db.store.steps["t1"].values()
    assert row["review_roles"] == [{"role": "security", "required": True, "version": 1},
                                   {"role": "style", "required": True, "version": 1}]
    assert [(r["role"], r["passed"]) for r in row["result"]["role_reviews"]] == [("security", True), ("style", True)]


def test_a_required_review_role_that_does_not_pass_stops_the_review_roles_after_it(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    roles.add("style", "review", "You check the style.")
    claude = planner_claude(plan(TWO_REVIEWS), decide("review"), decide("fail", reason="insecure"))
    h = drive(claude, _reviews_by({None: REVIEW_PASS, "security": REVIEW_FAIL, "style": REVIEW_PASS}))

    assert h.final().payload["status"] == "failed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", None), ("review", None), ("review", ("security", 1))]
    decision = claude.turns()[2]["text"]
    assert "Its 1 other review role(s) did not review it: a required one failed first." in decision
    assert "Allowed decisions now: retry" in decision and "accept," not in decision.split("Allowed decisions now:")[1]


ADVISORY_FIRST = {**step("s1", review=True), "review_roles": [{"role": "security", "required": False}, "style"]}


def test_an_advisory_review_role_that_does_not_pass_lets_the_next_one_review(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    roles.add("style", "review", "You check the style.")
    claude = planner_claude(plan(ADVISORY_FIRST), decide("review"), decide("accept", summary="done"))
    h = drive(claude, _reviews_by({None: REVIEW_PASS, "security": REVIEW_FAIL, "style": REVIEW_PASS}))

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", None), ("review", None), ("review", ("security", 1)), ("review", ("style", 1))]
    decision = claude.turns()[2]["text"]
    assert "Review of step s1 by review role security v1 (advisory: its verdict does not block the step" in decision
    assert "Allowed decisions now: accept," in decision
    accepted = [e for e in agent_db.store.journal["t1"] if e["kind"] == "step_accepted"]
    assert "Accepted although the advisory review by role security did not pass" in accepted[0]["summary"]


ADVISORY_LAST = {**step("s1", review=True), "review_roles": ["security", {"role": "style", "required": False}]}


@pytest.mark.parametrize(("spec", "security", "style", "status"), [
    (ADVISORY_FIRST, REVIEW_FAIL, REVIEW_FAIL, "failed"),
    (ADVISORY_FIRST, REVIEW_FAIL, REVIEW_PASS, "completed"),
    # The last verdict alone does not decide: an advisory one that fails after a required one passed accepts.
    (ADVISORY_LAST, REVIEW_PASS, REVIEW_FAIL, "completed"),
])
def test_without_a_decision_every_required_review_role_must_have_passed(
    planner_claude: Any, agent_db: Any, roles: _Roles, spec: dict[str, Any], security: dict[str, Any],
    style: dict[str, Any], status: str,
) -> None:
    roles.add("style", "review", "You check the style.")
    claude = planner_claude(plan(spec), decide("review"), "no json", "still no json")
    h = drive(claude, _reviews_by({None: REVIEW_PASS, "security": security, "style": style}))

    assert h.final().payload["status"] == status  # type: ignore[union-attr]
    assert _flow(h)[-1] == ("review", ("style", 1))


def test_a_review_role_that_cannot_be_read_fails_its_review_and_the_chain_goes_on_as_for_any_verdict(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    # gone is named while it exists, then its versions are lost before its review starts.
    roles.add("gone", "review", "You check something.")
    spec = {**step("s1", review=True), "review_roles": [{"role": "gone", "required": False}, "security"]}
    claude = planner_claude(plan(spec), decide("review"), decide("accept", summary="done"))

    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.WORK_ITEM:
            roles.rows = [r for r in roles.rows if r["role_key"] != "gone"]
            return WORK_OK
        return REVIEW_PASS

    h = drive(claude, outcome)

    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
    assert _flow(h) == [("work", None), ("review", None), ("review", ("security", 1))]
    assert "Cannot read role gone, which reviews step s1" in claude.turns()[2]["text"]


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
    assert "review_roles" not in claude.prompts()[0]


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
                              review_roles=[StepReviewRole("security", version=1)], review_stage=ReviewStage.ROLE,
                              review_position=0, status="completed")
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


def test_a_restart_in_the_middle_of_the_review_roles_runs_the_one_the_step_waits_for(
    agent_db: Any, roles: _Roles,
) -> None:
    async def run() -> list[MessageEnvelope]:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(runtime_root=Path(tmp))
            sent: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.VERIFY_REQUEST, lambda e: sent.append(e) or True, consumes=True)
            planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            planner._bus = bus
            task = TaskPlan(plan_id="p1", goal_id="g1", requirement="r", work_dir=tmp, status="executing")
            roles.add("style", "review", "v1 of the style review")
            review = PlanStep(step_id="step-1", index=0, instruction="do it", key="s1", review=True,
                              review_roles=[StepReviewRole("security", version=1), StepReviewRole("style", version=1)],
                              review_stage=ReviewStage.ROLE, review_position=1, status="completed")
            task.steps = [review]
            roles.add("style", "review", "v2 of the style review")
            await planner._spawn_reviewer(task, review, same_attempt=True)
            await asyncio.sleep(0.05)
            await bus.shutdown()
            return sent

    sent = asyncio.run(run())
    assert [_role(e) for e in sent] == [("style", 1)]


def test_a_planner_started_again_reads_its_steps_roles_back(agent_db: Any) -> None:
    async def run() -> PlanStep:
        with tempfile.TemporaryDirectory() as tmp:
            first = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            task = TaskPlan(plan_id="p1", goal_id="g1", requirement="r", work_dir=tmp, status="executing")
            task.steps = [PlanStep(step_id="step-1", index=0, instruction="do it", key="s1", review=True, role="tester",
                                   review_roles=[StepReviewRole("security", version=3),
                                                 StepReviewRole("style", required=False, version=1)],
                                   role_version=2, review_stage=ReviewStage.ROLE, review_position=1,
                                   status="completed")]
            await first._save(task, *task.steps, plan_row=True)
            again = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            await again._load_plan()
            assert again._plan is not None
            return again._plan.steps[0]

    loaded = asyncio.run(run())
    assert (loaded.role, loaded.review_roles, loaded.role_version, loaded.review_stage, loaded.review_position) == (
        "tester", [StepReviewRole("security", True, 3), StepReviewRole("style", False, 1)], 2, ReviewStage.ROLE, 1)


TEMPLATE_LINES = ("This project's role templates (when one fits a step, define its role from it with "
                  "looper_task_role_set and template):\n- security (review): steps that change authentication\n"
                  "- tester (work)")


def _templates(agent_db: Any) -> None:
    agent_db.list_templates.return_value = [
        {"template_key": "security", "kind": "review", "description": "steps that change authentication"},
        {"template_key": "tester", "kind": "work", "description": ""}]


def test_the_planning_and_restart_turns_list_the_projects_role_templates(
    planner_claude: Any, agent_db: Any, roles: _Roles,
) -> None:
    _templates(agent_db)
    claude = planner_claude(plan(step("s1")), reply({"reply": "Going on.", "action": "none"}), decide("accept"))
    held: list[MessageEnvelope] = []

    def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
        held.append(envelope)
        return None if len(held) == 1 else WORK_OK  # the first attempt runs until the task stops

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held)
        await h.planner.stop()
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_REQUEST,
            payload={"requirement": "build the calc package", "goal_id": "g1", "work_dir": str(h.work_dir),
                     "project_id": "proj-1", "restart": RestartMode.CONTINUE.value, "ended": "stopped"},
            correlation_id="corr-1"))
        await h.finished()

    h = drive(claude, outcome, scenario)

    texts = [turn["text"] for turn in claude.turns()]
    assert texts[0].startswith("[PLAN]") and texts[1].startswith("[RESTART]")
    assert TEMPLATE_LINES in texts[0] and TEMPLATE_LINES in texts[1]
    assert TEMPLATE_LINES not in texts[2]  # a decision turn plans nothing
    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]


def test_where_the_project_lets_its_planner_define_no_role_no_turn_lists_its_templates(
    planner_claude: Any, agent_db: Any,
) -> None:
    _templates(agent_db)
    claude = planner_claude(plan(step("s1")), decide("accept"))
    drive(claude, lambda e: WORK_OK)

    assert "role templates" not in claude.turns()[0]["text"]
