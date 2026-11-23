"""The runs going on now and their tokens per minute: a recorder lists its run from its start until it closes, every
usage event it takes counts once in the run's last minute, and a runtime that reports usage only when a turn ends has
no rate of its own while it runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from looper.agents import live_runs
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.pricing import usage_event


def _launch(**fields: Any) -> Any:
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import builtin_resolved_profile

    return Launch(AgentSettings(profile=builtin_resolved_profile()), role=AgentType.WORKER, **fields)


def test_a_window_holds_every_token_kind_of_the_last_minute() -> None:
    now = [1000.0]
    window = live_runs.TokenWindow(clock=lambda: now[0])
    window.add(Usage(input_tokens=10, output_tokens=5, cache_read_tokens=1000, cache_creation_tokens=200))
    now[0] += 30
    window.add(Usage(output_tokens=7))
    assert window.rate() == {"total": 1222, "input": 10, "output": 12, "cache_read": 1000, "cache_creation": 200}
    now[0] += 31  # the first event is 61 s old
    assert window.rate() == {"total": 7, "input": 0, "output": 7, "cache_read": 0, "cache_creation": 0}
    now[0] += 30
    assert window.empty


def test_nothing_older_than_the_window_is_kept_when_nobody_reads_it() -> None:
    now = [0.0]

    def run(agent_id: str) -> live_runs.LiveRun:
        made = live_runs.LiveRun(agent_id=agent_id, role="worker", task_id=None, step_id=None, runtime="Claude Code",
                                 model=None, streams_usage=True, started_at=datetime.now(timezone.utc),
                                 window=live_runs.TokenWindow(clock=lambda: now[0]))
        live_runs.track(made)
        return made

    first, second = run("worker-1"), run("worker-2")
    for _ in range(3):
        first.window.add(Usage(output_tokens=1))
        second.window.add(Usage(output_tokens=1))
    live_runs.untrack(first)
    now[0] += 61
    second.window.add(Usage(output_tokens=1))
    assert len(second.window._events) == 1  # the run's older events went as new ones came
    live_runs.untrack(second)
    assert live_runs._ended == [second]  # the run that ended a minute ago went when the next one ended


@pytest.mark.parametrize("close", ["end_left_open", "detach"])
def test_a_recorder_lists_its_run_until_it_closes_and_its_tokens_stay_in_the_total(agent_db: Any, close: str) -> None:
    from looper.agents.claude_cli import ClaudeRunRecorder

    recorder = ClaudeRunRecorder(agent_id="worker-a", task_id="task-a", command=["claude"], cwd="/tmp",
                                 input_text="work", step_id="step-a", launch=_launch(), whole_process=True,
                                 pid_of=lambda: 4242)
    tokens = Usage(input_tokens=3, output_tokens=40, cache_read_tokens=900, cache_creation_tokens=50)

    async def run() -> None:
        await recorder.start()
        [listed] = live_runs.running()
        assert (listed.agent_id, listed.role, listed.task_id, listed.runtime, listed.pid) == (
            "worker-a", "worker", "task-a", "Claude Code", 4242)
        recorder.account(usage_event("m", tokens))
        recorder.account(usage_event("m", tokens), replayed=True)  # a resumed run's output read again
        assert listed.tpm() == {"total": 993, "input": 3, "output": 40, "cache_read": 900, "cache_creation": 50}
        if close == "detach":
            recorder.detach()
        else:
            await recorder.end_left_open(None)
        assert live_runs.running() == []
        assert live_runs.total_rate(run.window for run in live_runs.counted())["total"] == 993

    asyncio.run(run())


def test_a_run_whose_runtime_reports_at_turn_end_counts_in_the_total_only() -> None:
    run = live_runs.LiveRun(agent_id="worker-c", role="worker", task_id="task-c", step_id=None, runtime="Codex",
                            model="gpt", streams_usage=False, started_at=datetime.now(timezone.utc))
    live_runs.track(run)
    run.window.add(Usage(input_tokens=100, output_tokens=20))
    assert run.tpm() is None
    assert live_runs.total_rate(r.window for r in live_runs.counted())["total"] == 120


def test_only_the_runtimes_that_report_usage_at_turn_end_have_no_rate_while_they_run() -> None:
    assert {kind for kind, runtime in ADAPTERS.items() if not runtime.streams_usage} == {"codex", "coco", "cursor-agent", "acp"}
