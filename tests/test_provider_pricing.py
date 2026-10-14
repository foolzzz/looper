"""Pricing a run at its provider's prices, and the soft per-run cap Looper keeps (multi-runtime RFC section 8).

Expected figures are worked out by hand from the prices each test states: tokens times US dollars per million
tokens, for uncached input, output, cache reads and cache writes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from looper.agents.profile_registry import CatalogModel, ProviderSpec
from looper.agents.run_usage import Usage, reported_totals, run_usage
from looper.agents.runtimes import ApiProtocol, AuthKind, CostSource, cap_exceeded, price_cost
from looper.agents.runtimes.pricing import RunSpend, model_price, pricer, usage_event


def _provider(*catalog: CatalogModel) -> ProviderSpec:
    return ProviderSpec("prov-x", "x", AuthKind.API_KEY, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                        base_url="https://example.invalid/anthropic", catalog=catalog)


MAIN = CatalogModel("m-main", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, cache_read_usd_per_mtok=0.2,
                    cache_write_usd_per_mtok=2.5)


def test_a_runs_cost_is_each_kind_of_token_at_its_price() -> None:
    usage = Usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000, cache_creation_tokens=4_000)
    cost = price_cost(usage, _provider(MAIN), "m-main")
    # 1000 * 2 + 2000 * 10 + 30000 * 0.2 + 4000 * 2.5 = 2000 + 20000 + 6000 + 10000 = 38000 per million
    assert cost.source is CostSource.PROVIDER_PRICE
    assert cost.usage == Usage(cost_usd=0.038, input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000,
                               cache_creation_tokens=4_000)


def test_a_catalog_price_wins_and_an_unknown_one_comes_from_the_builtin_table() -> None:
    # glm-5.3's built-in prices: 1.40 input, 4.40 output, 0.26 cache read, 1.40 cache write.
    provider = _provider(CatalogModel("glm-5.3[1m]", output_usd_per_mtok=5.0))
    price = model_price(provider, "glm-5.3[1m]")
    assert (price.input_usd_per_mtok, price.output_usd_per_mtok, price.cache_read_usd_per_mtok,
            price.cache_write_usd_per_mtok) == (1.40, 5.0, 0.26, 1.40)
    # The runtime reports the model without its suffix or with a date: the same prices.
    assert model_price(provider, "glm-5.3") == price
    assert model_price(_provider(), "claude-haiku-4-5-20251001") == model_price(_provider(), "claude-haiku-4-5")


@pytest.mark.parametrize("usage", [
    Usage(input_tokens=10, output_tokens=5),
    Usage(input_tokens=10, cache_read_tokens=3),  # a kind the model has no price for
])
def test_a_model_without_the_prices_it_needs_records_tokens_only(usage: Usage) -> None:
    provider = _provider(CatalogModel("half-priced", input_usd_per_mtok=1.0, output_usd_per_mtok=None))
    model = "mystery-1" if usage.output_tokens else "half-priced"
    cost = price_cost(usage, provider, model)
    assert cost.usage.cost_usd is None and cost.usage.input_tokens == 10


def test_a_result_is_priced_model_by_model_and_a_resumed_turn_counts_only_its_own_share() -> None:
    provider = _provider(MAIN)  # the background model (glm-5.3-flash) is priced by the built-in table

    def result(main_input: int, flash_output: int) -> dict:
        return {"type": "result", "total_cost_usd": 99.0, "modelUsage": {
            "m-main": {"inputTokens": main_input, "outputTokens": 0, "cacheReadInputTokens": 0,
                       "cacheCreationInputTokens": 0},
            "glm-5.3-flash[1m]": {"inputTokens": 0, "outputTokens": flash_output, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0}}}

    price = pricer(provider)
    first = reported_totals(result(1_000_000, 1_000_000), price)
    # Claude Code's 99.0 is ignored: 1M input at 2.0 + 1M output at 0.50 (glm-5.3-flash).
    assert first.cost_usd == pytest.approx(2.5) and first.input_tokens == 1_000_000
    # The next result of the same session reports running totals; the turn's own share is the difference.
    own = run_usage(result(1_500_000, 1_000_000), before=first, price=price)
    assert own.cost_usd == pytest.approx(1.0) and own.input_tokens == 500_000


def test_the_soft_cap_counts_usage_events_at_the_providers_prices() -> None:
    spend = RunSpend(_provider(MAIN), "m-main", cap=0.5)
    request = usage_event("m-main", Usage(input_tokens=200_000, output_tokens=0))  # $0.40
    assert spend.add({"type": "assistant"}) is False
    assert spend.add(request) is False and not spend.exceeded
    assert spend.add(request) is True and spend.exceeded  # $0.80: the request that crossed it
    assert spend.add(request) is False  # already over
    assert spend.usage.cost_usd == pytest.approx(1.2) and spend.usage.input_tokens == 600_000
    # An unpriced request takes the run out of the cap, and nothing without a cap is ever over it.
    unpriced = RunSpend(_provider(MAIN), "m-main", cap=0.0001)
    unpriced.add(usage_event("mystery-1", Usage(input_tokens=1)))
    unpriced.add(request)
    assert unpriced.usage.cost_usd is None and not unpriced.exceeded
    assert (cap_exceeded(1.0, None), cap_exceeded(None, 1.0), cap_exceeded(0.5, 0.5), cap_exceeded(0.6, 0.5)) \
        == (False, False, False, True)


def test_claude_codes_stream_yields_one_usage_event_per_model_request() -> None:
    from looper.agents.claude_cli import stream_events

    def stream(kind: str, parent: str | None = None, **event: object) -> dict:
        return {"type": "stream_event", "parent_tool_use_id": parent, "event": {"type": kind, **event}}

    lines = [
        stream("message_start", message={"model": "m-main", "usage": {"input_tokens": 900, "output_tokens": 0}}),
        {"type": "assistant", "message": {"usage": {"input_tokens": 900, "output_tokens": 0}}},
        stream("message_start", "tool-1", message={"model": "m-sub", "usage": {"input_tokens": 7}}),
        stream("message_delta", usage={"input_tokens": 800, "output_tokens": 30, "cache_read_input_tokens": 100}),
        stream("message_stop"),
        stream("message_delta", "tool-1", usage={"output_tokens": 4}),
        stream("message_stop", "tool-1"),
    ]

    async def _read() -> list[dict]:
        reader = asyncio.StreamReader()
        reader.feed_data("".join(json.dumps(line) + "\n" for line in lines).encode())
        reader.feed_eof()
        return [event async for event in stream_events(reader)]

    usage = [e for e in asyncio.run(_read()) if e["type"] == "usage"]
    assert usage == [
        {"type": "usage", "model": "m-main", "parent_tool_use_id": None,
         "usage": {"input_tokens": 800, "output_tokens": 30, "cache_read_tokens": 100}},
        {"type": "usage", "model": "m-sub", "parent_tool_use_id": "tool-1",
         "usage": {"input_tokens": 7, "output_tokens": 4}},
    ]
