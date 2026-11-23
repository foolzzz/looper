"""A Planner turn claims the explorer results attached to it only when its message reached the runtime."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import claude_cli
from looper.agents.project_execution import ProjectExecution
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import planner
from test_ttadk_runtime import _launch


@pytest.mark.parametrize("resuming,fields,claimed", [
    ("old-session", {"session_started": False, "prompt_sent": False}, False),  # failed before prompting: retried
    (None, {"session_started": True, "prompt_sent": True}, True),              # sent and answered: claimed once
])
def test_results_are_claimed_only_when_the_turn_was_sent(
        resuming: str | None, fields: dict[str, bool], claimed: bool, tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
    marked = AsyncMock()
    monkeypatch.setattr(planner.subagent_delivery, "planner_turn_note", AsyncMock(return_value=("\nNOTE", [7])))
    monkeypatch.setattr(planner.subagent_delivery, "mark_turn_delivered", marked)

    class Claude:
        command, started, pid, stderr, totals = ["claude"], True, 1, "", {}

        async def run_turn(self, text: str, on_event: Any, stop: Any = None) -> claude_cli.ResidentTurn:
            assert text.endswith("NOTE")
            result = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
            return claude_cli.ResidentTurn(result, **fields)

        async def stop(self) -> None:
            pass

    async def run() -> None:
        agent = planner.PlannerAgent(task_id="claim", runtime_root=tmp_path)
        agent._plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path))
        agent._execution = ProjectExecution(subagents_on=True)
        agent._claude = Claude()
        process = planner._Process(["claude"], None, SystemPrompt("x"), _launch())
        try:
            await agent._exchange("hello", process, 5, resuming=resuming)
        except planner._SessionNotResumed:
            pass

    asyncio.run(asyncio.wait_for(run(), 10))
    assert marked.await_count == (1 if claimed else 0)
    if claimed:
        marked.assert_awaited_once_with([7])
