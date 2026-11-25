"""A Planner turn claims the explorer results attached to it exactly when the runtime sends its prompt."""
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

OK = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}


async def _hang(cancel: Any) -> claude_cli.ResidentTurn:
    await asyncio.sleep(30)
    raise AssertionError


async def _boom(cancel: Any) -> claude_cli.ResidentTurn:
    raise RuntimeError("read failed")


async def _answer(cancel: Any) -> claude_cli.ResidentTurn:
    return claude_cli.ResidentTurn(OK, session_started=True, prompt_sent=True)


async def _unsent(cancel: Any) -> claude_cli.ResidentTurn:
    # Failed before prompting (a tool listing failure, a lost runner): the session may even have started.
    return claude_cli.ResidentTurn({**OK, "is_error": True, "subtype": "error_during_execution"},
                                   session_started=True, prompt_sent=False)


@pytest.mark.parametrize("script,sends,expected_error,claimed", [
    (_hang, True, planner.PlannerTurnError, True),       # timeout after the prompt was sent
    (_boom, True, planner.PlannerTurnError, True),       # read failure after the prompt was sent
    (_answer, True, None, True),                          # a normal turn claims once
    (_unsent, False, planner.PlannerTurnError, False),                       # failed before sending: results ride the next turn
    (_boom, False, planner.PlannerTurnError, False),      # raised before sending
])
def test_results_are_claimed_when_the_prompt_is_sent_and_only_then(
        script: Any, sends: bool, expected_error: Any, claimed: bool, tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
    marked = AsyncMock()
    monkeypatch.setattr(planner.subagent_delivery, "planner_turn_note", AsyncMock(return_value=("\nNOTE", [7])))
    monkeypatch.setattr(planner.subagent_delivery, "mark_turn_delivered", marked)

    prompts = []

    class Claude:
        command, started, pid, stderr, totals = ["claude"], True, 1, "", {}

        async def run_turn(self, text: str, on_event: Any, stop: Any = None, cancel: Any = None):
            prompts.append(text)
            if sends:
                cancel.sent()  # what every runtime does at the moment it sends the prompt
            return await script(cancel)

        async def stop(self) -> None:
            pass

    async def run() -> None:
        agent = planner.PlannerAgent(task_id="claim", runtime_root=tmp_path)
        agent._plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path))
        agent._execution = ProjectExecution(subagents_on=True)
        agent._claude = Claude()
        process = planner._Process(["claude"], None, SystemPrompt("x"), _launch())
        limit = 0.3 if script is _hang else 5
        if expected_error:
            with pytest.raises(expected_error):
                await agent._exchange("hello", process, limit)
        else:
            await agent._exchange("hello", process, limit)

    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(prompts) == 1 and prompts[0].endswith("NOTE")
    assert marked.await_count == (1 if claimed else 0)
    if claimed:
        marked.assert_awaited_once_with([7])


def test_nothing_to_claim_passes_no_cancel_to_the_runtime() -> None:
    from looper.agents.subagent_delivery import claim_when_sent

    kwargs, settled = claim_when_sent([])
    assert kwargs == {}
    asyncio.run(settled())
