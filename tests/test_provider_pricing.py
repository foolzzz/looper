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


@pytest.mark.parametrize("preset_id", ["glm-bigmodel", "glm-zai", "kimi-moonshot-cn", "kimi-moonshot", "deepseek"])
def test_context_suffixes_belong_only_to_a_presets_anthropic_api(preset_id: str) -> None:
    from looper.agents.provider_presets import api_models, presets_view

    preset = next(p for p in presets_view() if p["preset_id"] == preset_id)
    for api in preset["apis"]:
        models = api_models(preset, api)
        assert len(models) == len(preset["models"])
        anthropic = "anthropic-messages" in api["protocols"]
        for original, model in zip(preset["models"], models):
            for field in ("name", "background_model"):
                expected = original[field]
                if expected and not anthropic:
                    expected = expected.removesuffix("[1m]")
                assert model[field] == expected
            assert model["context_window"] == original["context_window"]
            if original["name"].endswith("[1m]"):
                assert model["context_window"] == 1_000_000


@pytest.mark.parametrize(("name", "base", "suffix"), [
    ("glm-5.3[1m]", "glm-5.3", "[1m]"),
    (" sonnet[200k] ", "sonnet", "[200k]"),
    ("deepseek-flash", "deepseek-flash", None),
    ("claude-haiku-4-5-20251001[1m]", "claude-haiku-4-5-20251001", "[1m]"),
    ("model[1m]-preview", "model[1m]-preview", None),
])
def test_model_names_parse_a_trailing_context_suffix(name: str, base: str, suffix: str | None) -> None:
    from looper.agents.model_names import parse_model_name

    parsed = parse_model_name(name)
    assert parsed == (base, suffix)
    assert parsed.base == base and parsed.context_suffix == suffix


@pytest.mark.parametrize("protocol", list(ApiProtocol))
def test_provider_context_suffix_warnings_name_main_and_background_models(protocol: ApiProtocol) -> None:
    from dataclasses import replace
    from looper.agents.profile_registry import provider_warnings

    provider = replace(_provider(CatalogModel("glm-5.3[1m]", background_model="glm-5.3-flash[1m]")),
                       protocols=frozenset({protocol}))
    warnings = provider_warnings(provider)
    if protocol is ApiProtocol.ANTHROPIC_MESSAGES:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert all(name in warnings[0] for name in provider.models + ("glm-5.3-flash[1m]",))
        assert "Claude Code convention" in warnings[0] and "verbatim" in warnings[0]
    assert provider_warnings(replace(provider, catalog=(CatalogModel("deepseek-flash"),))) == []
    background_only = provider_warnings(replace(provider, catalog=(
        CatalogModel("glm-5.3", background_model="glm-5.3-flash[1m]"),)))
    assert len(background_only) == (0 if protocol is ApiProtocol.ANTHROPIC_MESSAGES else 1)
    if background_only:
        assert "glm-5.3-flash[1m]" in background_only[0]
    assert provider_warnings(replace(provider, protocols=frozenset(ApiProtocol))) == []


@pytest.mark.parametrize("protocol", ["openai-chat", "anthropic-messages"])
def test_provider_api_saves_return_context_suffix_warnings(
    monkeypatch: pytest.MonkeyPatch, protocol: str,
) -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from looper.db import connection, profile_registry as db
    from tests.test_profile_registry_db import _ok

    # Exercise the HTTP/registry save path; only PostgreSQL persistence is replaced.
    row = {"id": "prov-test", "builtin": False, "created_at": "", "updated_at": ""}

    @asynccontextmanager
    async def transaction():
        yield AsyncMock()

    async def insert(_conn, fields, _key):
        row.update(fields)
        return row["id"]

    async def update(_conn, _id, fields, **_kwargs):
        row.update(fields)

    monkeypatch.setattr(connection, "transaction", transaction)
    monkeypatch.setattr(db, "insert_provider", insert)
    monkeypatch.setattr(db, "update_provider", update)
    monkeypatch.setattr(db, "get_provider", AsyncMock(side_effect=lambda *_a, **_kw: dict(row)))
    monkeypatch.setattr(db, "list_providers", AsyncMock(side_effect=lambda: [dict(row)]))
    monkeypatch.setattr(db, "list_profiles", AsyncMock(return_value=[]))

    async def save() -> None:
        answer = await _ok("POST", "/api/providers", {"name": "test", "protocols": [protocol], "auth": "api_key",
            "models": [{"name": "glm-5.3[1m]", "background_model": "glm-5.3-flash[1m]"}]})
        expected = 0 if protocol == "anthropic-messages" else 1
        assert len(answer["warnings"]) == expected
        assert row["models"][0]["name"] == "glm-5.3[1m]"
        answer = await _ok("PUT", "/api/providers/prov-test", {"name": "renamed"})
        assert len(answer["warnings"]) == expected
        if expected:
            assert "glm-5.3-flash[1m]" in answer["warnings"][0]
        answer = await _ok("PUT", "/api/providers/prov-test", {"models": [{"name": "glm-5.3"}]})
        assert answer["warnings"] == [] and row["models"][0]["name"] == "glm-5.3"

    asyncio.run(save())


@pytest.mark.parametrize("runtime_type", ["claude-code", "omp", "codex"])
@pytest.mark.parametrize(("model", "background"), [
    ("glm-5.3[1m]", "glm-5.3-flash[1m]"), ("glm-5.3", "glm-5.3-flash[1m]"),
    ("glm-5.3", None),
])
def test_profile_context_suffix_warnings_follow_its_runtime(
    monkeypatch: pytest.MonkeyPatch, runtime_type: str, model: str, background: str | None,
) -> None:
    from unittest.mock import AsyncMock

    from looper.agents.profile_registry import _profile_answer
    from looper.db import profile_registry as db

    # Even a provider that serves Anthropic messages can be used by another runtime.
    row = {"id": "profile-test", "name": "test", "model": model, "builtin": False,
           "runtime_id": "rt-test", "runtime_name": "test", "runtime_type": runtime_type,
           "provider_id": "prov-test", "provider_name": "test", "provider_auth": "api_key",
           "provider_protocols": ["anthropic-messages"], "provider_base_url": "https://example.invalid",
           "provider_models": [{"name": model, "background_model": background}, {"name": "unrelated[1m]"}]}
    monkeypatch.setattr(db, "get_profile", AsyncMock(return_value=row))
    monkeypatch.setattr(db, "profile_usage", AsyncMock(return_value={}))
    warnings = asyncio.run(_profile_answer(row["id"]))["warnings"]
    if runtime_type == "claude-code" or not (model.endswith("[1m]") or background):
        assert warnings == []
    else:
        assert len(warnings) == 1 and "Claude Code convention" in warnings[0] and "verbatim" in warnings[0]
        assert "unrelated" not in warnings[0]
        for name in (model, background):
            if name and name.endswith("[1m]"):
                assert name in warnings[0]


def test_manager_provider_registration_keeps_registry_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from looper.agents import profile_registry
    from looper.agents.ops.config import _providers

    provider = {"provider_id": "prov-test", "name": "test", "auth": "api_key", "protocols": ["openai-chat"],
                "base_url": "https://example.invalid", "models": [{"name": "glm-5.3[1m]"}],
                "profile_count": 0, "builtin": False, "api_key": None}
    warnings = ["glm-5.3[1m]: a Claude Code convention; other runtimes send it verbatim"]
    monkeypatch.setattr(profile_registry, "create_provider", AsyncMock(return_value={
        "provider": provider, "warnings": warnings}))
    answer = asyncio.run(_providers(SimpleNamespace(arguments={
        "action": "register", "name": "test", "protocols": ["openai-chat"], "models": ["glm-5.3[1m]"]})))
    assert answer["warnings"] == warnings


def test_a_runs_cost_is_each_kind_of_token_at_its_price() -> None:
    usage = Usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000, cache_creation_tokens=4_000)
    cost = price_cost(usage, _provider(MAIN), "m-main")
    # 1000 * 2 + 2000 * 10 + 30000 * 0.2 + 4000 * 2.5 = 2000 + 20000 + 6000 + 10000 = 38000 per million
    assert cost.source is CostSource.PROVIDER_PRICE
    assert cost.usage == Usage(cost_usd=0.038, input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000,
                               cache_creation_tokens=4_000)


def test_deepseek_models_stay_priced_with_and_without_the_context_window_suffix() -> None:
    """``[1m]`` is Claude Code's context-window mark. DeepSeek's OpenAI API names the model without it, and a
    run reports whichever form it used. Both are the same full price."""
    bare = _provider()
    for name, prices in (("deepseek-flash", (0.30, 1.20, 0.006, 0.30)),
                         ("deepseek-v4-pro", (1.32, 3.96, 0.044, 1.32))):
        plain, marked = model_price(bare, name), model_price(bare, f"{name}[1m]")
        assert plain.complete and plain == marked
        assert (plain.input_usd_per_mtok, plain.output_usd_per_mtok, plain.cache_read_usd_per_mtok,
                plain.cache_write_usd_per_mtok) == prices


def test_a_model_name_without_a_catalog_price_falls_back_to_the_builtin_table() -> None:
    """OpenCode prices some models as ranges, so the table stays the vendors' list prices and those names fall back."""
    from looper.agents.runtimes.pricing import BUILTIN_PRICES, PRICE_SOURCES

    provider = _provider()
    for name in ("gpt-6.1-sol", "gpt-5.6-sol", "claude-opus-5-5", "claude-sonnet-5-5", "deepseek-v4-pro", "glm-5.3",
                 "kimi-k3"):
        assert name in BUILTIN_PRICES
        assert model_price(provider, name) == BUILTIN_PRICES[name]
        assert model_price(provider, name).complete
    assert all("opencode.ai" not in source for source in PRICE_SOURCES)


def test_a_catalog_price_wins_and_an_unknown_one_comes_from_the_builtin_table() -> None:
    # glm-5.3's built-in prices: 1.40 input, 4.40 output, 0.26 cache read, 1.40 cache write.
    provider = _provider(CatalogModel("glm-5.3[1m]", output_usd_per_mtok=5.0))
    price = model_price(provider, "glm-5.3[1m]")
    assert (price.input_usd_per_mtok, price.output_usd_per_mtok, price.cache_read_usd_per_mtok,
            price.cache_write_usd_per_mtok) == (1.40, 5.0, 0.26, 1.40)
    # The runtime reports the model without its suffix or with a date: the same prices.
    assert model_price(provider, "glm-5.3") == price
    assert model_price(_provider(), "claude-haiku-4-5-20251001") == model_price(_provider(), "claude-haiku-4-5")


@pytest.mark.parametrize(("auth", "model", "priced_as"), [
    # With Claude Code's own login, an alias is the model it runs (its runs report claude-opus-5-5 for opus).
    (AuthKind.OAUTH, "opus", "claude-opus-5-5"),
    (AuthKind.OAUTH, "sonnet[1m]", "claude-sonnet-5-5"),
    (AuthKind.OAUTH, "haiku", "claude-haiku-4-5"),
    # With another provider, its environment may map an alias to a model of its own: unpriced.
    (AuthKind.API_KEY, "sonnet", None),
])
def test_a_claude_code_alias_is_priced_as_its_model_only_with_claude_codes_own_login(
    auth: AuthKind, model: str, priced_as: str | None,
) -> None:
    provider = ProviderSpec("prov-c", "c", auth, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                            oauth_runtime_type="claude-code" if auth is AuthKind.OAUTH else None)
    price = model_price(provider, model)
    assert price == (model_price(provider, priced_as) if priced_as else model_price(_provider(), "no-such-model"))
    assert price.complete is (priced_as is not None)


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
