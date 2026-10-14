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

import pytest
from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN, plugin_dirs

from loopx.agents.agent_settings import DEFAULT_HARNESS_PROMPT
from loopx.agents.bus import MessageBus
from loopx.agents.claude_cli import ISOLATION_ID, isolation_args
from loopx.agents.run_usage import Usage
from loopx.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
)
from loopx.agents.tiers.planner import PlannerAgent
from loopx.agents.tiers.planner_protocol import REPLY_CONTRACT
from loopx.agents.tiers.task import TaskRouter

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


def decide(decision: str, **fields: Any) -> str:
    return reply({"decision": decision, **fields})


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
    from loopx.agents.claude_cli import permission_args

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
        assert final.payload["summary"] == "calc is built and tested"  # the last accept's summary
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
        (persisted,) = [call.args[1] for call in agent_db.upsert_steps.await_args_list]
        assert [row["instruction"] for row in persisted] == ["do s1", "do s2", "do s3"]
        phases = [call.args[1] for call in agent_db.update_task_phase.await_args_list]
        assert phases == ["planning", "executing", "completed"]

        first, *decisions = [turn["text"] for turn in claude.turns()]
        assert first.startswith("[PLAN]")
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

    def test_claude_error_fails_planning_without_a_repair_turn(self, planner_claude: Any) -> None:
        claude = planner_claude("<error>")
        h = drive(claude, always(WORK_OK))

        assert "did not succeed (error_during_execution)" in h.final().payload["summary"]  # type: ignore[union-attr]
        assert len(claude.turns()) == 1

    def test_turn_over_the_time_limit_fails_planning(
        self, planner_claude: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from loopx.db import agent_configs

        async def _config(agent_id: str) -> dict[str, Any]:
            assert agent_id == "planner"
            # Long enough for the stand-in to start on a loaded machine.
            return {"system_prompt": "You plan.", "timeout_seconds": 3, "model": None, "max_turns": 0}

        monkeypatch.setattr(agent_configs, "get_config", _config)
        claude = planner_claude("<hang>")
        h = drive(claude, always(WORK_OK))

        assert "longer than the 3 s limit (timeout_seconds)" in h.final().payload["summary"]  # type: ignore[union-attr]
        (pid,) = claude.pids()
        assert not _alive(pid)


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
        assert final.payload["summary"] == "reviewed and done"

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

    def test_replan_adds_a_step(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(
            plan(step("s1")),
            decide("replan", reason="tests are missing", steps=[step("s2", "s1")]),
            decide("accept", summary="all done"),
        )
        h = drive(claude, always(WORK_OK))

        assert [item.payload["instruction"] for item in h.work_items] == ["do s1", "do s2"]
        assert h.final().payload["summary"] == "all done"  # type: ignore[union-attr]
        plan_steps = h.planner._plan.steps  # type: ignore[union-attr]
        assert [(s.label, s.status) for s in plan_steps] == [("s1", "verified"), ("s2", "verified")]
        added = agent_db.upsert_steps.await_args_list[-1].args[1]
        assert [row["instruction"] for row in added] == ["do s2"]
        assert any("Plan changed: s2 (after s1)" in line for line in activity(agent_db))

    def test_replan_replaces_a_failed_step(self, planner_claude: Any) -> None:
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
    def test_one_process_serves_every_turn(self, planner_claude: Any) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept"), decide("accept"))
        drive(claude, always(WORK_OK))

        (pid,) = claude.pids()
        assert [turn["pid"] for turn in claude.turns()] == [pid, pid, pid]
        assert not _alive(pid)  # stop() ended it

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
            # The server stops; the plan file keeps the session and its running total.
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

    def test_claude_runs_unrestricted_in_the_task_directory(
        self, planner_claude: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from loopx.db import agent_configs

        async def _config(agent_id: str) -> dict[str, Any]:
            return {"system_prompt": "You plan carefully.", "timeout_seconds": 60, "model": "claude-test",
                    "max_turns": 9}

        monkeypatch.setattr(agent_configs, "get_config", _config)
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


# --- The system prompt: harness, role, project background, task, reply contract ---


def _project(
    name: str = "calc", *, background: str = "", description: str = "", planner_harness: str = "",
    worker_harness: str = "",
) -> dict[str, Any]:
    return {"id": "proj-1", "name": name, "description": description, "work_dir": "/srv/calc",
            "background": background, "planner_harness_prompt": planner_harness,
            "worker_harness_prompt": worker_harness, "updated_at": "2026-10-13T12:00:00+00:00"}


@pytest.fixture
def planner_row(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The ``planner`` agent config row; a test may change its prompt between turns."""
    from loopx.db import agent_configs

    row: dict[str, Any] = {"system_prompt": "You plan carefully.", "default_prompt": "You plan.", "timeout_seconds": 60,
                           "model": None, "max_turns": 0}

    async def _config(agent_id: str) -> dict[str, Any]:
        assert agent_id == "planner"
        return dict(row)

    monkeypatch.setattr(agent_configs, "get_config", _config)
    return row


def _starts(agent_db: Any) -> list[dict[str, Any]]:
    return [call.kwargs for call in agent_db.start_record.await_args_list]


class TestRecommendedRole:
    """The Planner has every tool, so its recommended role prompt is what keeps
    it to planning: it decides and hands every change to a Worker."""

    def test_it_says_the_planner_plans_and_workers_do_the_work(self) -> None:
        from loopx.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT

        text = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "You plan and decide; Workers do the work." in text
        assert "Do not modify files yourself" in text
        assert "every change to the project goes to a Worker as a step" in text

    def test_it_no_longer_calls_the_planner_read_only(self) -> None:
        from loopx.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT

        text = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        for stale in ("Read, Grep, Glob", "cannot change files", "or run commands"):
            assert stale not in text

    def test_the_plan_turn_asks_it_to_leave_the_project_unchanged(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        first = claude.turns()[0]["text"]
        assert first.startswith("[PLAN] ")
        assert "without changing it" in first and "You may read the project first" not in first


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
        assert layers["harness"] == {"source": "builtin_default", "sha256": _sha(DEFAULT_HARNESS_PROMPT)}
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

    def test_the_recommended_role_prompt_is_recorded_as_such(
        self, planner_claude: Any, agent_db: Any, planner_row: dict[str, Any],
    ) -> None:
        planner_row["system_prompt"] = planner_row["default_prompt"]
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        assert _starts(agent_db)[0]["metadata"]["prompt_layers"]["role"]["recommended"] is True

    def test_without_a_background_there_is_no_project_section(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = _project(background="")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (prompt,) = claude.prompts()
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nYou plan carefully.\n\n## Task\n")
        assert "## Project" not in prompt
        assert _starts(agent_db)[0]["metadata"]["prompt_layers"]["project"]["background_sha256"] is None
        assert not [line for line in activity(agent_db) if "background" in line]

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

    def test_a_changed_role_prompt_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, planner_row: dict[str, Any],
    ) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            planner_row["system_prompt"] = "You plan very carefully."
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(), scenario)

        first, second = claude.argvs()
        first_prompt, second_prompt = claude.prompts()
        assert first_prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nYou plan carefully.\n\n")
        assert second_prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\nYou plan very carefully.\n\n")
        assert _flag(second, "--resume") == "sess-p"
        roles = [s["metadata"]["prompt_layers"]["role"]["sha256"] for s in _starts(agent_db)]
        assert roles == [_sha("You plan carefully."), _sha("You plan very carefully.")]

    def test_a_task_moved_to_another_project_gets_its_task_section_updated(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = _project("calc", description="a calculator")
        claude = planner_claude(plan(step("s1")), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            # The task moved to another project while its Worker ran.
            agent_db.get_task_project.return_value = _project("tools", description="handy tools")
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(), scenario)

        first_prompt, second_prompt = claude.prompts()
        assert "calc" in first_prompt and "a calculator" in first_prompt
        assert "tools" in second_prompt and "handy tools" in second_prompt and "a calculator" not in second_prompt
        for turn in claude.turns():
            assert "a calculator" not in turn["text"] and "handy tools" not in turn["text"]

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
    """What the Settings table holds: harness, model, effort and env (a JSON list)."""
    keys = {"harness": "agent.harness_prompt", "model": "agent.model", "effort": "agent.effort", "env": "agent.env"}
    agent_db.get_settings.return_value = {keys[name]: value for name, value in stored.items()}


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
    """The Planner's harness is its project's Planner override, else the Settings
    default; its model is its own row's, else the Settings default (opus). A
    change to either, or to the Settings environment, restarts its Claude,
    which resumes the session."""

    def test_the_projects_planner_harness_comes_first(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, harness="DEFAULT HARNESS")
        agent_db.get_task_project.return_value = _project(planner_harness="PLAN LIKE AN ARCHITECT",
                                                          worker_harness="WORK LIKE A CRAFTSMAN")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (prompt,) = claude.prompts()
        assert prompt.startswith("PLAN LIKE AN ARCHITECT\n\nYou plan carefully.\n\n")
        assert "DEFAULT HARNESS" not in prompt and "WORK LIKE A CRAFTSMAN" not in prompt
        assert _starts(agent_db)[0]["metadata"]["prompt_layers"]["harness"] == {
            "source": "project_override", "sha256": _sha("PLAN LIKE AN ARCHITECT"), "project_id": "proj-1"}

    def test_without_an_override_the_settings_default_comes_first(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, harness="DEFAULT HARNESS")
        agent_db.get_task_project.return_value = _project(worker_harness="WORK LIKE A CRAFTSMAN")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (prompt,) = claude.prompts()
        assert prompt.startswith("DEFAULT HARNESS\n\nYou plan carefully.\n\n")
        assert _starts(agent_db)[0]["metadata"]["prompt_layers"]["harness"]["source"] == "system_default"

    def test_a_changed_harness_restarts_claude_resuming_its_session(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, harness="HARNESS V1")
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, hold_first(), _between_turns(agent_db, lambda: _settings(agent_db, harness="HARNESS V2")))

        first, second = claude.argvs()
        first_prompt, second_prompt = claude.prompts()
        assert first_prompt.startswith("HARNESS V1\n\n") and second_prompt.startswith("HARNESS V2\n\n")
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-p")

    def test_the_model_is_opus_by_default_and_recorded(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        assert _flag(argv, "--model") == "opus" and "--effort" not in argv
        assert {s["metadata"]["model_choice"]["source"] for s in _starts(agent_db)} == {"builtin_default"}

    def test_the_planners_model_beats_the_settings_default(
        self, planner_claude: Any, agent_db: Any, planner_row: dict[str, Any],
    ) -> None:
        _settings(agent_db, model="sonnet", effort="max")
        planner_row["model"] = "claude-haiku-4-5"
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        assert (_flag(argv, "--model"), _flag(argv, "--effort")) == ("claude-haiku-4-5", "max")
        assert _starts(agent_db)[0]["metadata"]["model_choice"] == {
            "model": "claude-haiku-4-5", "source": "agent_override", "effort": "max"}

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
        _settings(agent_db, env=json.dumps([{"key": "LOOPX_TEST_FLAVOUR", "value": "mint"}]))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        change = lambda: _settings(agent_db, env=json.dumps([{"key": "LOOPX_TEST_FLAVOUR", "value": "lime"}]))
        drive(claude, hold_first(), _between_turns(agent_db, change))

        assert [kwargs["env"]["LOOPX_TEST_FLAVOUR"] for kwargs in launched] == ["mint", "lime"]
        first, second = claude.argvs()
        assert first == [arg for arg in second if arg not in ("--resume", "sess-p")]  # same command, new env
        assert _flag(second, "--resume") == "sess-p"
        recorded = [s["metadata"]["env"]["keys"] for s in _starts(agent_db)]
        assert recorded == [{"LOOPX_TEST_FLAVOUR": _sha("mint")}, {"LOOPX_TEST_FLAVOUR": _sha("lime")}]

    def test_unchanged_settings_keep_one_process(self, planner_claude: Any, agent_db: Any) -> None:
        _settings(agent_db, harness="H", model="sonnet", env=json.dumps([{"key": "LOOPX_TEST_FLAVOUR", "value": "x"}]))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        assert len(claude.argvs()) == 1 and len(claude.turns()) == 2


@pytest.mark.usefixtures("agent_db", "planner_row")
class TestLibrary:
    """The Planner loads the enabled plugins and the skills plugin from the
    library; a change to the enabled set restarts its Claude, which resumes
    the session."""

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
            {"kind": "plugin", "name": "demo@market", "sha256": demo["sha256"]},
            {"kind": "skill", "name": "alpha", "sha256": alpha["sha256"]}] for s in _starts(agent_db))

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
        knowledge_root = f"{h.work_dir.parent}/.local/projects/proj-1/knowledge"
        # The task runs in its own directory, so the project's is mounted for the path document.
        assert _add_dirs(argv) == [knowledge_root, str(project_dir)]
        _assert_unrestricted(argv)
        (prompt,) = claude.prompts()
        _in_order(prompt, "## Project: calc\nUse uv.\n\n", "## Project knowledge\n",
                  f"- Glossary: Terms the code uses (read: {knowledge_root}/{_sha('MARKER-ALPHA')}/Glossary.md)\n",
                  f"- Runbook: How to deploy (read: {project_dir / 'docs' / 'runbook.md'})\n\n## Task\n",
                  REPLY_CONTRACT)
        index = prompt[prompt.index("## Project knowledge"):prompt.index("\n\n## Task")]
        for start in _starts(agent_db):
            project = start["metadata"]["prompt_layers"]["project"]
            assert (project["knowledge_sha256"], project["knowledge_documents"]) == (_sha(index), 2)
        assert [line for line in activity(agent_db) if "knowledge" in line] == [
            "Project knowledge applied (2 documents)"]

    def test_a_changed_knowledge_restarts_claude_resuming_its_session(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        add_style = lambda: project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"),
                                              ("text", "Style", "How we write", "MARKER-DELTA"))
        drive(claude, hold_first(), _between_turns(agent_db, add_style))

        first, second = claude.argvs()
        assert (_flag(first, "--resume"), _flag(second, "--resume")) == (None, "sess-p")
        first_prompt, second_prompt = claude.prompts()
        assert "- Style: How we write" not in first_prompt and "- Style: How we write (read: " in second_prompt
        assert [s["metadata"]["prompt_layers"]["project"]["knowledge_documents"] for s in _starts(agent_db)] == [1, 2]
        assert [line for line in activity(agent_db) if "knowledge" in line] == [
            "Project knowledge applied (1 document)", "Project knowledge changed (2 documents)"]

    def test_removing_the_knowledge_restarts_claude_without_it(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, hold_first(), _between_turns(agent_db, lambda: project_documents()))

        first, second = claude.argvs()
        assert _add_dirs(first) and not _add_dirs(second) and _flag(second, "--resume") == "sess-p"
        assert "## Project knowledge" not in claude.prompts()[1]
        assert [line for line in activity(agent_db) if "knowledge" in line] == [
            "Project knowledge applied (1 document)", "Project knowledge removed"]

    def test_unchanged_knowledge_keeps_one_process(
        self, planner_claude: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))
        assert len(claude.argvs()) == 1 and len(claude.turns()) == 2

    def test_a_failed_read_keeps_the_last_knowledge_and_its_claude(
        self, planner_claude: Any, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms", "MARKER-ALPHA"))
        claude = planner_claude(plan(step("s1")), decide("accept"))
        fail = lambda: setattr(agent_db.list_documents, "side_effect", ConnectionError("database is down"))
        h = drive(claude, hold_first(), _between_turns(agent_db, fail))

        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (argv,) = claude.argvs()  # same knowledge, so Claude was not restarted
        assert "- Glossary: Terms (read: " in claude.prompts()[0]
        assert "Warning: the project's knowledge could not be read" in activity(agent_db)


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
                    payload={"goal_id": "g1", "requirement": "build calc", "work_dir": str(h.work_dir),
                             "project_id": "proj-1"},
                ))
                # Teardown ends the Planner, which ends and reaps its Claude.
                await h.until(lambda: claude.pids() and not router._planners and not _alive(claude.pids()[0]))
                (task,) = router._tasks.values()
                assert task.status == "completed"
                await router.stop()
                await h.bus.shutdown()

        asyncio.run(_run())
        final = agent_db.update_task_status.await_args_list
        assert any(call.args[1] == "completed" and call.kwargs.get("result_summary") == "calc works"
                   for call in final)

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
                    payload={"goal_id": "g1", "requirement": "build calc", "work_dir": str(h.work_dir),
                             "project_id": "proj-1"},
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
