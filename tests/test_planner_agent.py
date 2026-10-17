"""A task's Planner is a resident Claude Code agent that plans the steps and decides on every result.

The Planner's Claude is a stand-in (``planner_claude`` in conftest) that
answers each turn with a scripted reply; Workers and Reviewers are a stand-in
pool on the bus that answers each dispatch with a scripted result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_fixtures import profile_json
from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN, plugin_dirs

from looper.agents.agent_settings import DEFAULT_HARNESS_PROMPT
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ISOLATION_ID, isolation_args
from looper.agents.run_usage import Usage
from looper.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
)
from looper.agents.tiers.planner import PlannerAgent
from looper.agents.tiers.planner_protocol import REPLY_CONTRACT
from looper.agents.tiers.task import TaskRouter

WEB = channel_address("web")

WORK_OK = {"status": "completed", "summary": "did it", "changed_files": {"calc.py": "added"}}
WORK_FAILED = {"status": "failed", "error": "tests fail", "summary": "tests fail"}
REVIEW_PASS = {"passed": True, "findings": [], "summary": "looks right"}
REVIEW_FAIL = {"passed": False, "findings": ["no tests"], "summary": "missing tests"}

Outcome = Callable[[MessageEnvelope], "dict[str, Any] | None"]


# --- Scripted Planner replies ---


def reply(data: dict[str, Any]) -> str:
    return "Here is my answer.\n\n```json\n" + json.dumps(data) + "\n```"


def step(step_id: str, *depends_on: str, review: bool = False) -> dict[str, Any]:
    return {
        "id": step_id,
        "instruction": f"do {step_id}",
        "acceptance_criteria": [f"{step_id} works"],
        "depends_on": list(depends_on),
        "review": review,
    }


def plan(*steps: dict[str, Any]) -> str:
    return reply({"steps": list(steps)})


# The final summary every scripted accept carries (code keeps it only on the decision that completes the task),
# and how the task's result shows it.
FINAL = {"delivered": "calc.py with add and its tests", "how_to_use": "import calc; calc.add(1, 2)",
         "verification": "pytest: 3 passed", "open_issues": "none"}
FINAL_RESULT = ("## Delivered\ncalc.py with add and its tests\n\n## How to use\nimport calc; calc.add(1, 2)\n\n"
                "## Verification\npytest: 3 passed\n\n## Open issues\nnone")


def decide(decision: str, **fields: Any) -> str:
    if decision == "accept":
        fields.setdefault("final", FINAL)
    return reply({"decision": decision, **{k: v for k, v in fields.items() if v is not None}})


def chat(answer: str, action: str = "none", **fields: Any) -> str:
    return reply({"reply": answer, "action": action, **fields})


# --- A Planner on a bus with a stand-in worker pool ---


class Harness:
    """One Planner (task ``t1``) on a bus whose worker pool is ``outcome``.

    ``outcome(envelope)`` gives a dispatched WORK_ITEM's or VERIFY_REQUEST's
    result payload; None holds it (the Worker keeps running) until
    ``release``. A held Worker takes forwarded user turns.
    """

    def __init__(self, root: Path, claude: Any, outcome: Outcome) -> None:
        self.bus = MessageBus(runtime_root=root)
        self.claude = claude
        self.work_dir = root / "work"
        self.outcome = outcome
        self.dispatched: list[MessageEnvelope] = []
        self.dispatched_at_turn: list[int] = []  # the Planner turns run when each was dispatched
        self.held: list[MessageEnvelope] = []
        self.forwarded: list[MessageEnvelope] = []
        self.killed: list[MessageEnvelope] = []
        self.statuses: list[MessageEnvelope] = []
        self.replies: list[MessageEnvelope] = []
        self.bus.subscribe_topic(MessageType.WORK_ITEM, self._pool, consumes=True)
        self.bus.subscribe_topic(MessageType.VERIFY_REQUEST, self._pool, consumes=True)
        self.bus.subscribe_topic(MessageType.WORKER_INPUT, self._worker_input, consumes=True)
        self.bus.subscribe_topic(MessageType.KILL_WORK, self._kill, consumes=True)
        self.bus.subscribe_topic(MessageType.GOAL_STATUS, self.statuses.append)
        self.bus.subscribe_topic(MessageType.TASK_ACCEPTED, self.statuses.append)
        self.bus.subscribe_topic(MessageType.AGENT_REPLY, self.replies.append)
        self.planner = PlannerAgent(task_id="t1", runtime_root=root, claude_bin=claude.bin)

    def _pool(self, envelope: MessageEnvelope) -> bool:
        self.dispatched.append(envelope)
        self.dispatched_at_turn.append(len(self.claude.turns()))
        result = self.outcome(envelope)
        if result is None:
            self.held.append(envelope)
        else:
            asyncio.get_event_loop().create_task(self.answer(envelope, result))
        return True

    async def answer(self, envelope: MessageEnvelope, result: dict[str, Any]) -> None:
        verify = envelope.message_type == MessageType.VERIFY_REQUEST
        await self.bus.publish(MessageEnvelope.create(
            source=envelope.target,
            target=envelope.source,
            message_type=MessageType.VERIFY_RESULT if verify else MessageType.WORK_RESULT,
            payload={"task_id": envelope.payload["task_id"], "plan_id": envelope.payload["plan_id"], **result},
            correlation_id=envelope.correlation_id,
        ))

    async def release(self, result: dict[str, Any]) -> None:
        """Finish the oldest held Worker with ``result``."""
        await self.answer(self.held.pop(0), result)

    def _worker_input(self, envelope: MessageEnvelope) -> bool:
        self.forwarded.append(envelope)
        delivered = any(h.target == envelope.target for h in self.held)
        asyncio.get_event_loop().create_task(self.bus.publish(MessageEnvelope.create(
            source=envelope.target, target=envelope.source,
            message_type=MessageType.WORKER_INPUT_RESULT,
            payload={"delivered": delivered}, reply_to=envelope.message_id,
        )))
        return True

    def _kill(self, envelope: MessageEnvelope) -> bool:
        """Stop a held Worker: a killed Worker publishes no result (like the real pool)."""
        self.killed.append(envelope)
        held = next((h for h in self.held if h.target == envelope.target), None)
        if held is not None:
            self.held.remove(held)
        asyncio.get_event_loop().create_task(self.bus.publish(MessageEnvelope.create(
            source=envelope.target, target=envelope.source,
            message_type=MessageType.KILL_WORK_RESULT,
            payload={"killed": held is not None}, reply_to=envelope.message_id,
        )))
        return True

    async def start(self, requirement: str = "build the calc package") -> None:
        await self.planner.start(self.bus)
        await self.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=self.planner.agent_address,
            message_type=MessageType.TASK_REQUEST,
            payload={"goal_id": "g1", "requirement": requirement, "work_dir": str(self.work_dir),
                     "project_id": "proj-1"},
            correlation_id="corr-1",
            metadata={"origin_channel_id": "channel-web"},
        ))

    @property
    def work_items(self) -> list[MessageEnvelope]:
        return [e for e in self.dispatched if e.message_type == MessageType.WORK_ITEM]

    @property
    def reviews(self) -> list[MessageEnvelope]:
        return [e for e in self.dispatched if e.message_type == MessageType.VERIFY_REQUEST]

    def final(self) -> MessageEnvelope | None:
        return next(
            (e for e in self.statuses if e.message_type == MessageType.GOAL_STATUS
             and e.payload["status"] in ("completed", "failed")),
            None,
        )

    async def until(self, condition: Callable[[], Any], timeout: float = 15.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while not condition():
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("timed out waiting for the Planner")
            await asyncio.sleep(0.01)

    async def finished(self) -> MessageEnvelope:
        await self.until(self.final)
        final = self.final()
        assert final is not None
        return final

    async def say(self, text: str) -> str:
        """Send a chat message to the Planner and return its reply."""
        before = len(self.replies)
        await self.bus.publish(MessageEnvelope.create(
            source=WEB, target=planner_address("t1"), message_type=MessageType.USER_MESSAGE,
            payload={"text": text}, correlation_id="corr-chat",
            metadata={"origin_channel_id": WEB.agent_id},
        ))
        await self.until(lambda: len(self.replies) > before)
        return self.replies[-1].payload["text"]


def drive(
    claude: Any,
    outcome: Outcome,
    scenario: Callable[[Harness], Awaitable[Any]] | None = None,
) -> Harness:
    """Run ``scenario`` (default: start the task and wait until it ends), then stop the Planner."""

    async def _default(h: Harness) -> None:
        await h.start()
        await h.finished()

    async def _run() -> Harness:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), claude, outcome)
            try:
                await asyncio.wait_for((scenario or _default)(h), 30)
            finally:
                await h.planner.stop()
                await h.bus.shutdown()
            return h

    return asyncio.run(_run())


def always(work: dict[str, Any], review: dict[str, Any] | None = None) -> Outcome:
    def outcome(envelope: MessageEnvelope) -> dict[str, Any]:
        if envelope.message_type == MessageType.VERIFY_REQUEST:
            assert review is not None, "no Reviewer expected"
            return review
        return work

    return outcome


def hold_first(then: dict[str, Any] = WORK_OK) -> Outcome:
    """Keep the first dispatched Worker running; answer the later ones with ``then``."""
    seen: list[MessageEnvelope] = []

    def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
        seen.append(envelope)
        return None if len(seen) == 1 else then

    return outcome


def activity(agent_db: Any) -> list[str]:
    return [str(call.kwargs.get("summary")) for call in agent_db.insert_activity.await_args_list]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _assert_unrestricted(argv: list[str]) -> None:
    """The shared permission flags, once, and nothing that narrows the tools or the permissions again."""
    from looper.agents.claude_cli import permission_args

    flags = permission_args()
    assert any(argv[i:i + len(flags)] == flags for i in range(len(argv)))
    assert argv.count("--dangerously-skip-permissions") == 1
    assert not {"--tools", "--allowedTools", "--disallowedTools", "--permission-mode"} & set(argv)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _in_order(text: str, *parts: str) -> None:
    """Every part is in ``text``, in this order."""
    at = [text.index(part) for part in parts]
    assert at == sorted(at), f"out of order: {parts}"


# --- Planning ---


@pytest.mark.usefixtures("agent_db")
class TestPlanning:
    def test_steps_run_in_dependency_order(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "description": "a tiny calculator",
                                                  "work_dir": "/srv/calc", "background": ""}
        claude = planner_claude(
            plan(step("s1"), step("s2"), step("s3", "s1", "s2")),
            decide("accept"), decide("accept"), decide("accept", summary="calc is built and tested"),
        )
        h = drive(claude, always(WORK_OK))

        final = h.final()
        assert final is not None and final.payload["status"] == "completed"
        assert final.payload["summary"] == FINAL_RESULT  # the final summary of the accept that completed it
        assert [item.payload["instruction"] for item in h.work_items] == ["do s1", "do s2", "do s3"]
        # s1 and s2 start together after the plan turn; s3 waits for both decisions.
        assert h.dispatched_at_turn == [1, 1, 3]
        assert h.work_items[2].payload["acceptance_criteria"] == ["s3 works"]
        assert h.work_items[0].payload["context"] == {"work_dir": str(h.work_dir)}
        # A step gets what the steps it depends on reported (their Workers' words, as data).
        assert h.work_items[0].payload["dependencies"] == []
        assert h.work_items[2].payload["dependencies"] == [
            {"step": "s1", "summary": "did it", "changed_files": ["calc.py"]},
            {"step": "s2", "summary": "did it", "changed_files": ["calc.py"]},
        ]

        accepted = next(e for e in h.statuses if e.message_type == MessageType.TASK_ACCEPTED)
        assert accepted.payload["step_count"] == 3
        # The plan is saved with all its steps at once; each step is saved again as it changes.
        created = next(call.kwargs for call in agent_db.save_plan.await_args_list if call.kwargs["steps"])
        assert created["plan"]["status"] == "executing"
        assert [row["instruction"] for row in created["steps"]] == ["do s1", "do s2", "do s3"]
        assert [s["status"] for s in agent_db.store.steps["t1"].values()] == ["verified"] * 3
        phases = [call.args[1] for call in agent_db.update_task_phase.await_args_list]
        assert phases == ["planning", "executing", "completed"]

        first, *decisions = [turn["text"] for turn in claude.turns()]
        assert first.startswith("[PLAN]")
        assert "without changing it" in first and "You may read the project first" not in first
        assert all(text.startswith("[DECISION]") for text in decisions)
        # The task is the system prompt's task section; the turns carry only what changes.
        (prompt,) = claude.prompts()
        task = prompt[prompt.index("## Task"):]
        for part in ("t1", "build the calc package", "calc", "a tiny calculator", str(h.work_dir)):
            assert part in task
        for text in (first, *decisions):
            assert "build the calc package" not in text and str(h.work_dir) not in text
        assert "- s3 [pending] do s3 (depends on: s1, s2; review: no; retries: 0)" in decisions[0]
        assert any("Plan created: 3 step(s): s1, s2, s3 (after s1+s2)" in line for line in activity(agent_db))

    def test_invalid_plan_gets_one_repair_turn(self, planner_claude: Any) -> None:
        claude = planner_claude("I would split it in two.", plan(step("s1")), decide("accept"))
        h = drive(claude, always(WORK_OK))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        repair = claude.turns()[1]["text"]
        assert repair.startswith("[PLAN] Your last reply broke the reply contract: the reply has no ```json")

    def test_second_invalid_plan_fails_the_task(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude("no plan", plan(step("s1"), step("s2", "s9")))
        h = drive(claude, always(WORK_OK))

        final = h.final()
        assert final is not None and final.payload["status"] == "failed"
        assert "Planning failed: invalid reply after a repair turn" in final.payload["summary"]
        assert "unknown step 's9'" in final.payload["summary"]
        assert h.dispatched == []
        assert not any(e.message_type == MessageType.TASK_ACCEPTED for e in h.statuses)
        assert agent_db.update_task_phase.await_args_list[-1].args == ("t1", "failed")
        assert any(line.startswith("Task failed: Planning failed") for line in activity(agent_db))
        assert len(claude.turns()) == 2

    def test_turn_over_the_time_limit_fails_planning(
        self, planner_claude: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from looper.db import role_configs

        async def _config(agent_id: str) -> dict[str, Any]:
            assert agent_id == "planner"
            return {"system_prompt": "You plan.", "timeout_seconds": 1, "max_turns": 0}

        monkeypatch.setattr(role_configs, "get_config", _config)
        claude = planner_claude("<hang>")
        h = drive(claude, always(WORK_OK))

        assert "longer than the 1 s limit (timeout_seconds)" in h.final().payload["summary"]  # type: ignore[union-attr]
        # The stand-in was killed (one killed before it logged its pid is not listed at all).
        assert not any(_alive(pid) for pid in claude.pids())


# --- Decisions on results ---


@pytest.mark.usefixtures("agent_db")
class TestDecisions:
    def test_review_flagged_step_completes_only_after_the_review_passes(self, planner_claude: Any) -> None:
        claude = planner_claude(
            plan(step("s1", review=True)),
            decide("accept"),  # refused: the step needs a passing review first
            decide("review", reason="needs a second look"),
            decide("accept", summary="reviewed and done"),
        )
        h = drive(claude, always(WORK_OK, REVIEW_PASS))

        assert [e.message_type for e in h.dispatched] == [MessageType.WORK_ITEM, MessageType.VERIFY_REQUEST]
        (review,) = h.reviews
        assert review.payload["parent_task_id"] == "t1"
        assert review.payload["context"] == {"work_dir": str(h.work_dir)}
        assert review.payload["acceptance_criteria"] == ["s1 works"]
        assert review.payload["work_result"]["changed_files"] == {"calc.py": "added"}
        assert (review.payload["requirement"], review.payload["step_key"]) == ("build the calc package", "s1")
        texts = [turn["text"] for turn in claude.turns()]
        assert "Allowed decisions now: review, retry (2 left), replan, fail." in texts[1]
        assert "needs a passing review before it can be accepted" in texts[2]
        assert texts[3].startswith("[DECISION]") and "Review of step s1" in texts[3]
        final = h.final()
        assert final is not None and final.payload["status"] == "completed"
        assert final.payload["summary"] == FINAL_RESULT

    def test_failed_review_cannot_be_accepted(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))
        h = drive(claude, always(WORK_OK, REVIEW_FAIL))

        # accept is refused twice, so the Reviewer's verdict decides.
        final = h.final()
        assert final is not None and final.payload["status"] == "failed"
        assert "review failed: no tests" in final.payload["summary"]
        assert any("Reviewer's verdict decides: failed" in line for line in activity(agent_db))

    def test_retry_runs_a_new_worker_with_the_feedback_and_is_capped(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(
            plan(step("s1")),
            decide("retry", feedback="fix the import"),
            decide("retry", feedback="add the sub function"),
            decide("retry", feedback="try once more"),  # refused: 2 retries used
        )
        h = drive(claude, always(WORK_FAILED))

        workers = [item.target.agent_id for item in h.work_items]
        assert len(workers) == 3 and len(set(workers)) == 3
        assert [item.payload["instruction"] for item in h.work_items] == ["do s1"] * 3
        assert [item.payload["attempt"] for item in h.work_items] == [1, 2, 3]
        assert [item.payload["feedback"] for item in h.work_items] == [
            [], ["fix the import"], ["fix the import", "add the sub function"]]
        # Each Worker also gets the task and its step's label.
        assert {item.payload["requirement"] for item in h.work_items} == {"build the calc package"}
        assert {item.payload["step_key"] for item in h.work_items} == {"s1"}
        repair = claude.turns()[4]["text"]
        assert "retry is not allowed: step s1 has used its 2 retries; allowed now: replan, fail" in repair
        final = h.final()
        assert final is not None and final.payload["status"] == "failed"
        assert any("Worker's own verdict decides: failed" in line for line in activity(agent_db))

    @pytest.mark.parametrize("retries", [0, 5])
    def test_a_project_sets_how_many_retries_a_step_has(
        self, planner_claude: Any, agent_db: Any, retries: int,
    ) -> None:
        agent_db.get_task_project.return_value = {**_project(), "max_step_retries": retries}
        claude = planner_claude(
            plan(step("s1")),
            *(decide("retry", feedback=f"try {n}") for n in range(1, retries + 1)),
            decide("retry", feedback="once more"),  # refused: every retry used
        )
        h = drive(claude, always(WORK_FAILED))

        assert [item.payload["attempt"] for item in h.work_items] == list(range(1, retries + 2))
        texts = [turn["text"] for turn in claude.turns()]
        allowed = texts[1].split("Allowed decisions now: ")[1].split("\n")[0]
        assert (f"retry ({retries} left)" in allowed) if retries else ("retry" not in allowed)
        assert f"retry is not allowed: step s1 has used its {retries} retries" in texts[-1]
        assert f"At most {retries} retries per step." in claude.prompts()[0]
        assert h.final().payload["status"] == "failed"  # type: ignore[union-attr]

    def test_replan_replaces_a_failed_step(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),
            decide("replan", steps=[step("s1b"), step("s2", "s1b")]),
            decide("accept"),
            decide("accept"),
        )
        outcomes = iter([WORK_FAILED, WORK_OK, WORK_OK])
        h = drive(claude, lambda envelope: next(outcomes))

        assert [item.payload["instruction"] for item in h.work_items] == ["do s1", "do s1b", "do s2"]
        plan_steps = h.planner._plan.steps  # type: ignore[union-attr]
        assert [(s.label, s.status) for s in plan_steps] == [
            ("s1", "skipped"), ("s2", "verified"), ("s1b", "verified"),
        ]
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        # The decided step and the replan's steps (the new s1b and the replaced, not yet started s2) are
        # saved together.
        replanned = [call.kwargs["steps"] for call in agent_db.save_plan.await_args_list
                     if len(call.kwargs["steps"]) > 1][-1]
        assert [(row["key"], row["status"]) for row in replanned] == [
            ("s1", "skipped"), ("s1b", "pending"), ("s2", "pending"),
        ]
        assert any("Plan changed: s1b, s2 (after s1b)" in line for line in activity(agent_db))

    def test_fail_decision_fails_the_task_with_its_reason(self, planner_claude: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("fail", reason="the API does not exist"))
        h = drive(claude, always(WORK_FAILED))

        final = h.final()
        assert final is not None and final.payload["status"] == "failed"
        assert "the API does not exist" in final.payload["summary"]

    @pytest.mark.parametrize(
        ("replies", "work", "status", "turns"),
        [
            (("<error>",), WORK_OK, "completed", 2),  # Claude failed: no repair turn
            (("not json", "still not json"), WORK_OK, "completed", 3),  # invalid twice
            (("not json", "still not json"), WORK_FAILED, "failed", 3),
        ],
    )
    def test_failed_decision_falls_back_to_the_workers_verdict(
        self, planner_claude: Any, agent_db: Any,
        replies: tuple[str, ...], work: dict[str, Any], status: str, turns: int,
    ) -> None:
        claude = planner_claude(plan(step("s1", review=True)), *replies)
        h = drive(claude, always(work))

        assert h.final().payload["status"] == status  # type: ignore[union-attr]
        assert h.reviews == []  # the fallback spawns no Reviewer, even for a step flagged for review
        assert len(claude.turns()) == turns
        assert any(
            line.startswith("Warning: the Planner could not decide on step 1 (s1)")
            and f"Worker's own verdict decides: {'verified' if status == 'completed' else 'failed'}" in line
            for line in activity(agent_db)
        )


# --- The resident Claude ---


@pytest.mark.usefixtures("agent_db")
class TestResidentClaude:
    def test_claude_that_dies_is_restarted_with_its_session(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), "<exit>", decide("accept"))
        h = drive(claude, always(WORK_OK))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        first, second = claude.argvs()
        assert _flag(first, "--resume") is None
        assert _flag(second, "--resume") == "sess-p"
        assert len(set(claude.pids())) == 2
        # The turn that Claude died in fell back to the Worker's verdict.
        assert any("exited before finishing the turn (exit code 3)" in line for line in activity(agent_db))
        assert h.planner._plan.session_id == "sess-p"  # type: ignore[union-attr]
        # The resumed process reports the session's total (0.5); its turn spent 0.25 of it.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, None, 0.25]
        assert h.planner._plan.session_totals == Usage(cost_usd=0.5)  # type: ignore[union-attr]

    @pytest.mark.parametrize(("ends", "status", "cost"), [
        ("claude_killed", "failed", None), ("stopped_mid_turn", "interrupted", None),
        ("stopped_while_saving", "completed", 0.25),
    ])
    def test_a_turn_ends_its_record_however_the_turn_ends(
        self, planner_claude: Any, agent_db: Any, ends: str, status: str, cost: float | None,
    ) -> None:
        """A turn whose Claude is killed mid-turn ends ``failed``; one its task's stop cuts off mid-turn ends
        ``interrupted``; one stopped after Claude answered, while the Planner saves its session, is recorded
        already, with its cost. No record is left ``running``."""
        import signal

        claude = planner_claude(plan(step("s1")) if ends == "stopped_while_saving" else "<hang>")
        saving = asyncio.Event()
        save = agent_db.save_session.side_effect

        async def save_session(agent_id: str, session_id: Any, totals: dict[str, Any], **fields: Any) -> None:
            if totals.get("cost_usd"):  # the save after the turn's result
                saving.set()
                await asyncio.sleep(60)
            await save(agent_id, session_id, totals, **fields)

        agent_db.save_session.side_effect = save_session

        async def scenario(h: Harness) -> None:
            await h.start()
            if ends == "claude_killed":
                await h.until(lambda: claude.turns())
                os.kill(claude.pids()[0], signal.SIGKILL)
                await h.until(lambda: agent_db.finish_record.await_count)
            elif ends == "stopped_mid_turn":
                await h.until(lambda: claude.turns())  # then the task stops
            else:
                await h.until(saving.is_set)  # then the task stops

        drive(claude, lambda envelope: None, scenario)
        finishes = agent_db.finish_record.await_args_list
        assert agent_db.start_record.await_count == len(finishes)
        assert (finishes[0].kwargs["status"], finishes[0].kwargs["cost_usd"]) == (status, cost)
        if ends == "claude_killed":
            assert "exit code -9" in finishes[0].kwargs["error"]

    def test_a_session_that_no_longer_exists_is_dropped_and_the_turn_retried(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        # Claude dies after the plan; its session is then gone, so resuming it fails at once.
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), "<exit>", decide("accept"), forget_sessions=True)
        h = drive(claude, always(WORK_OK))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        first, resumed, fresh = claude.argvs()
        assert _flag(first, "--resume") is None
        assert _flag(resumed, "--resume") == "sess-p"
        assert _flag(fresh, "--resume") is None  # the same turn, in a new session
        assert any("session sess-p could not be resumed" in line for line in activity(agent_db))
        # The new session starts from zero: its turn spent all of the 0.25 it reports.
        costs = [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list]
        assert costs == [0.25, None, None, 0.25]
        assert h.planner._plan.session_totals == Usage(cost_usd=0.25)  # type: ignore[union-attr]

    def test_after_a_server_restart_the_first_turn_records_only_its_own_cost(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1")), chat("still here"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)  # planned (0.25); the Worker keeps running
            # The server stops; the database keeps the plan, the session and its running total.
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            await h.planner.stop()
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            assert await h.say("are you there?") == "still here"

        h = drive(claude, hold_first(), scenario)
        first, resumed = claude.argvs()
        assert (_flag(first, "--resume"), _flag(resumed, "--resume")) == (None, "sess-p")
        # The resumed Claude reports 0.5, the session's total; the chat turn spent 0.25.
        assert [call.kwargs["cost_usd"] for call in agent_db.finish_record.await_args_list] == [0.25, 0.25]
        assert h.planner._plan.session_totals == Usage(cost_usd=0.5)  # type: ignore[union-attr]

    @pytest.mark.parametrize("first_reply", ["<hang>", "plan"])
    def test_stop_ends_and_reaps_claude(self, planner_claude: Any, first_reply: str) -> None:
        """stop() ends Claude whether it is in the middle of a turn or idle between turns."""
        claude = planner_claude("<hang>" if first_reply == "<hang>" else plan(step("s1")))

        async def scenario(h: Harness) -> None:
            await h.start()
            if first_reply == "<hang>":
                await h.until(lambda: claude.turns())
            else:
                await h.until(lambda: h.held)  # the Worker runs; Claude idles

        h = drive(claude, lambda envelope: None, scenario)
        (pid,) = claude.pids()
        assert not _alive(pid)
        assert h.final() is None

    def test_cancelled_stop_is_not_swallowed(self) -> None:
        """stop() waits for the message loop to end; being cancelled meanwhile must still cancel it."""

        async def _run() -> asyncio.Task[None]:
            with tempfile.TemporaryDirectory() as tmp:
                planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))

                async def loop_ending_slowly() -> None:
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        await asyncio.sleep(0.3)  # like a turn that is killing its Claude
                        raise

                planner._loop_task = asyncio.create_task(loop_ending_slowly())
                await asyncio.sleep(0)
                stopper = asyncio.create_task(planner.stop())
                await asyncio.sleep(0.05)
                stopper.cancel()
                await asyncio.wait([stopper])
                return stopper

        assert asyncio.run(_run()).cancelled()

    def test_a_stopping_planners_token_files_are_gone_once_its_token_no_longer_works(self) -> None:
        """The MCP configuration that carries the Planner's token is deleted with the token, before stop() waits for
        its Claude to end: a stop the server's shutdown cuts off then leaves no token on disk."""
        from types import SimpleNamespace

        from looper.agents.ops.planner import PlannerOps

        async def _run() -> tuple[bool, bool, bool]:
            with tempfile.TemporaryDirectory() as tmp:
                ops = PlannerOps(SimpleNamespace(bus=None, task_router=None, worker_pool=None), url="http://x")
                planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp), ops=ops)
                planner._token = token = ops.issue("t1")
                config = Path(tmp) / "mcp.json"
                config.write_text(token)
                planner._token_files.add(str(config))
                exiting = asyncio.Event()

                async def claude_exiting_slowly(*, kill: bool = False) -> None:
                    exiting.set()
                    await asyncio.sleep(60)  # a Claude that takes its time to exit

                planner._claude.stop = claude_exiting_slowly  # type: ignore[method-assign]
                stopper = asyncio.create_task(planner.stop())
                await exiting.wait()
                revoked, gone_while_waiting = not ops.authorized(f"Bearer {token}"), not config.exists()
                stopper.cancel()  # the server shuts down meanwhile
                await asyncio.wait([stopper])
                return revoked, gone_while_waiting, not config.exists()

        assert asyncio.run(_run()) == (True, True, True)

    def test_claude_runs_unrestricted_in_the_task_directory(
        self, planner_claude: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from looper.db import role_configs

        async def _config(agent_id: str) -> dict[str, Any]:
            return {"system_prompt": "You plan carefully.", "timeout_seconds": 60, "max_turns": 9}

        monkeypatch.setattr(role_configs, "get_config", _config)
        from looper.db import agent_setting_layers

        monkeypatch.setattr(agent_setting_layers, "list_layers", AsyncMock(
            return_value=[{"scope": "role", "role": "planner", "profile": profile_json("claude-test")}]))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        h = drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        # Every tool, no permission prompts: the role prompt, not the command, keeps it to planning.
        _assert_unrestricted(argv)
        assert _flag(argv, "--input-format") == "stream-json"
        assert _flag(argv, "--output-format") == "stream-json"
        assert (_flag(argv, "--model"), _flag(argv, "--max-turns")) == ("claude-test", "9")
        # The shared isolation flags: no personal settings, memory, hooks, plugins or MCP servers.
        isolation = isolation_args(h.work_dir)
        at = argv.index(isolation[0])
        assert argv[at:at + len(isolation)] == isolation
        # The system prompt is a file named by its hash, read again on every launch.
        assert "--append-system-prompt" not in argv
        assert _flag(argv, "--system-prompt-snapshot") == "off"
        (prompt,) = claude.prompts()
        assert Path(_flag(argv, "--append-system-prompt-file") or "").name == f"{_sha(prompt)}.md"
        _in_order(prompt, "You plan carefully.\n\n## Task\n", "build the calc package", REPLY_CONTRACT)
        assert prompt.endswith(REPLY_CONTRACT)
        # Every turn arrives on stdin; the command line carries no prompt.
        takes_value = {"--input-format", "--output-format",
                       "--append-system-prompt-file", "--system-prompt-snapshot", "--setting-sources",
                       "--settings", "--model", "--max-turns"}
        assert [a for i, a in enumerate(argv) if not a.startswith("--") and argv[i - 1] not in takes_value] == []

    def test_each_turn_is_recorded_with_its_own_cost(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        starts = [call.kwargs for call in agent_db.start_record.await_args_list]
        assert [s["agent_id"] for s in starts] == ["planner-t1", "planner-t1"]
        assert [s["task_id"] for s in starts] == ["t1", "t1"]
        assert [s["input_text"] for s in starts] == [turn["text"] for turn in claude.turns()]
        (pid,) = claude.pids()
        assert all(s["metadata"]["pid"] == pid and s["metadata"]["command"][0] == claude.bin for s in starts)
        finishes = [call.kwargs for call in agent_db.finish_record.await_args_list]
        assert [f["status"] for f in finishes] == ["completed", "completed"]
        # Claude reports its process's running total; each turn records its own share.
        assert [f["cost_usd"] for f in finishes] == [0.25, 0.25]
        assert finishes[1]["output_text"].endswith("```")

    def test_a_turn_claude_runs_on_its_own_is_recorded_and_never_taken_for_a_reply(
        self, planner_claude: Any, agent_db: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # After planning, Claude runs a turn of its own (its background work ended); what it says there
        # would fail the task if it were taken for the next turn's reply.
        claude = planner_claude(plan(step("s1")), decide("accept"),
                                autonomous={1: decide("fail", reason="said on its own")})
        with caplog.at_level("INFO", logger="looper.agents.tiers.planner"):
            h = drive(claude, always(WORK_OK))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        assert not any("said on its own" in line for line in activity(agent_db))
        from looper.agents.claude_cli import AUTONOMOUS_INPUT

        starts = [call.kwargs for call in agent_db.start_record.await_args_list]
        assert [s["input_text"] for s in starts].count(AUTONOMOUS_INPUT) == 1
        (own,) = [s for s in starts if s["input_text"] == AUTONOMOUS_INPUT]
        assert own["agent_id"] == "planner-t1" and own["task_id"] == "t1" and own["metadata"]["autonomous"] is True
        # Three runs at 0.25 each, the autonomous one among them; the plan keeps the whole running total.
        finishes = [call.kwargs for call in agent_db.finish_record.await_args_list]
        assert sorted(f["output_text"] for f in finishes) == sorted(
            [plan(step("s1")), decide("accept"), decide("fail", reason="said on its own")])
        assert [f["cost_usd"] for f in finishes] == [0.25, 0.25, 0.25]
        assert h.planner._plan.session_totals == Usage(cost_usd=0.75)  # type: ignore[union-attr]
        assert any("ran a turn on its own" in r.getMessage() for r in caplog.records)


# --- Resuming after a server restart ---


@pytest.mark.usefixtures("agent_db")
class TestResume:
    def test_a_restarted_planner_carries_on_from_the_saved_plan(self, planner_claude: Any, agent_db: Any) -> None:
        """Each step goes on from where the stop left it, in the same session, without planning again."""
        claude = planner_claude(
            plan(step("a"), step("b"), step("c", review=True), step("d", "a")),
            decide("accept"),  # a
            decide("retry", reason="b failed", feedback="fix the parser"),  # b
            decide("review", reason="c needs a look"),  # c
            "<hang>",  # d: the server stops in the middle of this decision
            decide("accept"),  # every decision after the restart
        )
        resumed = False

        def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
            key = envelope.payload["step_key"]
            if envelope.message_type == MessageType.VERIFY_REQUEST:
                return REVIEW_PASS if resumed else None  # c's first Reviewer is still running at the stop
            if key == "b" and envelope.payload["attempt"] == 1:
                return WORK_FAILED
            if key == "b" and not resumed:
                return None  # b's retry is still running at the stop
            return WORK_OK

        async def scenario(h: Harness) -> None:
            nonlocal resumed
            await h.start()
            await h.until(lambda: len(h.held) == 2 and len(claude.turns()) == 5)
            await h.planner.stop()  # the server stops
            saved = {s["key"]: s for s in agent_db.store.steps["t1"].values()}
            assert {key: s["status"] for key, s in saved.items()} == {
                "a": "verified", "b": "dispatched", "c": "completed", "d": "deciding"}
            assert (saved["b"]["retries"], saved["b"]["feedback"]) == (1, ["fix the parser"])
            assert saved["d"]["result"]["summary"] == "did it"  # the cut-off decision's input

            resumed = True
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
                payload={"task_id": "t1"}, correlation_id="corr-1",
            ))
            await h.finished()

        h = drive(claude, outcome, scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        texts = [turn["text"] for turn in claude.turns()]
        assert texts[0].startswith("[PLAN]") and all(t.startswith("[DECISION]") for t in texts[1:])
        assert texts[5].startswith("[DECISION] Step d")  # the cut-off decision, taken again first
        # The restarted Claude resumes the session.
        assert [_flag(argv, "--resume") for argv in claude.argvs()] == [None, "sess-p"]
        # b runs again as the same attempt with its feedback; d's result is not run again; c gets a new Reviewer.
        b_runs = [w.payload for w in h.work_items if w.payload["step_key"] == "b"]
        assert [(p["attempt"], p["feedback"]) for p in b_runs] == [
            (1, []), (2, ["fix the parser"]), (2, ["fix the parser"])]
        assert [w.payload["step_key"] for w in h.work_items].count("d") == 1
        assert len(h.reviews) == 2
        assert agent_db.store.plans["t1"]["status"] == "completed"
        assert not (h.work_dir.parent / ".local" / "planner").exists()  # no plan file is written


# --- The system prompt: harness, role, project background, task, reply contract ---


def _project(name: str = "calc", *, background: str = "", description: str = "") -> dict[str, Any]:
    return {"id": "proj-1", "name": name, "description": description, "work_dir": "/srv/calc",
            "background": background, "updated_at": "2026-10-13T12:00:00+00:00"}


@pytest.fixture
def planner_row(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The ``planner`` agent config row; a test may change its prompt between turns."""
    from looper.db import role_configs

    row: dict[str, Any] = {"system_prompt": "You plan carefully.", "default_prompt": "You plan.", "timeout_seconds": 60,
                           "max_turns": 0}

    async def _config(agent_id: str) -> dict[str, Any]:
        assert agent_id == "planner"
        return dict(row)

    monkeypatch.setattr(role_configs, "get_config", _config)
    return row


def _starts(agent_db: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in agent_db.start_record.await_args_list]


class TestRecommendedRole:
    """The Planner has every tool, so its recommended role prompt is what keeps
    it to planning: it decides and hands every change to a Worker."""

    def test_it_says_the_planner_plans_and_workers_do_the_work(self) -> None:
        from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT

        text = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "You plan and decide; Workers do the work." in text
        assert "Do not modify files yourself" in text
        assert "every change to the project goes to a Worker as a step" in text
        for stale in ("Read, Grep, Glob", "cannot change files", "or run commands"):  # no longer read-only
            assert stale not in text


def test_every_ready_step_is_dispatched_at_once(planner_claude: Any, agent_db: Any) -> None:
    """No per-task cap: six steps that need nothing of each other all go to the worker pool together."""
    claude = planner_claude(plan(*(step(f"s{i}") for i in range(1, 7))), *(decide("accept") for _ in range(6)))

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: len(h.held) == 6)
        assert len(h.work_items) == 6 and set(h.dispatched_at_turn) == {1}  # all after the PLAN turn
        for _ in range(6):
            await h.release(WORK_OK)
        await h.finished()

    h = drive(claude, lambda envelope: None, scenario)
    assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestSystemPrompt:
    def test_the_layers_are_harness_role_project_task_then_the_reply_contract(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = _project(background="Use uv.\nPython 3.12.")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        h = drive(claude, always(WORK_OK))

        (prompt,) = claude.prompts()
        _in_order(prompt, f"{DEFAULT_HARNESS_PROMPT}\n\n", "You plan carefully.\n\n",
                  "## Project: calc\nUse uv.\nPython 3.12.\n\n", "## Task\n", "build the calc package", REPLY_CONTRACT)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nYou plan carefully.\n\n")
        assert prompt.endswith(REPLY_CONTRACT)
        agent_db.get_task_project.assert_awaited_with("t1")  # the task's project as it is now
        # Every turn records the system prompt it ran with, its hash and its layers.
        starts = _starts(agent_db)
        assert [s["metadata"]["system_prompt"] for s in starts] == [prompt, prompt]
        assert {s["metadata"]["system_prompt_sha256"] for s in starts} == {_sha(prompt)}
        layers = starts[0]["metadata"]["prompt_layers"]
        assert layers["harness"] == {"source": "builtin", "sha256": _sha(DEFAULT_HARNESS_PROMPT)}
        assert layers["role"] == {"config_id": "planner", "sha256": _sha("You plan carefully."), "recommended": False}
        assert layers["project"] == {"id": "proj-1", "background_sha256": _sha("Use uv.\nPython 3.12."),
                                     "updated_at": "2026-10-13T12:00:00+00:00", "knowledge_sha256": None,
                                     "knowledge_documents": 0}
        task = prompt[prompt.index("## Task"):prompt.index(REPLY_CONTRACT)].rstrip("\n")
        assert layers["task"] == {"kind": "plan", "task_id": "t1", "step_id": None, "sha256": _sha(task)}
        assert layers["contract"] == {"name": "REPLY_CONTRACT", "sha256": _sha(REPLY_CONTRACT)}
        assert layers["isolation"] == ISOLATION_ID
        assert str(h.work_dir) in task
        assert [line for line in activity(agent_db) if "background" in line] == [
            "Project background applied (20 chars)"]

    @pytest.mark.parametrize(("new_background", "note"), [
        ("v2: use pnpm", "Project background changed (12 chars)"),
        ("", "Project background removed"),
    ])
    def test_a_changed_background_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, new_background: str, note: str,
    ) -> None:
        agent_db.get_task_project.return_value = _project(background="v1")
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept"), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)  # s1's Worker runs; the plan turn used v1
            agent_db.get_task_project.return_value = _project(background=new_background)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        first, second = claude.argvs()
        first_prompt, second_prompt = claude.prompts()
        assert "## Project: calc\nv1\n\n## Task" in first_prompt
        expected = f"## Project: calc\n{new_background}\n\n## Task" if new_background else "carefully.\n\n## Task"
        assert expected in second_prompt
        assert second_prompt.endswith(REPLY_CONTRACT)
        # A changed prompt is a new file with a new hash; the restarted process resumes the session.
        assert _flag(first, "--append-system-prompt-file") != _flag(second, "--append-system-prompt-file")
        assert Path(_flag(second, "--append-system-prompt-file") or "").name == f"{_sha(second_prompt)}.md"
        assert _flag(first, "--resume") is None
        assert _flag(second, "--resume") == "sess-p"
        assert [s["metadata"]["system_prompt_sha256"] for s in _starts(agent_db)] == [
            _sha(first_prompt), _sha(second_prompt), _sha(second_prompt)]
        # The plan turn ran in the first process; both decisions in the restarted one.
        first_pid, second_pid = claude.pids()
        assert [turn["pid"] for turn in claude.turns()] == [first_pid, second_pid, second_pid]
        notes = [line for line in activity(agent_db) if line.startswith("Project background")]
        assert notes == ["Project background applied (2 chars)", note]

    def test_a_failed_read_keeps_the_last_project_and_its_claude(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = _project(background="v1", description="a calculator")
        claude = planner_claude(plan(step("s1")), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            agent_db.get_task_project.side_effect = ConnectionError("database is down")
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (argv,) = claude.argvs()  # the system prompt did not change, so Claude was not restarted
        (prompt,) = claude.prompts()
        assert "## Project: calc\nv1\n\n" in prompt and "a calculator" in prompt


def _settings(agent_db: Any, **stored: str) -> None:
    """What the system layer holds: harness, the profile of a model and env (a JSON list)."""
    names = {"harness": "harness_prompt", "model": "profile", "env": "env"}
    agent_db.list_layers.return_value = [
        {"scope": "system", **{names[k]: json.loads(v) if k == "env" else profile_json(v) if k == "model" else v
                               for k, v in stored.items()}}]


def _between_turns(agent_db: Any, change: Callable[[], None]) -> Callable[[Harness], Awaitable[None]]:
    """A scenario that makes ``change`` while the first step's Worker runs (after the plan turn)."""

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.until(lambda: h.held)
        change()
        await h.release(WORK_OK)
        await h.finished()

    return scenario


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestHarnessAndLaunch:
    """The Planner's agent settings are resolved in its task's project every
    turn; a changed harness, model or environment restarts its Claude, which
    resumes the session."""

    def test_the_projects_planner_harness_comes_first(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.list_layers.return_value = [
            {"scope": "system", "harness_prompt": "DEFAULT HARNESS"},
            {"scope": "role_project", "role": "planner", "project_id": "proj-1",
             "harness_prompt": "PLAN LIKE AN ARCHITECT"},
            {"scope": "role_project", "role": "worker", "project_id": "proj-1",
             "harness_prompt": "WORK LIKE A CRAFTSMAN"},
        ]
        agent_db.get_task_project.return_value = _project()
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (prompt,) = claude.prompts()
        assert prompt.startswith("PLAN LIKE AN ARCHITECT\n\nYou plan carefully.\n\n")
        assert "DEFAULT HARNESS" not in prompt and "WORK LIKE A CRAFTSMAN" not in prompt
        assert _starts(agent_db)[0]["metadata"]["prompt_layers"]["harness"] == {
            "source": "role_project", "sha256": _sha("PLAN LIKE AN ARCHITECT"), "project_id": "proj-1"}

    @pytest.mark.parametrize(("harness", "role"), [
        ("HARNESS V2", "You plan carefully."),  # the Settings harness changed
        ("HARNESS V1", "You plan very carefully."),  # the planner row's role prompt was edited
    ])
    def test_a_changed_harness_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, planner_row: dict[str, Any], harness: str, role: str,
    ) -> None:
        _settings(agent_db, harness="HARNESS V1")
        claude = planner_claude(plan(step("s1")), decide("accept"))

        def edit() -> None:
            _settings(agent_db, harness=harness)
            planner_row["system_prompt"] = role

        drive(claude, hold_first(), _between_turns(agent_db, edit))

        first, second = claude.argvs()
        first_prompt, second_prompt = claude.prompts()
        assert first_prompt.startswith("HARNESS V1\n\nYou plan carefully.\n\n")
        assert second_prompt.startswith(f"{harness}\n\n{role}\n\n")
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-p")
        roles = [s["metadata"]["prompt_layers"]["role"]["sha256"] for s in _starts(agent_db)]
        assert roles == [_sha("You plan carefully."), _sha(role)]

    def test_a_changed_model_restarts_claude_resuming_its_session(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, model="sonnet")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, hold_first(), _between_turns(agent_db, lambda: _settings(agent_db, model="opus")))

        first, second = claude.argvs()
        assert (_flag(first, "--model"), _flag(second, "--model")) == ("sonnet", "opus")
        assert _flag(second, "--resume") == "sess-p"
        first_prompt, second_prompt = claude.prompts()
        assert first_prompt == second_prompt  # the model alone restarted it
        assert [s["metadata"]["model_choice"]["model"] for s in _starts(agent_db)] == ["sonnet", "opus"]

    def test_a_changed_environment_restarts_claude_with_it(
        self, planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launched: list[dict[str, Any]] = []
        real_exec = asyncio.create_subprocess_exec

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            launched.append(kwargs)
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
        _settings(agent_db, env=json.dumps([{"key": "LOOPER_TEST_FLAVOUR", "value": "mint"}]))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        change = lambda: _settings(agent_db, env=json.dumps([{"key": "LOOPER_TEST_FLAVOUR", "value": "lime"}]))
        drive(claude, hold_first(), _between_turns(agent_db, change))

        assert [kwargs["env"]["LOOPER_TEST_FLAVOUR"] for kwargs in launched] == ["mint", "lime"]
        first, second = claude.argvs()
        assert first == [arg for arg in second if arg not in ("--resume", "sess-p")]  # same command, new env
        assert _flag(second, "--resume") == "sess-p"
        recorded = [s["metadata"]["env"]["keys"] for s in _starts(agent_db)]
        assert recorded == [{"LOOPER_TEST_FLAVOUR": _sha("mint")}, {"LOOPER_TEST_FLAVOUR": _sha("lime")}]

    def test_unchanged_settings_keep_one_process(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, harness="H", model="sonnet", env=json.dumps([{"key": "LOOPER_TEST_FLAVOUR", "value": "x"}]))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        assert len(claude.argvs()) == 1 and len(claude.turns()) == 2


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestLibrary:
    """The Planner loads the plugins and the skills plugin installed for it
    globally; a change to that set restarts its Claude, which resumes the
    session."""

    def test_the_command_carries_the_library_and_records_it(
        self, planner_claude: Any, agent_db: Any, enabled_library: Any,
    ) -> None:
        demo, alpha = enabled_library(DEMO_PLUGIN, ALPHA_SKILL)
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        plugin, skills = plugin_dirs(argv)
        assert plugin.endswith(f"/.local/library/{demo['sha256']}/demo") and skills.endswith("/personal")
        # Every tool, Skill included, so that the library's skills reach the Planner too.
        _assert_unrestricted(argv)
        assert all(s["metadata"]["library"]["items"] == [
            {"kind": "plugin", "name": "demo@market", "sha256": demo["sha256"], "plugin": "demo", "scope": "global"},
            {"kind": "skill", "name": "alpha", "sha256": alpha["sha256"], "plugin": "personal", "scope": "global"}]
            for s in _starts(agent_db))
        assert {call.args[0] for call in agent_db.list_installed.await_args_list} == {"planner"}

    def test_a_changed_library_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, enabled_library: Any,
    ) -> None:
        enabled_library(DEMO_PLUGIN, ALPHA_SKILL)
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, hold_first(), _between_turns(agent_db, lambda: enabled_library(DEMO_PLUGIN)))

        first, second = claude.argvs()
        assert (len(plugin_dirs(first)), len(plugin_dirs(second))) == (2, 1)
        assert _flag(second, "--resume") == "sess-p"
        first_prompt, second_prompt = claude.prompts()
        assert first_prompt == second_prompt  # the library alone restarted it
        hashes = [s["metadata"]["library"]["sha256"] for s in _starts(agent_db)]
        assert hashes[0] != hashes[1]


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestKnowledge:
    """The Planner's project layer carries its project's knowledge index and its command mounts the
    documents' directories; a change to the knowledge restarts its Claude, which resumes the session."""

    @pytest.fixture
    def project_dir(self, tmp_path: Path, agent_db: Any) -> Path:
        directory = tmp_path / "calc"
        (directory / "docs").mkdir(parents=True)
        (directory / "docs" / "runbook.md").write_text("Deploy with MARKER-BETA.\n", encoding="utf-8")
        agent_db.get_task_project.return_value = {**_project(background="Use uv."), "work_dir": str(directory)}
        return directory

    def test_the_command_mounts_the_documents_and_the_prompt_carries_their_index(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms the code uses", "MARKER-ALPHA"),
                          ("path", "Runbook", "How to deploy", "docs/runbook.md"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        h = drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        set_dir, mounted_project = _add_dirs(argv)
        # The stored documents' set directory, and the project's since the task runs in its own directory.
        assert Path(set_dir).parent == h.work_dir.parent / ".local" / "projects" / "proj-1" / "knowledge"
        assert mounted_project == str(project_dir)
        _assert_unrestricted(argv)
        (prompt,) = claude.prompts()
        _in_order(prompt, "## Project: calc\nUse uv.\n\n", "## Project knowledge\n",
                  f"In {set_dir}:\n- Glossary: Terms the code uses (Glossary.md)\n",
                  f"In {project_dir}:\n- Runbook: How to deploy (docs/runbook.md)\n\n## Task\n",
                  REPLY_CONTRACT)
        index = prompt[prompt.index("## Project knowledge"):prompt.index("\n\n## Task")]
        for start in _starts(agent_db):
            project = start["metadata"]["prompt_layers"]["project"]
            assert (project["knowledge_sha256"], project["knowledge_documents"]) == (_sha(index), 2)
        assert [line for line in activity(agent_db) if "knowledge" in line] == [
            "Project knowledge applied (2 documents)"]

    @pytest.mark.parametrize(("after", "note"), [
        ((("text", "Glossary", "Terms", "MARKER-ALPHA"), ("text", "Style", "How we write", "MARKER-DELTA")),
         "Project knowledge changed (2 documents)"),
        ((), "Project knowledge removed"),
    ], ids=["added", "removed"])
    def test_a_changed_knowledge_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
        after: tuple[tuple[str, str, str, str], ...], note: str,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, hold_first(), _between_turns(agent_db, lambda: project_documents(*after)))

        first, second = claude.argvs()
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-p")
        assert _add_dirs(first) and bool(_add_dirs(second)) == bool(after)
        first_prompt, second_prompt = claude.prompts()
        assert "- Style: How we write" not in first_prompt
        if after:
            assert "- Style: How we write (Style.md)" in second_prompt
        else:
            assert "## Project knowledge" not in second_prompt
        documents = [s["metadata"]["prompt_layers"]["project"]["knowledge_documents"] for s in _starts(agent_db)]
        assert documents == [1, len(after)]
        assert [line for line in activity(agent_db) if "knowledge" in line] == [
            "Project knowledge applied (1 document)", note]

    def test_a_failed_read_keeps_the_last_knowledge_and_its_claude(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        fail = lambda: setattr(agent_db.list_documents, "side_effect", ConnectionError("database is down"))
        h = drive(claude, hold_first(), _between_turns(agent_db, fail))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (argv,) = claude.argvs()  # same knowledge, so Claude was not restarted
        assert "- Glossary: Terms (Glossary.md)" in claude.prompts()[0]
        assert "Warning: the project's knowledge could not be read" in activity(agent_db)


def _project_plugins(argv: list[str]) -> list[str]:
    """The ``--plugin-dir`` values that are the generated plugin of a project's own skills."""
    return [d for d in plugin_dirs(argv) if d.endswith("/project")]


def _reading_plugins(
    found: dict[str, dict[str, str]], change: Callable[[], None] | None = None,
) -> Callable[[Harness], Awaitable[None]]:
    """Run the task (making ``change`` while the first step's Worker runs, if given), then keep the
    SKILL.md files of every project plugin the Planner's commands mounted, by directory, before the
    runtime root goes away."""

    async def scenario(h: Harness) -> None:
        await h.start()
        if change is not None:
            await h.until(lambda: h.held)
            change()
            await h.release(WORK_OK)
        await h.finished()
        for argv in h.claude.argvs():
            for directory in _project_plugins(argv):
                skills = Path(directory) / "skills"
                found[directory] = {p.name: (p / "SKILL.md").read_text(encoding="utf-8")
                                    for p in sorted(skills.iterdir())}

    return scenario


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestProjectSkills:
    """The Planner's command mounts the skills its task's project wrote and installed for the Planner as one
    plugin, read as each turn starts; a change to them restarts its Claude, which resumes the session."""

    def test_the_command_mounts_its_projects_skills_once_and_records_them(
        self, planner_claude: Any, agent_db: Any, project_skills: Any,
    ) -> None:
        project_skills(("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."),
                       ("proj-2", "elsewhere", "Another project's.", "Reply ELSEWHERE."))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        found: dict[str, dict[str, str]] = {}
        h = drive(claude, always(WORK_OK), _reading_plugins(found))

        (argv,) = claude.argvs()
        (plugin,) = _project_plugins(argv)
        assert plugin.startswith(f"{h.work_dir.parent}/.local/library/")
        assert found[plugin] == {"deploy-check": (
            "---\nname: deploy-check\ndescription: \"Use before a deploy.\"\n---\n\nReply DEPLOY-OK.\n")}
        _assert_unrestricted(argv)
        for start in _starts(agent_db):
            library = start["metadata"]["library"]
            assert (library["role"], library["project_id"]) == ("planner", "proj-1")
            assert library["items"] == [{"kind": "skill", "name": "deploy-check", "plugin": "project",
                                         "scope": "project", "sha256": library["items"][0]["sha256"]}]
        assert [line for line in activity(agent_db) if "skills" in line] == [
            "Installed plugins and skills applied (1 skill)"]

    @pytest.mark.parametrize(("after", "note"), [
        ((("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."),
          ("proj-1", "release-notes", "Drafts release notes.", "Reply NOTES-OK.")),
         "Installed plugins and skills changed (2 skills)"),
        ((), "Installed plugins and skills removed"),  # the last skill uninstalled
    ], ids=["added", "removed"])
    def test_changed_skills_restart_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, project_skills: Any,
        after: tuple[tuple[str, str, str, str], ...], note: str,
    ) -> None:
        project_skills(("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        found: dict[str, dict[str, str]] = {}
        drive(claude, hold_first(), _reading_plugins(found, lambda: project_skills(*after)))

        first, second = claude.argvs()
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-p")
        assert [sorted(found[d]) for d in _project_plugins(second)] == ([[s[1] for s in after]] if after else [])
        assert claude.prompts()[0] == claude.prompts()[1]  # the skills alone restarted it
        recorded = [s["metadata"].get("library") for s in _starts(agent_db)]
        assert [None if r is None else len(r["items"]) for r in recorded] == [1, len(after) or None]
        assert [line for line in activity(agent_db) if "skills" in line] == [
            "Installed plugins and skills applied (1 skill)", note]

    def test_a_task_moved_to_another_project_takes_that_projects_skills(
        self, planner_claude: Any, agent_db: Any, project_skills: Any,
    ) -> None:
        project_skills(("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."),
                       ("proj-2", "elsewhere", "Another project's.", "Reply ELSEWHERE."))
        agent_db.get_task_project.return_value = _project()
        claude = planner_claude(plan(step("s1")), decide("accept"))

        def move() -> None:
            agent_db.get_task_project.return_value = {**_project("other"), "id": "proj-2"}

        found: dict[str, dict[str, str]] = {}
        drive(claude, hold_first(), _reading_plugins(found, move))

        first, second = claude.argvs()
        (ours,), (theirs,) = _project_plugins(first), _project_plugins(second)
        assert (sorted(found[ours]), sorted(found[theirs])) == (["deploy-check"], ["elsewhere"])
        assert [call.args for call in agent_db.list_installed.await_args_list][-2:] == [
            ("planner", "proj-1"), ("planner", "proj-2")]

    def test_a_failed_read_keeps_the_last_skills_and_its_claude(
        self, planner_claude: Any, agent_db: Any, project_skills: Any,
    ) -> None:
        project_skills(("proj-1", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."))
        claude = planner_claude(plan(step("s1")), decide("accept"))

        def fail() -> None:
            agent_db.list_installed.side_effect = ConnectionError("database is down")

        h = drive(claude, hold_first(), _between_turns(agent_db, fail))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (argv,) = claude.argvs()  # the same skills, so Claude was not restarted
        assert _project_plugins(argv)
        assert "Warning: the installed items could not be read" in activity(agent_db)


# --- Ending the task through the TaskRouter ---


@pytest.mark.usefixtures("agent_db")
class TestTaskEnd:
    def test_task_end_reaps_the_planners_claude_and_keeps_its_summary(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept", summary="calc works"))

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), claude, always(WORK_OK))  # its bus and worker pool; the router makes the Planner
                router = TaskRouter(runtime_root=Path(tmp), claude_bin=claude.bin)
                await router.start(h.bus)
                await h.bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_REQUEST,
                    payload={"task_id": "task-calc", "goal_id": "g1", "requirement": "build calc",
                             "work_dir": str(h.work_dir), "project_id": "proj-1"},
                ))
                await h.bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_START,
                    payload={"task_id": "task-calc"},
                ))
                # Teardown ends the Planner, which ends and reaps its Claude.
                await h.until(lambda: claude.pids() and not router._planners and not _alive(claude.pids()[0]))
                (task,) = router._tasks.values()
                assert task.status == "completed"
                await router.stop()
                await h.bus.shutdown()

        asyncio.run(_run())
        final = agent_db.update_task_status.await_args_list
        assert any(call.args[1] == "completed" and call.kwargs.get("result_summary") == FINAL_RESULT
                   for call in final)

    def test_a_stopped_task_stops_its_running_workers_and_reviewers(self, planner_claude: Any) -> None:
        """A task's stop ends every step run it has going: s2's Worker and the Reviewer of s1."""
        claude = planner_claude(plan(step("s1", review=True), step("s2")), decide("review"))

        def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
            if envelope.message_type == MessageType.VERIFY_REQUEST or envelope.payload["instruction"] == "do s2":
                return None  # still running
            return WORK_OK

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.reviews)  # then the task stops

        h = drive(claude, outcome, scenario)
        s2 = next(item for item in h.work_items if item.payload["instruction"] == "do s2")
        assert {envelope.target for envelope in h.killed} == {s2.target, h.reviews[0].target}

    def test_server_stop_during_a_teardown_still_reaps_claude(self, planner_claude: Any) -> None:
        """Stopping the router while it ends a Planner neither hangs nor leaves Claude running."""
        claude = planner_claude(plan(step("s1")), decide("accept"), linger=True)  # outlives its stdin

        async def _run() -> tuple[float, bool]:
            with tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), claude, always(WORK_OK))
                router = TaskRouter(runtime_root=Path(tmp), claude_bin=claude.bin)
                await router.start(h.bus)
                await h.bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_REQUEST,
                    payload={"task_id": "task-calc", "goal_id": "g1", "requirement": "build calc",
                             "work_dir": str(h.work_dir), "project_id": "proj-1"},
                ))
                await h.bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_START,
                    payload={"task_id": "task-calc"},
                ))
                # The task has ended and its teardown waits for the lingering Claude to exit.
                await h.until(lambda: router._tasks and not router._planners and claude.pids()
                              and next(iter(router._tasks.values())).status == "completed")
                assert _alive(claude.pids()[0])
                started = asyncio.get_event_loop().time()
                await asyncio.wait_for(router.stop(), 4)
                took = asyncio.get_event_loop().time() - started
                (pid,) = claude.pids()
                alive = _alive(pid)  # checked before the event loop closes, which would kill it anyway
                await h.bus.shutdown()
                return took, alive

        took, alive = asyncio.run(_run())
        assert took < 2  # well inside the 5 s stop grace
        assert not alive

    @pytest.mark.parametrize(("status", "fail_reason", "opening"), [
        ("failed", "c cannot parse the input", "The task failed: c cannot parse the input. The user restarted it"),
        # A reason that already ends its sentence gets no second period.
        ("failed", "c cannot parse the input; replanning.",
         "The task failed: c cannot parse the input; replanning. The user restarted it"),
        ("failed", "Can c parse the input?", "The task failed: Can c parse the input? The user restarted it"),
        ("stopped", "", "The task was stopped. The user restarted it"),
    ])
    def test_the_restart_turn_says_how_the_task_ended_in_one_sentence(
        self, tmp_path: Path, status: str, fail_reason: str, opening: str,
    ) -> None:
        from looper.agents.tiers.planner import PlanStep, TaskPlan
        from looper.db.task_changes import TaskChanges

        planner = PlannerAgent(task_id="t1", runtime_root=tmp_path)
        task_plan = TaskPlan(plan_id="p1", goal_id="", requirement="build it", work_dir=str(tmp_path))
        task_plan.steps = [PlanStep(step_id="c", index=0, instruction="do c")]
        message = planner._restart_message(task_plan, planner._ended(status, fail_reason), TaskChanges())
        assert message.startswith(f"[RESTART] {opening}, and it goes on from its plan")


@pytest.mark.usefixtures("agent_db")
class TestTaskJournal:
    def test_over_its_cap_the_progress_section_drops_the_oldest_entries_and_counts_them(
        self, planner_claude: Any,
    ) -> None:
        """Each accepted step's entry carries five 300-character learnings, so two entries fill the section."""

        def learned(envelope: MessageEnvelope) -> dict[str, Any]:
            key = envelope.payload["step_key"]
            return {**WORK_OK, "summary": f"{key} done", "learnings": [f"{key}-tip{n} " + "x" * 290 for n in range(5)]}

        claude = planner_claude(
            plan(step("s1"), step("s2", "s1"), step("s3", "s2"), step("s4", "s3")),
            decide("accept"), decide("accept"), decide("accept"), decide("accept"),
        )
        drive(claude, learned)

        decision_on_s4 = claude.turns()[-1]["text"]
        assert decision_on_s4.startswith("[DECISION] Step s4")
        section = decision_on_s4[decision_on_s4.index("Progress and findings"):decision_on_s4.index("Result of step")]
        assert len(section) <= 4_000
        assert "(1 entry left out)" in section.splitlines()[0]
        # Newest first: s3, then s2; s1, the oldest, is the one left out.
        assert section.index("- s3: s3 done") < section.index("- s2: s2 done")
        assert "s1-tip" not in section and "s3-tip4" in section


@pytest.mark.usefixtures("agent_db")
class TestProgressAndFinalSummary:
    def test_progress_notes_go_to_the_journal_and_the_last_open_step_is_named(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        notes = [{"done": "nothing yet", "now": "s2", "next": "nothing", "risks": "none", "estimate": "a minute"},
                 {"done": "s1 and s2", "now": "nothing", "next": "nothing", "risks": "none", "estimate": "done"}]
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept", progress=notes[0]),
                                decide("accept", progress=notes[1]))
        final = drive(claude, always(WORK_OK)).final()

        assert final is not None and final.payload["summary"] == FINAL_RESULT
        # Each decision's note is a journal entry of the whole task, which the agents' sections leave out.
        kept = [e for e in agent_db.store.journal["t1"] if e["kind"] == "progress"]
        assert [(e["step_id"], json.loads(e["summary"])) for e in kept] == [("", note) for note in notes]
        decisions = [t["text"] for t in claude.turns() if t["text"].startswith("[DECISION]")]
        assert "nothing yet" not in decisions[1]
        # Only the decision on the last open step is told it must carry the final summary.
        assert "last open step" not in decisions[0] and "This is the task's last open step" in decisions[1]

    def test_a_completing_accept_without_a_final_summary_gets_a_repair_turn(self, planner_claude: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept", final=None), decide("accept"))
        final = drive(claude, always(WORK_OK)).final()

        assert final is not None and final.payload["summary"] == FINAL_RESULT
        repair = claude.turns()[-1]["text"]
        assert repair.startswith("[DECISION] Your last reply broke the reply contract")
        assert 'must carry "final"' in repair

    def test_a_stopped_task_gets_a_closing_note_from_its_saved_plan(self, agent_db: Any) -> None:
        from looper.agents.tiers.task import TaskRouter

        agent_db.store.plans["task-x"] = {"plan_id": "p1", "status": "executing"}
        agent_db.store.steps["task-x"] = {
            "a": {"id": "a", "key": "a", "index": 0, "status": "verified", "result": {"summary": "parser written"}},
            "b": {"id": "b", "key": "b", "index": 1, "status": "dispatched", "result": None},
        }
        note = asyncio.run(TaskRouter._stop_note("task-x"))
        assert note == ("## Why it ended\nThe task was stopped before it finished.\n\n## Done\n- a: parser written"
                        "\n\n## Not done\n- b (was running)")

