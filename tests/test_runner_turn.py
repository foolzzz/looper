"""One trace's host prices, budget reservations and safe tool shutdown."""
import asyncio

import pytest

from looper.agents.run_usage import Usage
from looper.agents.runtimes.api.loop import ToolOutcome
from looper.agents.runtimes.pricing import ModelPrice
from looper.agents.runtimes.runner.protocol import ErrorCode, ProtocolError
from looper.agents.runtimes.runner.turn import RunnerTurn


class Tools:
    async def call(self, call):
        return ToolOutcome(call.arguments, False, True)


def turn(*, caps=None, price=True, **kwargs):
    async def limits():
        return caps if caps is not None else {"run_cap": None, "task_cap": None, "task_spent": 0}

    return RunnerTurn(session_id="s", trace_id="t", role=kwargs.pop("role", "planner"), tools=kwargs.pop("tools", Tools()),
                      provider_id="p", model="m", price=(lambda model, usage: ModelPrice(1, 2, 3, 4).cost(usage))
                      if price else None, limits=limits, **kwargs)


def authorize(request_id="r", **kwargs):
    return {"session_id": "s", "trace_id": "t", "request_id": request_id, "kind": "turn",
            "provider": "p", "model": "m", "estimated_input_tokens": 10, "max_output_tokens": 5, **kwargs}


def ended(request_id="r", **kwargs):
    return {"session_id": "s", "trace_id": "t", "request_id": request_id, "request_kind": "turn", "model": "m",
            "complete": True, "input_uncached": 2, "output": 1, "cache_read": 0, "cache_write": 0, **kwargs}


def settled(**kwargs):
    return {"session_id": "s", "trace_id": "t", "requests": 1, "usage_complete": True,
            "outcome": "completed", **kwargs}


@pytest.mark.parametrize("cap,allowed", [(0.000019, False), (0.00002, True), (0.000021, True)])
@pytest.mark.parametrize("cap_key", ["run_cap", "task_cap"])
def test_estimate_against_remaining_cap(cap, allowed, cap_key):
    async def test():
        caps = {"run_cap": None, "task_cap": None, "task_spent": 0.1 if cap_key == "task_cap" else 0}
        caps[cap_key] = round(cap + caps["task_spent"], 10)
        t = turn(caps=caps, prior_totals=Usage(cost_usd=100))
        result = await t.authorize(authorize(estimated_cost_usd=0))
        assert result["allow"] is allowed
        if not allowed:
            assert result["reason"] == cap_key
            assert (await t.finish(settled(requests=0))).outcome == "capped"
    asyncio.run(test())


def test_output_estimate_is_clamped_and_concurrent_reservations_are_serialized():
    async def test():
        t = turn(caps={"run_cap": 0.016384, "task_cap": None, "task_spent": 0})
        a, b = await asyncio.gather(t.authorize(authorize("a", estimated_input_tokens=0, max_output_tokens=10000)),
                                    t.authorize(authorize("b", estimated_input_tokens=1, max_output_tokens=0)))
        assert a["allow"] and b["reason"] == "run_cap"
    asyncio.run(test())


def test_actual_replaces_reservation_and_duplicates_cannot_double_charge():
    async def test():
        t = turn(caps={"run_cap": 0.000025, "task_cap": None, "task_spent": 0})
        assert (await t.authorize(authorize()))["allow"]
        event = await t.request_end(ended(total_cost_usd=999))
        assert event["request_id"] == "r" and event["trace_id"] == "t" and event["usage_complete"]
        assert event["usage"]["input_tokens"] == 2
        with pytest.raises(ProtocolError):
            await t.request_end(ended())
        assert (await t.authorize(authorize("next")))["allow"]
        await t.request_end(ended("next"))
        result = await t.finish(settled(requests=2))
        assert result.usage.cost_usd == 0.000008
        assert result.model_usage["m"] == result.totals == result.usage
    asyncio.run(test())


@pytest.mark.parametrize("change,reason", [("cap", "run_cap"), ("stop", "stopped"), ("count", "max_requests")])
def test_next_authorization_observes_cap_stop_and_request_count(change, reason):
    async def test():
        caps = {"run_cap": 1, "task_cap": None, "task_spent": 0}
        t = turn(caps=caps, max_requests=1 if change == "count" else 30)
        await t.authorize(authorize())
        await t.request_end(ended())
        if change == "cap":
            caps["run_cap"] = 0.00001
        elif change == "stop":
            t.stop()
        assert (await t.authorize(authorize("next")))["reason"] == reason
    asyncio.run(test())


@pytest.mark.parametrize("changes,code", [({"trace_id": "old"}, ErrorCode.TRACE_MISMATCH),
    ({"session_id": "other"}, ErrorCode.SESSION_NOT_FOUND), ({"provider": "other"}, ErrorCode.INVALID_PARAMS),
    ({"model": "cheap"}, ErrorCode.INVALID_PARAMS), ({"kind": "invented"}, ErrorCode.INVALID_PARAMS),
    ({"estimated_input_tokens": True}, ErrorCode.INVALID_PARAMS), ({"max_output_tokens": -1}, ErrorCode.INVALID_PARAMS)])
def test_invalid_authorization_is_a_typed_failure(changes, code):
    async def test():
        with pytest.raises(ProtocolError) as error:
            await turn().authorize(authorize(**changes))
        assert error.value.code == code
    asyncio.run(test())


@pytest.mark.parametrize("capped", [False, True])
def test_unknown_price_is_allowed_only_without_a_cap(capped):
    async def test():
        t = turn(price=False, caps={"run_cap": 1 if capped else None, "task_cap": None, "task_spent": 0})
        reply = await t.authorize(authorize())
        if capped:
            assert reply["reason"] == "budget_unverifiable"
        else:
            assert reply["allow"]
            await t.request_end(ended())
            summary = await t.finish(settled())
            assert summary.usage_complete and summary.usage.cost_usd is None
    asyncio.run(test())


@pytest.mark.parametrize("missing", ["input_uncached", "output", "cache_read", "cache_write", "complete", "request_end"])
def test_missing_usage_never_becomes_zero_or_a_known_partial_sum(missing):
    async def test():
        t = turn(prior_totals=Usage(cost_usd=10), prior_model_usage={"m": Usage(cost_usd=10)})
        await t.authorize(authorize())
        if missing != "request_end":
            params = ended()
            if missing == "complete":
                params["complete"] = False
            else:
                params.pop(missing)
            assert not (await t.request_end(params))["usage_complete"]
            await t.authorize(authorize("next"))
            await t.request_end(ended("next"))
        summary = await t.finish(settled(requests=1 if missing == "request_end" else 2))
        assert not summary.usage_complete
        assert summary.usage.cost_usd is summary.totals.cost_usd is summary.model_usage["m"].cost_usd is None
    asyncio.run(test())


@pytest.mark.parametrize("actual,reason", [(20, "run_cap"), (21, "run_cap"), (None, "budget_unverifiable")])
@pytest.mark.parametrize("cap_key", ["run_cap", "task_cap"])
def test_actual_at_cap_or_incomplete_requests_trace_cancel(actual, reason, cap_key):
    async def test():
        cancelled = []
        async def cancel(cause):
            cancelled.append(cause)
        caps = {"run_cap": None, "task_cap": None, "task_spent": 0}
        caps[cap_key] = 0.00002
        expected = cap_key if reason == "run_cap" else reason
        t = turn(caps=caps, cancel=cancel)
        await t.authorize(authorize())
        await t.request_end(ended(input_uncached=actual or 0, output=0, complete=actual is not None))
        assert cancelled == [expected]
        assert (await t.authorize(authorize("next")))["reason"] == "stopped"
        summary = await t.finish(settled())
        assert summary.outcome == "capped" and summary.cap_reason == expected
    asyncio.run(test())


def test_task_aggregate_is_current_trace_only_and_unreadable_fails_closed():
    async def test():
        caps = {"run_cap": None, "task_cap": 1, "task_spent": 0.5}
        t = turn(caps=caps, prior_totals=Usage(cost_usd=100))
        assert (await t.authorize(authorize()))["allow"]
        await t.request_end(ended())
        caps["task_spent"] = None
        assert (await t.authorize(authorize("next")))["reason"] == "budget_unverifiable"
        manager = turn(caps=caps, role="manager")
        assert (await manager.authorize(authorize()))["allow"]
    asyncio.run(test())


def test_stop_and_caller_cancel_drain_started_tool_without_restarting_turn():
    async def test():
        started, release = asyncio.Event(), asyncio.Event()
        class Write:
            async def call(self, call):
                started.set()
                await release.wait()
                return ToolOutcome("written", False, True)
        t = turn(tools=Write())
        params = {"session_id": "s", "trace_id": "t", "call_id": "c", "name": "write", "arguments": {}}
        work = asyncio.create_task(t.call_tool(params))
        await started.wait()
        t.stop()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        with pytest.raises(ProtocolError) as error:
            await t.call_tool(params)
        assert error.value.code == ErrorCode.NOT_RUNNING
        finishing = asyncio.create_task(t.finish(settled(requests=0, outcome="cancelled")))
        await asyncio.sleep(0)
        assert not finishing.done()
        release.set()
        assert (await finishing).outcome == "cancelled"
        assert (await t.authorize(authorize()))["reason"] == "stopped"
    asyncio.run(asyncio.wait_for(test(), 5))


@pytest.mark.parametrize("changes", [{"request_id": "unknown"}, {"request_id": []}, {"model": "cheap"},
    {"request_kind": "retry"}, {"input_uncached": -1}, {"output": True}, {"cache_read": 1.2},
    {"cache_write": "1"}, {"reasoning": -1}, {"complete": 1}])
def test_request_end_rejects_unmatched_or_invalid_usage(changes):
    async def test():
        t = turn()
        await t.authorize(authorize())
        with pytest.raises(ProtocolError) as error:
            await t.request_end(ended(**changes))
        assert error.value.code == ErrorCode.INVALID_PARAMS
        await t.request_end(ended())
        assert (await t.finish(settled())).usage.cost_usd == 0.000004
    asyncio.run(test())


def test_authorized_ids_cannot_be_reused_and_settlement_rejects_extra_requests():
    async def test():
        t = turn()
        await t.authorize(authorize())
        with pytest.raises(ProtocolError):
            await t.authorize(authorize())
        await t.request_end(ended())
        with pytest.raises(ProtocolError):
            await t.finish(settled(requests=2))
        assert (await t.finish(settled())).usage.cost_usd == 0.000004
    asyncio.run(test())


def test_pending_request_at_settlement_is_unverifiable_under_cap():
    async def test():
        t = turn(caps={"run_cap": 1, "task_cap": None, "task_spent": 0})
        await t.authorize(authorize())
        summary = await t.finish(settled(outcome="failed"))
        assert summary.outcome == "capped" and summary.cap_reason == "budget_unverifiable"
        assert summary.usage.cost_usd is None and not summary.usage_complete
    asyncio.run(test())


def test_tool_timeout_keeps_the_write_for_shutdown(monkeypatch):
    async def test():
        started, release, written = asyncio.Event(), asyncio.Event(), []
        class Write:
            async def call(self, call):
                started.set()
                await release.wait()
                written.append(call.call_id)
                return ToolOutcome("written", False, True)
        wait_for = asyncio.wait_for
        async def expire(awaitable, timeout):
            assert timeout == 120
            await started.wait()
            return await wait_for(awaitable, 0)
        monkeypatch.setattr(asyncio, "wait_for", expire)
        t = turn(tools=Write())
        result = await t.call_tool({"session_id": "s", "trace_id": "t", "call_id": "c", "name": "write", "arguments": {}})
        assert result["is_error"] and result["ran"] and not written
        t.stop()
        release.set()
        await t.drain_tools()
        assert written == ["c"]
    asyncio.run(test())


@pytest.mark.parametrize("name,wait,expected", [
    ("looper_room_inbox", 120, 140), ("looper_room_inbox", 150.5, 170.5),
    ("looper_room_inbox", 100, 120), ("looper_room_inbox", True, 120),
    ("looper_room_inbox", "120", 120), ("looper_room_inbox", -1, 120),
    ("other_tool", 120, 120),
])
def test_room_inbox_wait_has_time_to_return_before_tool_timeout(monkeypatch, name, wait, expected):
    async def test():
        wait_for = asyncio.wait_for

        async def inspect_timeout(awaitable, timeout):
            assert timeout == expected
            return await wait_for(awaitable, 1)

        monkeypatch.setattr(asyncio, "wait_for", inspect_timeout)
        t = turn()
        result = await t.call_tool({"session_id": "s", "trace_id": "t", "call_id": "c", "name": name,
                                    "arguments": {"wait_seconds": wait}})
        assert not result["is_error"] and result["ran"]
        await t.drain_tools()
    asyncio.run(test())


@pytest.mark.parametrize("max_requests", [0, -1, True, 1.5, "30", None])
def test_invalid_request_limit_is_rejected(max_requests):
    with pytest.raises(ValueError, match="max_requests must be a positive integer"):
        turn(max_requests=max_requests)


@pytest.mark.parametrize("cap_key", ["run_cap", "task_cap"])
@pytest.mark.parametrize("actual", [20, 21])
def test_finish_checks_actual_cap_after_request_end_limits_read_failed(cap_key, actual):
    async def test():
        reads, cancelled = 0, []
        caps = {"run_cap": None, "task_cap": None, "task_spent": 0.1 if cap_key == "task_cap" else 0}
        caps[cap_key] = round(caps["task_spent"] + 0.00002, 10)

        async def limits():
            nonlocal reads
            reads += 1
            if reads == 2:
                raise OSError("limits temporarily unavailable")
            return caps

        async def cancel(reason):
            cancelled.append(reason)

        t = RunnerTurn(session_id="s", trace_id="t", role="planner", tools=Tools(), provider_id="p", model="m",
                       price=lambda model, usage: ModelPrice(1, 2, 3, 4).cost(usage), limits=limits, cancel=cancel)
        assert (await t.authorize(authorize()))["allow"]
        with pytest.raises(OSError, match="limits temporarily unavailable"):
            await t.request_end(ended(input_uncached=actual, output=0))
        summary = await t.finish(settled())
        assert summary.outcome == "capped" and summary.cap_reason == cap_key
        assert summary.usage_complete and summary.usage.cost_usd == actual / 1_000_000
        assert cancelled == [cap_key]
        assert (await t.authorize(authorize("next")))["reason"] == "stopped"
    asyncio.run(test())
