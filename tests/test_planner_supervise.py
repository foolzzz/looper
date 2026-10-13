"""The Planner supervises the Workers running now: on a timer it looks at each
running step and, without waiting for a result, steers one that drifts (nudge)
or stops one whose direction is wrong (kill), then decides what replaces it.

These tests drive ``_supervise`` directly after a Worker is held running, and
cover the timer and the self-posted supervise tick end to end. Assertions about
what supervision did run inside the scenario, because stopping the Planner at
the end of ``drive`` itself stops the Workers the task still runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from loopx.agents.message import MessageEnvelope, MessageType, planner_address
from loopx.agents.tiers.planner import PlannerSettings

from tests.test_planner_agent import Harness, always, decide, drive, plan, reply, step

pytestmark = pytest.mark.usefixtures("agent_db")


def supervise(*reviews: dict[str, Any]) -> str:
    return reply({"reviews": list(reviews)})


async def _one_step_held(h: Harness, *, requirement: str = "build it") -> None:
    """Start the task; its single step s1 runs in a Worker that is held open."""
    await h.start(requirement)
    await h.until(lambda: len(h.held) == 1)


def test_nudge_steers_the_running_worker(planner_claude: Any) -> None:
    claude = planner_claude(
        plan(step("s1")),
        supervise({"step": "s1", "action": "nudge", "text": "use the public API"}),
    )

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        worker = h.held[0].target
        await h.planner._supervise()
        assert len(h.forwarded) == 1 and not h.killed
        assert h.forwarded[0].target == worker  # the Worker still running s1
        assert "use the public API" in h.forwarded[0].payload["text"]

    drive(claude, always(None), scenario)


def test_kill_then_retry_redispatches_the_step(planner_claude: Any) -> None:
    claude = planner_claude(
        plan(step("s1")),
        supervise({"step": "s1", "action": "kill", "reason": "editing the wrong file"}),
        decide("retry", feedback="edit calc.py instead"),
    )

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        first_worker = h.work_items[0].target
        await h.planner._supervise()
        # The kill stops the first Worker and the retry starts a fresh one.
        await h.until(lambda: len(h.work_items) == 2)
        assert len(h.killed) == 1
        assert h.work_items[1].target != first_worker
        assert h.work_items[1].payload["feedback"] == ["edit calc.py instead"]

    drive(claude, always(None), scenario)


def test_kill_then_fail_ends_the_task(planner_claude: Any) -> None:
    claude = planner_claude(
        plan(step("s1")),
        supervise({"step": "s1", "action": "kill", "reason": "the approach cannot work"}),
        decide("fail", reason="no viable approach"),
    )

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        await h.planner._supervise()
        await h.finished()
        assert len(h.killed) == 1

    h = drive(claude, always(None), scenario)
    final = h.final()
    assert final is not None and final.payload["status"] == "failed"


def test_empty_review_leaves_everything_running(planner_claude: Any) -> None:
    claude = planner_claude(plan(step("s1")), supervise())

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        await h.planner._supervise()
        assert not h.forwarded and not h.killed  # nothing steered or stopped
        assert len(h.held) == 1  # s1 still running

    drive(claude, always(None), scenario)


def test_a_supervise_tick_runs_a_supervise_turn(planner_claude: Any) -> None:
    """A SYSTEM_EVENT supervise tick (what the timer posts) runs a SUPERVISE turn."""
    claude = planner_claude(plan(step("s1")), supervise())

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        await h.bus.publish(MessageEnvelope.create(
            source=planner_address("t1"), target=planner_address("t1"),
            message_type=MessageType.SYSTEM_EVENT, payload={"type": "supervise"},
        ))
        await h.until(lambda: any(t["text"].startswith("[SUPERVISE]") for t in claude.turns()))

    drive(claude, always(None), scenario)


def test_the_timer_posts_a_tick_while_a_worker_runs(planner_claude: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a 1 s interval, the Planner supervises a held Worker on its own."""
    from loopx.agents.tiers import planner as planner_module

    async def fast_settings() -> PlannerSettings:
        return PlannerSettings(
            system_prompt="You are the Planner.", timeout_seconds=0, model=None,
            max_turns=0, supervise_interval_seconds=1,
        )

    monkeypatch.setattr(planner_module, "load_settings", fast_settings)
    claude = planner_claude(plan(step("s1")), supervise(), supervise(), supervise())

    async def scenario(h: Harness) -> None:
        await _one_step_held(h)
        await h.until(
            lambda: any(t["text"].startswith("[SUPERVISE]") for t in claude.turns()),
            timeout=8.0,
        )

    drive(claude, always(None), scenario)
