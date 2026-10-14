"""What a Claude run spent and how large its context grew, read from real stream-json.

The fixtures in ``fixtures/claude_stream`` are Claude Code 2.1.285 (haiku)
output, trimmed to the events that matter here and with local paths
redacted:

- ``multi_request_turns``: one resident process, a turn of three model
  requests (two Bash calls) and then a one-request turn;
- ``subagent_turn``: a turn that starts a background subagent; its
  notification makes Claude run a second turn by itself;
- ``manual_compact``: a turn, then ``/compact`` sent as a user turn, then a turn;
- ``auto_compact``: ``--autocompact 100k`` with ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=15``:
  one failed and two successful automatic compactions;
- ``resume``: two turns in one process, then two in a new process started
  with ``--resume`` on the same session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from looper.agents.claude_cli import ClaudeRunRecorder, ResidentClaude
from looper.agents.run_usage import Usage, compaction, reported_totals, request_context, turn_tokens

FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"


def _turns(name: str) -> list[list[dict[str, Any]]]:
    """The fixture's events, cut after each result: one list per turn."""
    turns: list[list[dict[str, Any]]] = [[]]
    for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines():
        event = json.loads(line)
        turns[-1].append(event)
        if event["type"] == "result":
            turns.append([])
    return [turn for turn in turns if turn]


def _result(turn: list[dict[str, Any]]) -> dict[str, Any]:
    return turn[-1]


def _context(turn: list[dict[str, Any]]) -> int | None:
    """The prompt size of the turn's last model request."""
    context = None
    for event in turn:
        size = request_context(event)
        if size is not None:
            context = size
    return context


def _compactions(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [found for event in turn if (found := compaction(event)) is not None]


def _tokens(usage: Usage) -> tuple[int | None, ...]:
    return (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_creation_tokens)


class TestTokensAndContext:
    def test_a_result_reports_the_processs_running_totals_over_every_request(self) -> None:
        first, second = _turns("multi_request_turns")
        # Three requests: 10+8+8 input, 13970+18328+18527 cache read, 4358+199+129 cache creation.
        assert reported_totals(_result(first)) == Usage(
            cost_usd=0.0157805, input_tokens=26, output_tokens=260, cache_read_tokens=50825,
            cache_creation_tokens=4686,
        )
        # modelUsage keeps counting across the process's turns, like total_cost_usd.
        totals = reported_totals(_result(second))
        assert (totals.cost_usd, _tokens(totals)) == (0.0179591, (36, 291, 69481, 4760))

    def test_a_turns_own_share_is_its_totals_less_the_earlier_ones(self) -> None:
        first, second = _turns("multi_request_turns")
        own = reported_totals(_result(second)).since(reported_totals(_result(first)))
        assert own.cost_usd == pytest.approx(0.0021786)
        assert _tokens(own) == (10, 31, 18656, 74)
        # Without compaction or subagents the turn's own result.usage says the same.
        assert _tokens(turn_tokens(_result(second))) == (10, 31, 18656, 74)

    def test_the_context_is_the_prompt_of_the_turns_last_request(self) -> None:
        first, second = _turns("multi_request_turns")
        assert _context(first) == 8 + 18527 + 129  # not the first request's 10 + 13970 + 4358
        assert _context(second) == 10 + 18656 + 74

    def test_a_subagents_requests_count_in_the_tokens_but_not_in_the_context(self) -> None:
        launched, notified = _turns("subagent_turn")
        assert any(e.get("parent_tool_use_id") for e in notified if e["type"] == "assistant")
        # The subagent's last request (8 + 11206 + 632) comes after the main loop's; it is not the context.
        assert _context(launched) == 8 + 18333 + 574
        assert _context(notified) == 10 + 18907 + 522
        own = reported_totals(_result(notified)).since(reported_totals(_result(launched)))
        # The subagent's two requests (10 + 8 input, 11206 + 632 cache creation) are in modelUsage only.
        assert _tokens(own) == (10 + 8 + 10, 538 - 276, 63546 - 33433, 11206 + 632 + 522)
        assert _tokens(turn_tokens(_result(notified))) == (10, 71, 18907, 522)

    def test_an_assistant_message_without_usage_is_not_a_request(self) -> None:
        assert request_context({"type": "assistant", "message": {"content": []}}) is None
        assert request_context({"type": "assistant", "message": {"usage": {"input_tokens": "x"}}}) is None
        assert request_context({"type": "user", "message": {"usage": {"input_tokens": 3}}}) is None


class TestCompaction:
    def test_compact_sent_as_a_user_turn_is_a_manual_compaction(self) -> None:
        before, compact, after = _turns("manual_compact")
        assert _compactions(before) == [] and _compactions(after) == []
        assert _compactions(compact) == [
            {"trigger": "manual", "pre_tokens": 18379, "post_tokens": 1285, "duration_ms": 12256},
        ]
        assert _context(compact) is None  # the /compact turn makes no model request of its own

    def test_the_compactions_own_request_is_in_the_totals_only(self) -> None:
        before, compact, _ = _turns("manual_compact")
        own = reported_totals(_result(compact)).since(reported_totals(_result(before)))
        assert own.cost_usd == pytest.approx(0.01705315 - 0.008248)
        assert _tokens(own) == (1446 - 10, 1154 - 64, 33404 - 15100, 3275 - 3204)
        assert _tokens(turn_tokens(_result(compact))) == (0, 0, 0, 0)  # what result.usage reports

    def test_automatic_compactions_are_recorded_and_a_failed_attempt_is_not(self) -> None:
        first, second, third = _turns("auto_compact")
        # The first turn tried and failed (too_few_groups): a status event, no boundary.
        assert any(e.get("compact_result") == "failed" for e in first)
        assert _compactions(first) == []
        assert _compactions(second) == [
            {"trigger": "auto", "pre_tokens": 25120, "post_tokens": 1359, "duration_ms": 13135},
        ]
        assert _compactions(third) == [
            {"trigger": "auto", "pre_tokens": 17457, "post_tokens": 1534, "duration_ms": 13156},
        ]
        # The context after a compaction is the next request's prompt.
        assert (_context(first), _context(second), _context(third)) == (
            8 + 18320 + 6703, 10 + 15100 + 2285, 10 + 15100 + 2382)
        # A boundary without metadata still counts; the "compacting" status alone does not.
        assert compaction({"type": "system", "subtype": "compact_boundary"}) == {}
        assert compaction({"type": "system", "subtype": "status", "status": "compacting"}) is None


class TestResumedSession:
    def test_a_counter_that_went_down_started_again(self) -> None:
        # Claude did not carry the session's totals: what it reports is all this turn's.
        assert Usage(cost_usd=0.2, input_tokens=5).since(Usage(cost_usd=1.0, input_tokens=50)) == Usage(
            cost_usd=0.2, input_tokens=5)

    def test_totals_round_trip_and_tolerate_junk(self) -> None:
        usage = Usage(cost_usd=0.5, input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=4)
        assert Usage.from_dict(json.loads(json.dumps(usage.to_dict()))) == usage
        assert Usage().to_dict() == {}
        assert Usage.from_dict(None) == Usage.from_dict("x") == Usage()
        assert Usage.from_dict({"cost_usd": "1", "input_tokens": True, "output_tokens": -1}) == Usage()


def _sleeper() -> list[str]:
    return [sys.executable, "-c", "import sys; sys.stdin.read()"]


class TestResidentClaudeBaseline:
    def _turn_costs(self, results: list[dict[str, Any]], totals: Usage | None) -> tuple[list[Usage], Usage]:
        async def _run() -> tuple[list[Usage], Usage]:
            claude = ResidentClaude(label="test")
            await claude.start(_sleeper(), cwd=".", signature=1, totals=totals)
            try:
                return [claude.turn_usage(result) for result in results], claude.totals
            finally:
                await claude.stop()

        return asyncio.run(_run())

    def test_a_resumed_process_starts_from_the_sessions_totals(self) -> None:
        first, second, resumed, after = _turns("resume")
        assert first[0]["session_id"] == resumed[0]["session_id"]
        session = reported_totals(_result(second))
        # A process started with --resume reports the session's whole spending, its earlier turns' too.
        assert reported_totals(_result(resumed)).cost_usd == pytest.approx(0.0127871)
        (own, then), totals = self._turn_costs([_result(resumed), _result(after)], session)
        assert own.cost_usd == pytest.approx(0.0127871 - 0.0104644)
        assert _tokens(own) == _tokens(turn_tokens(_result(resumed))) == (10, 58, 18407, 91)
        assert then.cost_usd == pytest.approx(0.0149809 - 0.0127871)
        assert totals == reported_totals(_result(after))

    def test_a_turn_without_modelusage_takes_its_tokens_from_its_own_usage(self) -> None:
        result = {"type": "result", "total_cost_usd": 0.75,
                  "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 6}}
        (own,), totals = self._turn_costs([result], Usage(cost_usd=0.5))
        assert own == Usage(cost_usd=0.25, input_tokens=3, output_tokens=4, cache_read_tokens=5,
                            cache_creation_tokens=6)
        assert totals == Usage(cost_usd=0.75)


class TestRecorder:
    def _record(self, agent_db: Any, turn: list[dict[str, Any]], usage: Usage | None = None) -> dict[str, Any]:
        async def _run() -> None:
            recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=["claude"], cwd=".",
                                         input_text="hi")
            await recorder.start()
            for event in turn:
                await recorder.record(event)
            await recorder.finish(output_text="ok", result_event=_result(turn), usage=usage)

        asyncio.run(_run())
        return agent_db.finish_record.await_args.kwargs

    def test_a_resident_turn_records_its_own_usage_context_and_compactions(self, agent_db: Any) -> None:
        first, second, _ = _turns("auto_compact")
        own = reported_totals(_result(second)).since(reported_totals(_result(first)))
        finish = self._record(agent_db, second, own)
        assert finish["cost_usd"] == pytest.approx(0.040975 - 0.018163)
        assert (finish["input_tokens"], finish["output_tokens"], finish["cache_read_tokens"],
                finish["cache_creation_tokens"]) == _tokens(own) == (8003 - 18, 1557 - 215, 70060 - 36640, 9152 - 6703)
        assert finish["context_tokens"] == 10 + 15100 + 2285
        assert finish["compactions"] == [
            {"trigger": "auto", "pre_tokens": 25120, "post_tokens": 1359, "duration_ms": 13135},
        ]
