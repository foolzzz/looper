"""Decision snapshots on the real Planner event path with scripted agents: off, nothing is written and nothing the
models see changes; on, every dispatch pass and step decision is recorded, and each replays."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from looper.agents.tiers import decisions
from looper.agents.tiers.decisions import replay
from looper.db import decision_snapshots as db_snapshots
from tests.test_planner_agent import WORK_FAILED, WORK_OK, decide, drive, plan, step


def _snapshots(monkeypatch: pytest.MonkeyPatch, agent_db: Any, *, on: bool) -> list[dict[str, Any]]:
    """Turn snapshots on or off, and keep what would be written, as the database hands it back."""
    rows: list[dict[str, Any]] = []
    agent_db.get_settings.return_value = {db_snapshots.ENABLED_KEY: "true"} if on else {}

    async def add(task_id: str, kind: str, *, inputs: Any, decision: Any, step_id: str | None = None,
                  record_id: int | None = None, db: Any = None) -> int:
        rows.append({"id": len(rows) + 1, "task_id": task_id, "kind": kind, "step_id": step_id,
                     "record_id": record_id, "inputs": json.loads(json.dumps(inputs)),
                     "decision": json.loads(json.dumps(decision))})
        return len(rows)

    async def turn(record_id: int, db: Any = None) -> dict[str, Any]:
        # The run record the turn opened: its metadata as the recorder wrote it.
        started = agent_db.start_record.await_args_list[-1].kwargs
        return {"id": record_id, "metadata": started["metadata"], "input_chars": len(started["input_text"]),
                "input_tokens": 10, "context_tokens": 20, "cache_read_tokens": 0}

    monkeypatch.setattr(db_snapshots, "add", add)
    monkeypatch.setattr(db_snapshots, "turn", turn)
    # The product bound is a production choice (a snapshot never holds a decision long); these runs must record,
    # so a loaded machine's scheduling delay must not turn into "snapshots are off".
    monkeypatch.setattr(decisions, "TIMEOUT_SECONDS", 30.0)
    return rows


def test_a_slow_read_of_the_switch_is_not_an_off_switch(agent_db, monkeypatch):
    async def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(1.2)  # longer than the production bound, as a loaded machine can be
        return {db_snapshots.ENABLED_KEY: "true"}

    _snapshots(monkeypatch, agent_db, on=True)
    monkeypatch.setattr(db_snapshots.system_settings, "get_settings", slow)
    assert asyncio.run(decisions.enabled()) is True


def _first_attempt_fails() -> Any:
    seen: dict[str, int] = {}

    def outcome(envelope: Any) -> dict[str, Any]:
        key = envelope.payload["step_key"]
        seen[key] = seen.get(key, 0) + 1
        return WORK_FAILED if key == "s1" and seen[key] == 1 else WORK_OK

    return outcome


def _run(planner_claude: Any) -> tuple[Any, Any]:
    claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("retry", feedback="cover the empty input"),
                            decide("accept", summary="s1 done"), decide("accept", summary="s2 done"))
    return claude, drive(claude, _first_attempt_fails())


def _seen(claude: Any, h: Any) -> dict[str, Any]:
    """What the models saw, with this run's directories and generated ids named alike; a prompt file is named by
    its text's hash, which holds the run's directory, and the prompts are compared themselves."""
    def same(text: str) -> str:
        text = text.replace(str(h.work_dir.parent), "<root>").replace(str(claude.state), "<claude>")
        text = re.sub(r"/prompts/[0-9a-f]{64}\.md", "/prompts/<sha>.md", text)
        return re.sub(r"\b(step|worker|reviewer|plan)-[0-9a-f]{8,12}\b", r"\1-<id>", text)

    return {"turns": [same(turn["text"]) for turn in claude.turns()],
            "argv": [[same(arg) for arg in argv] for argv in claude.argvs()],
            "prompts": [same(prompt) for prompt in claude.prompts()],
            "work": [same(json.dumps(item.payload, sort_keys=True)) for item in h.work_items]}


def test_off_writes_nothing_and_on_changes_nothing_the_models_see(planner_claude, agent_db, monkeypatch):
    rows = _snapshots(monkeypatch, agent_db, on=False)
    claude, h = _run(planner_claude)
    assert h.final().payload["status"] == "completed"
    assert rows == []
    off = _seen(claude, h)
    rows = _snapshots(monkeypatch, agent_db, on=True)
    claude, h = _run(planner_claude)
    assert h.final().payload["status"] == "completed"
    assert rows
    assert _seen(claude, h) == off


def test_on_records_each_dispatch_and_decision_and_each_replays(planner_claude, agent_db, monkeypatch):
    rows = _snapshots(monkeypatch, agent_db, on=True)
    claude, h = _run(planner_claude)
    assert h.final().payload["status"] == "completed"
    labels = {s["id"]: s["key"] for row in rows if row["kind"] == "dispatch" for s in row["inputs"]["steps"]}
    dispatches = [[labels[s] for s in row["decision"]["start"]] for row in rows if row["kind"] == "dispatch"]
    assert dispatches[:2] == [["s1"], ["s2"]] and all(start == [] for start in dispatches[2:])
    decisions = [row for row in rows if row["kind"] == "step_decision"]
    assert [(labels[row["step_id"]], row["inputs"]["attempt"], row["decision"]["decision"], row["decision"]["reason"])
            for row in decisions] == [("s1", 1, "retry", "cover the empty input"), ("s1", 2, "accept", ""),
                                      ("s2", 1, "accept", "")]
    assert decisions[0]["decision"]["allowed"] == ["retry", "replan", "fail"]
    assert decisions[1]["decision"]["allowed"] == ["accept", "review", "retry", "replan", "fail"]
    turn = decisions[0]["inputs"]["turn"]
    assert turn["record_id"] == decisions[0]["record_id"] == 1
    assert turn["model"] and turn["system_prompt_sha256"] and turn["prompt_layers"]["contract"]["sha256"]
    assert turn["sizes"]["input_chars"] > 0
    assert all(replay(row).same for row in rows), [replay(row) for row in rows if not replay(row).same]


def test_an_adjudicated_accept_records_what_the_turn_was_offered(planner_claude, agent_db, monkeypatch):
    from tests.test_planner_agent import REVIEW_PASS
    from tests.test_review_panel_planner import BLOCK, configure, held_reviews

    configure(agent_db, monkeypatch, adjudication=True)
    rows = _snapshots(monkeypatch, agent_db, on=True)
    judgment = {"member": "review_a", "finding": 0, "level": "suggestion", "reason": "Already covered by validation"}
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("accept", adjudications=[judgment]))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        await h.release(REVIEW_PASS)
        await h.finished()

    drive(claude, held_reviews, scenario)
    [accepted] = [row for row in rows if row["kind"] == "step_decision" and row["decision"]["decision"] == "accept"]
    assert all(replay(row).same for row in rows), [replay(row) for row in rows if not replay(row).same]
    assert accepted["inputs"]["adjudicated_accept"] is True and "accept" in accepted["decision"]["allowed"]
