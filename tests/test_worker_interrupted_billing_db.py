"""Reported Worker usage survives interruption and keeper replay on the same row."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from looper.agents.agent_settings import AgentType
from looper.agents.claude_cli import ClaudeRunRecorder, stream_events
from looper.agents.run_usage import Usage
from looper.agents.runtimes.codex import CodexStream
from looper.agents.runtimes.pricing import pricer, usage_event
from looper.db import llm_records
from tests.db_support import DSN, run_db
from tests.runtime_model_helpers import builtin_launch

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def _recorder(launch):
    return ClaudeRunRecorder(agent_id="worker-billing", task_id="billing-task", command=["stand-in"],
                             cwd=".", input_text="work", launch=launch, whole_process=True)


def _claude_launch():
    return builtin_launch(role=AgentType.WORKER)


def test_interrupted_codex_bills_two_completed_turns(monkeypatch):
    from tests.test_codex_runtime import _launch

    async def body(pool):
        launch = replace(_launch(), role=AgentType.WORKER)
        recorder = _recorder(launch)
        stream = CodexStream(model=launch.model)
        await recorder.start()
        for tokens in ((100, 20, 10), (300, 70, 40)):
            for event in stream.translate({"type": "turn.completed", "usage": {
                    "input_tokens": tokens[0], "cached_input_tokens": tokens[1], "output_tokens": tokens[2]}}):
                await recorder.record(event)
                live = await llm_records.get_record(recorder.record_id)
                assert live["input_tokens"] == tokens[0] - tokens[1]
        # Simulate loss of the controller: only the DB close path remains.
        recorder.detach()
        await llm_records.end_record_interrupted(recorder.record_id, "drain deadline")
        row = await llm_records.get_record(recorder.record_id)
        expected = Usage(input_tokens=230, output_tokens=40, cache_read_tokens=70, cache_creation_tokens=0)
        assert [row[k] for k in expected.to_dict()] == list(expected.to_dict().values())
        assert row["cost_usd"] == pytest.approx(launch.price(launch.model, expected))
        assert row["cost_source"] == "provider_price"
        assert row["metadata"]["usage_status"] == "partial"
        before = await pool.fetchrow("SELECT * FROM llm_records WHERE id=$1", recorder.record_id)
        await llm_records.end_record_interrupted(recorder.record_id, "retry")
        assert await pool.fetchrow("SELECT * FROM llm_records WHERE id=$1", recorder.record_id) == before
    run_db(body, monkeypatch)


@pytest.mark.parametrize("close", ["abandoned", "cancelled", "startup"])
def test_interrupted_claude_bills_reported_messages(monkeypatch, close):
    async def body(pool):
        launch = _claude_launch()
        recorder = _recorder(launch)
        await recorder.start()
        reader = asyncio.StreamReader()
        messages = [{"type": "assistant", "message": {"id": f"message-{i}", "model": "claude-opus-5-5",
                     "usage": {"input_tokens": 100, "output_tokens": 20,
                               "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10}}}
                    for i in range(2)]
        reader.feed_data("\n".join(json.dumps(e) for e in [*messages, messages[-1]]).encode())
        reader.feed_eof()
        async for event in stream_events(reader):
            await recorder.record(event)
        if close == "cancelled":
            await recorder.end_left_open(asyncio.CancelledError())
        else:
            recorder.detach()
            if close == "startup":
                assert await llm_records.end_unfinished_records() == 1
            else:
                await llm_records.end_record_interrupted(recorder.record_id, "cannot adopt")
        row = await llm_records.get_record(recorder.record_id)
        expected = Usage(input_tokens=200, output_tokens=40, cache_read_tokens=60, cache_creation_tokens=20)
        assert [row[k] for k in expected.to_dict()] == list(expected.to_dict().values())
        assert row["cost_usd"] == pytest.approx(pricer(launch.settings.resolved_model)("claude-opus-5-5", expected))
        assert row["cost_source"] == "provider_price"
        assert row["metadata"]["usage_status"] == "partial"
    run_db(body, monkeypatch)


@pytest.mark.parametrize("empty_event", [False, True])
def test_interrupted_without_usage_keeps_null_and_reason(monkeypatch, empty_event):
    async def body(pool):
        recorder = _recorder(_claude_launch())
        await recorder.start()
        if empty_event:
            await recorder.record(usage_event(None, Usage()))
        recorder.detach()
        await llm_records.end_record_interrupted(recorder.record_id, "cannot adopt")
        row = await llm_records.get_record(recorder.record_id)
        assert all(row[k] is None for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                           "cache_creation_tokens", "cost_usd"))
        assert row["metadata"]["usage_status"] == "unreported"
        assert row["metadata"]["usage_reason"] == "no_usage_reported_before_interruption"
    run_db(body, monkeypatch)


def test_keeper_replay_and_final_bill_replace_live_snapshot_once(monkeypatch):
    async def body(pool):
        launch = _claude_launch()
        recorder = _recorder(launch)
        event = usage_event("claude-opus-5-5", Usage(input_tokens=100, output_tokens=10))
        await recorder.start()
        await recorder.record(event)
        partial = await llm_records.get_record(recorder.record_id)
        assert partial["input_tokens"] == 100 and partial["cost_usd"] > 0
        recorder.detach()
        resumed = await ClaudeRunRecorder.resume(recorder.record_id, agent_id="worker-billing",
            task_id="billing-task", command=["stand-in"], cwd=".", step_id=None, launch=launch, max_budget_usd=None)
        resumed.account(event, replayed=True)
        await resumed.record(event)
        result = {"type": "result", "subtype": "success", "total_cost_usd": .125,
                  "modelUsage": {"claude-opus-5-5": {"inputTokens": 200, "outputTokens": 20,
                                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}}
        await resumed.record(result)
        await resumed.finish(output_text="done", result_event=result)
        await resumed.finish(output_text="done", result_event=result)
        assert await pool.fetchval("SELECT count(*) FROM llm_records") == 1
        row = await llm_records.get_record(recorder.record_id)
        assert (row["status"], row["input_tokens"], row["cost_usd"], row["cost_source"]) == (
            "completed", 200, .125, "runtime")
        assert "usage_status" not in row["metadata"] and "usage_reason" not in row["metadata"]
        assert (await llm_records.task_costs(["billing-task"]))["billing-task"]["cost_usd"] == .125
    run_db(body, monkeypatch)


def test_daily_spend_and_cost_cap_include_partial_row(monkeypatch):
    from looper.agents import cost_guard as cg
    from looper.agents.cost_guard_settings import Config, Mode

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('billing-project','Billing test')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement) "
                           "VALUES ('billing-task','billing-project','test')")
        recorder = _recorder(_claude_launch())
        await recorder.start()
        await recorder.record(usage_event("claude-opus-5-5", Usage(input_tokens=100_000)))
        recorder.detach()
        await llm_records.end_record_interrupted(recorder.record_id, "cannot adopt")
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        spend = await llm_records.spend_since(since)
        assert spend["equivalent_usd"] == pytest.approx(.4)
        guard = cg.Guard(Config(mode=Mode.ENFORCE, daily_equivalent_usd=.3), pool=pool)
        await guard.rebuild()
        scopes = guard.scopes(cg.RunKind.WORKER, "billing-task", None, cg.Currency.EQUIVALENT)
        admission = await guard.admit(cg.RunKind.WORKER, scopes, cg.Currency.EQUIVALENT, .01)
        assert admission.decision is cg.Decision.ASK and admission.spent_usd == pytest.approx(.4)
    run_db(body, monkeypatch)


@pytest.mark.parametrize("reported_cost", [None, .25])
def test_partial_bill_uses_runtime_total_then_prices_later_requests(monkeypatch, reported_cost):
    async def body(pool):
        recorder = _recorder(_claude_launch())
        await recorder.start()
        event = usage_event("claude-opus-5-5", Usage(input_tokens=100_000))
        await recorder.record(event)
        if reported_cost is not None:
            await recorder.record({"type": "result", "subtype": "success", "total_cost_usd": reported_cost,
                "modelUsage": {"claude-opus-5-5": {"inputTokens": 100_000}}})
        await recorder.record(event)
        await recorder.end_left_open(asyncio.CancelledError())
        row = await llm_records.get_record(recorder.record_id)
        assert row["input_tokens"] == 200_000
        assert row["cost_usd"] == pytest.approx((reported_cost if reported_cost is not None else .4) + .4)
        assert row["cost_source"] == ("runtime" if reported_cost is not None else "provider_price")
    run_db(body, monkeypatch)


def test_unpriced_reported_usage_remains_partial_with_unknown_cost(monkeypatch):
    async def body(pool):
        recorder = _recorder(_claude_launch())
        await recorder.start()
        await recorder.record(usage_event("unpriced-model", Usage(input_tokens=100)))
        recorder.detach()
        await llm_records.end_record_interrupted(recorder.record_id, "cannot adopt")
        row = await llm_records.get_record(recorder.record_id)
        assert row["input_tokens"] == 100 and row["cost_usd"] is None
        assert row["metadata"]["usage_status"] == "partial"
        assert "usage_reason" not in row["metadata"]
    run_db(body, monkeypatch)


def test_incomplete_stream_still_bills_reported_complete_requests(monkeypatch):
    async def body(pool):
        recorder = _recorder(_claude_launch())
        await recorder.start()
        await recorder.record(usage_event("claude-opus-5-5", Usage(input_tokens=100_000),
                                         usage_complete=False, cost_basis="observed", root_usage_complete=True))
        recorder.detach()
        await llm_records.end_record_interrupted(recorder.record_id, "cannot adopt")
        row = await llm_records.get_record(recorder.record_id)
        assert row["input_tokens"] == 100_000 and row["cost_usd"] == .4
        assert row["metadata"]["usage_status"] == "partial"
    run_db(body, monkeypatch)


def test_normal_settlement_is_identical_with_and_without_checkpoints(monkeypatch):
    from looper.agents import claude_cli

    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.)

    async def body(pool):
        rows = []
        for enabled in (False, True):
            recorder = _recorder(_claude_launch())
            if not enabled:
                recorder._checkpoint_spend = None
            await recorder.start()
            await recorder.record(usage_event("claude-opus-5-5", Usage(input_tokens=100, output_tokens=10)))
            result = {"type": "result", "subtype": "success", "total_cost_usd": .123,
                      "modelUsage": {"claude-opus-5-5": {"inputTokens": 100}}}
            await recorder.record(result)
            await recorder.finish(output_text="done", result_event=result)
            rows.append(await pool.fetchval("SELECT (to_jsonb(r) - ARRAY['id','created_at','finished_at',"
                                           "'last_output_at'])::text FROM llm_records r WHERE id=$1", recorder.record_id))
        assert rows[0] == rows[1]
    run_db(body, monkeypatch)
