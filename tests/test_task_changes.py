"""A task's changes reach its Planner: supplements and requirement edits, each in a turn, never lost.

The changes are stored first (``db.task_changes``, here the in-memory store
of ``agent_db``); the Planner takes every change waiting into one CHAT turn,
after the turn it was in, and records them delivered only once that turn's
reply is done. A failed turn or a restart leaves them for the next turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, planner_address
from looper.agents.tiers.planner import PlannerAgent
from looper.db.task_changes import RequirementEdit
from tests.test_planner_agent import WORK_OK, Harness, activity, chat, decide, drive, hold_first, plan, step

SUPPLEMENT = "导出改用 CSV，不要 JSON。"


def _texts(claude: Any) -> list[str]:
    return [turn["text"] for turn in claude.turns()]


def _pending(agent_db: Any) -> list[str]:
    return [entry["summary"] for entry in agent_db.store.journal.get("t1", [])
            if entry["kind"] == "supplement" and not entry["delivered"]]


async def _change(h: Harness, agent_db: Any, *, text: str = SUPPLEMENT, nudge: bool = True) -> None:
    """Store a supplement the way the platform does, then (``nudge``) wake the Planner."""
    await agent_db.store.add_supplement("t1", text, via="manager:web")
    if nudge:
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=planner_address("t1"), message_type=MessageType.TASK_CHANGE,
            payload={"task_id": "t1"}))


@pytest.mark.usefixtures("agent_db")
class TestDelivery:
    def test_a_change_waiting_when_a_turn_ends_is_given_in_the_next_turn(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        """Stored while s1 ran, with no wake-up at all: the turn after s1's decision gives it, and s2, started in
        that event, already lists it."""
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept"), chat("好的，改用 CSV。"),
                                decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db, nudge=False)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        texts = _texts(claude)
        assert texts[0].startswith("[PLAN]") and texts[1].startswith("[DECISION] Step s1")
        assert texts[2].startswith("[CHAT] The task changed.")
        assert SUPPLEMENT in texts[2] and "Allowed actions: none, add_steps, replan" in texts[2]
        assert _pending(agent_db) == []
        (s2,) = [w.payload for w in h.work_items if w.payload["step_key"] == "s2"]
        assert s2["supplements"] == [SUPPLEMENT]

    def test_a_change_during_a_turn_waits_for_it_and_a_failed_turn_leaves_it_for_the_next(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), "<error>", decide("accept"),
                                chat("好的，改用 CSV。"), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)  # the change turn, which fails
            await h.until(lambda: any("could not take a change" in line for line in activity(agent_db)))
            assert _pending(agent_db) == [SUPPLEMENT]
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(), scenario)
        texts = _texts(claude)
        assert texts[1].startswith("[CHAT] The task changed.") and texts[2].startswith("[DECISION] Step s1")
        assert texts[3].startswith("[CHAT] The task changed.") and SUPPLEMENT in texts[3]
        assert _pending(agent_db) == []

    def test_a_restarted_planner_gives_the_change_it_had_not(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), "<error>", chat("好的，改用 CSV。"), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: any("could not take a change" in line for line in activity(agent_db)))
            await h.planner.stop()  # the server stops before the change was delivered
            h.held.clear()
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
                payload={"task_id": "t1"}, correlation_id="corr-1"))
            await h.until(lambda: len(claude.turns()) == 3 and h.held)
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(None), scenario)  # type: ignore[arg-type]
        texts = _texts(claude)
        assert texts[2].startswith("[CHAT] The task changed.") and SUPPLEMENT in texts[2]
        assert _pending(agent_db) == []

    def test_changes_waiting_together_go_in_one_turn_and_the_new_requirement_reaches_later_steps(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), chat("明白。"), decide("accept"),
                                decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            agent_db.store.requirements["t1"] = RequirementEdit(2, "build the calc package, with a CLI", 1,
                                                                "build the calc package")
            await agent_db.store.add_supplement("t1", "也要支持减法。", via="chat")
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        change = _texts(claude)[1]
        assert change.count("[CHAT] The task changed.") == 1
        assert "也要支持减法。" in change and SUPPLEMENT in change
        assert "The requirement was edited (version 1 -> 2)" in change and "+build the calc package, with a CLI" in change
        # The new text is the system prompt's requirement from that turn on, and the later Worker's.
        assert "build the calc package, with a CLI" in claude.prompts()[-1]
        (s2,) = [w.payload for w in h.work_items if w.payload["step_key"] == "s2"]
        assert s2["requirement"] == "build the calc package, with a CLI"
        assert s2["supplements"] == ["也要支持减法。", SUPPLEMENT]
        assert _pending(agent_db) == [] and "t1" not in agent_db.store.requirements

    def test_the_plan_turn_gets_the_changes_made_before_it(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))

        async def scenario(h: Harness) -> None:
            await agent_db.store.add_supplement("t1", SUPPLEMENT, via="manager:web")
            agent_db.store.requirements["t1"] = RequirementEdit(2, "build calc, CSV export", 1, "build calc")
            await h.start()
            await h.finished()

        h = drive(claude, always_ok(), scenario)
        (planned, decided) = _texts(claude)
        assert planned.startswith("[PLAN]") and SUPPLEMENT in planned
        assert "build calc, CSV export" in claude.prompts()[0]
        assert h.work_items[0].payload["requirement"] == "build calc, CSV export"
        assert _pending(agent_db) == [] and "t1" not in agent_db.store.requirements
        assert not any(text.startswith("[CHAT]") for text in _texts(claude))


@pytest.mark.usefixtures("agent_db")
class TestChatReplan:
    def test_replan_stops_the_running_steps_it_names_and_replaces_the_ones_not_started(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),
            chat("改用 CSV：停掉 s1，改成 s3。", "replan", steps=[step("s3")], stop=["s1"]),
            decide("accept"),
        )

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (killed,) = h.killed
        assert killed.target == h.work_items[0].target  # s1's Worker
        assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s3"]
        saved = {s["key"]: s["status"] for s in agent_db.store.steps["t1"].values()}
        assert saved == {"s1": "skipped", "s2": "skipped", "s3": "verified"}
        assert any(e["kind"] == "decision" and "replan at a change to the task" in e["summary"]
                   for e in agent_db.store.journal["t1"])

    def test_replan_keeps_running_steps_it_does_not_name(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),
            chat("s1 保留，s2 改成 s3。", "replan", steps=[step("s3", "s1")]),
            decide("accept"), decide("accept"),
        )

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        assert h.killed == []
        assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s3"]
        saved = {s["key"]: s["status"] for s in agent_db.store.steps["t1"].values()}
        assert saved == {"s1": "verified", "s2": "skipped", "s3": "verified"}


def always_ok() -> Any:
    return lambda envelope: WORK_OK
