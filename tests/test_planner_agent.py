"""A task's Planner is a resident Claude Code agent that plans the steps and decides on every result.

The Planner's Claude is a stand-in (``planner_claude`` in conftest) that
answers each turn with a scripted reply; Workers and Reviewers are a stand-in
pool on the bus that answers each dispatch with a scripted result.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from loopx.agents.bus import MessageBus
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
        self.statuses: list[MessageEnvelope] = []
        self.replies: list[MessageEnvelope] = []
        self.bus.subscribe_topic(MessageType.WORK_ITEM, self._pool, consumes=True)
        self.bus.subscribe_topic(MessageType.VERIFY_REQUEST, self._pool, consumes=True)
        self.bus.subscribe_topic(MessageType.WORKER_INPUT, self._worker_input, consumes=True)
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


# --- Planning ---


@pytest.mark.usefixtures("agent_db")
class TestPlanning:
    def test_steps_run_in_dependency_order(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_project.return_value = {"id": "proj-1", "name": "calc", "description": "a tiny calculator",
                                             "work_dir": "/srv/calc"}
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

        accepted = next(e for e in h.statuses if e.message_type == MessageType.TASK_ACCEPTED)
        assert accepted.payload["step_count"] == 3
        (persisted,) = [call.args[1] for call in agent_db.upsert_steps.await_args_list]
        assert [row["instruction"] for row in persisted] == ["do s1", "do s2", "do s3"]
        phases = [call.args[1] for call in agent_db.update_task_phase.await_args_list]
        assert phases == ["planning", "executing", "completed"]

        first, *decisions = [turn["text"] for turn in claude.turns()]
        assert first.startswith("[PLAN]")
        assert "Task: build the calc package" in first
        assert "Project: calc: a tiny calculator" in first
        assert f"Working directory: {h.work_dir}" in first
        assert all(text.startswith("[DECISION]") for text in decisions)
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
        first, second, third = (item.payload["instruction"] for item in h.work_items)
        assert first == "do s1"
        assert "attempt 2" in second and "- fix the import" in second
        assert "attempt 3" in third and "- fix the import\n- add the sub function" in third
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

    def test_claude_runs_read_only_in_the_task_directory(
        self, planner_claude: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from loopx.db import agent_configs

        async def _config(agent_id: str) -> dict[str, Any]:
            return {"system_prompt": "You plan carefully.", "timeout_seconds": 60, "model": "claude-test",
                    "max_turns": 9}

        monkeypatch.setattr(agent_configs, "get_config", _config)
        claude = planner_claude(plan(step("s1")), decide("accept"))
        drive(claude, always(WORK_OK))

        (argv,) = claude.argvs()
        assert "--dangerously-skip-permissions" not in argv
        assert _flag(argv, "--tools") == "Read,Grep,Glob"
        assert _flag(argv, "--allowedTools") == "Read,Grep,Glob"
        assert _flag(argv, "--permission-mode") == "dontAsk"
        assert "--strict-mcp-config" in argv
        assert _flag(argv, "--input-format") == "stream-json"
        assert _flag(argv, "--output-format") == "stream-json"
        assert (_flag(argv, "--model"), _flag(argv, "--max-turns")) == ("claude-test", "9")
        prompt = _flag(argv, "--append-system-prompt")
        assert prompt == f"You plan carefully.\n\n{REPLY_CONTRACT}"
        # Every turn arrives on stdin; the command line carries no prompt.
        takes_value = {"--input-format", "--output-format", "--tools", "--allowedTools", "--permission-mode",
                       "--append-system-prompt", "--model", "--max-turns"}
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
