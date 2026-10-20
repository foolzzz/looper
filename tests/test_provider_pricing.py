"""Pricing a run at its provider's prices, and the soft per-run cap Looper keeps (multi-runtime RFC section 8).

Expected figures are worked out by hand from the prices each test states: tokens times US dollars per million
tokens, for uncached input, output, cache reads and cache writes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace

import pytest

from looper.agents.profile_registry import (
    CatalogModel, LegacyProviderSpec, ProviderSpec, ModelSpec, Endpoint, ResolvedModel,
    ResolvedAgentProfile, RuntimeSpec,
)
from looper.agents.run_usage import Usage, reported_totals, run_usage
from looper.agents.runtimes import ApiProtocol, AuthKind, CostSource, VendorType, cap_exceeded, price_cost
from looper.agents.runtimes.pricing import RunSpend, model_price, pricer, usage_event


def _provider(*catalog: CatalogModel) -> LegacyProviderSpec:
    return LegacyProviderSpec("prov-x", "x", AuthKind.API_KEY, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
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
    models = [{"name": "glm-5.3[1m]", "background_model": "glm-5.3-flash[1m]"}]

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
    monkeypatch.setattr(db, "list_models", AsyncMock(side_effect=lambda **_kw: list(models)))

    async def save() -> None:
        answer = await _ok("POST", "/api/providers", {"name": "test", "vendor_type": "custom",
            "endpoints": [{"protocol": protocol, "base_url": "https://example.invalid"}]})
        expected = 0 if protocol == "anthropic-messages" else 1
        assert len(answer["warnings"]) == expected
        assert models[0]["name"] == "glm-5.3[1m]"
        answer = await _ok("PUT", "/api/providers/prov-test", {"name": "renamed"})
        assert len(answer["warnings"]) == expected
        if expected:
            assert "glm-5.3-flash[1m]" in answer["warnings"][0]
        models[:] = [{"name": "glm-5.3", "background_model": None}]
        answer = await _ok("PUT", "/api/providers/prov-test", {"name": "renamed again"})
        assert answer["warnings"] == []

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
    resolved = _resolved(model, background_model=background)
    row.update(model_id=resolved.model.id, model=asdict(resolved.model),
               provider=asdict(resolved.provider), endpoint=asdict(resolved.endpoint))
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

    fields = {"name": "test", "vendor_type": "custom", "endpoints": [
        {"protocol": "openai-chat", "base_url": "https://example.invalid"}]}
    provider = {"provider_id": "prov-test", **fields, "auth": "api_key", "model_count": 0,
                "profile_count": 0, "builtin": False, "api_key": None, "login": None}
    # Forward registry warnings independently of how the registry derives them.
    warnings = ["glm-5.3[1m]: a Claude Code convention; other runtimes send it verbatim"]
    create = AsyncMock(return_value={"provider": provider, "warnings": warnings})
    monkeypatch.setattr(profile_registry, "create_provider", create)
    answer = asyncio.run(_providers(SimpleNamespace(arguments={"action": "register", **fields})))
    create.assert_awaited_once_with(fields)
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
    provider = LegacyProviderSpec("prov-c", "c", auth, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
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


# Normalized registry bindings: account and exact protocol are part of every run's price scope.
def _resolved(name: str = "m-main", *, protocol: ApiProtocol = ApiProtocol.ANTHROPIC_MESSAGES,
              vendor: VendorType = VendorType.CUSTOM, base_url: str = "https://example.invalid",
              **prices) -> ResolvedModel:
    provider = ProviderSpec("prov-x", "x", vendor, vendor.auth_kind, vendor.oauth_runtime_type)
    endpoint = Endpoint(provider.id, protocol, "" if provider.auth is AuthKind.OAUTH else base_url)
    return ResolvedModel(ModelSpec("model-main", provider.id, name, protocol, **prices), provider, endpoint)


def _launch(resolved: ResolvedModel, catalog: tuple[ModelSpec, ...] = (), runtime: str = "claude-code"):
    from looper.agents.agent_settings import AgentSettings, Launch

    profile = ResolvedAgentProfile("profile-x", "test", RuntimeSpec("rt-x", runtime, runtime),
                                   resolved.model.id, resolved_model=resolved)
    return Launch(AgentSettings(profile=profile, pricing_catalog=catalog, max_budget_usd=0.5))


@pytest.mark.parametrize(("name", "expected"), [
    ("claude-haiku-4-5-20251001[1m]", 7),
    ("claude-haiku-4-5-20251001", 3),
    (" CLAUDE-HAIKU-4-5-20261001[1M] ", 3),
])
def test_exact_name_wins_before_normalized_catalog_order(name: str, expected: float) -> None:
    resolved = _resolved("claude-haiku-4-5-20251001[1m]", input_usd_per_mtok=7, catalog_order=20)
    first = replace(resolved.model, id="model-first", name="claude-haiku-4-5[200k]",
                    input_usd_per_mtok=3, catalog_order=1)
    # Input order deliberately differs from catalog_order; a stale copy of the selected id cannot override it.
    price = pricer(resolved, catalog=(replace(resolved.model, input_usd_per_mtok=99), first))
    assert price(name, Usage(input_tokens=1_000_000)) == expected


def test_price_scope_excludes_other_protocols_and_accounts() -> None:
    resolved = _resolved("glm-5.3", output_usd_per_mtok=0)
    sibling = replace(resolved.model, id="sibling", name="glm-5.3-flash", output_usd_per_mtok=6)
    foreign = replace(sibling, id="foreign", provider_id="other", output_usd_per_mtok=99)
    other_api = replace(sibling, id="other-api", protocol=ApiProtocol.OPENAI_CHAT, output_usd_per_mtok=80)
    price = pricer(resolved, catalog=(foreign, other_api, sibling))
    assert price("glm-5.3", Usage(output_tokens=1_000_000)) == 0
    assert price("glm-5.3-flash", Usage(output_tokens=1_000_000)) == 6
    assert pricer(resolved, catalog=(foreign, other_api))(
        "glm-5.3-flash", Usage(output_tokens=1_000_000)) == 0.5  # built-in, not another protocol's override


@pytest.mark.parametrize(("usage", "expected"), [
    (Usage(), None), (Usage(input_tokens=0), 0),
    (Usage(input_tokens=1_000_000), 2), (Usage(output_tokens=1), None),
    (Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0), 2),
    (Usage(input_tokens=1, cache_read_tokens=1), None),
    (Usage(cache_creation_tokens=1), None), (Usage(output_tokens=0), 0),
])
def test_partial_prices_only_require_prices_for_used_token_kinds(usage: Usage, expected: float | None) -> None:
    price = pricer(_resolved("half-priced", input_usd_per_mtok=2))
    assert price("half-priced", usage) == expected


def test_launch_freezes_primary_auxiliary_and_catalog_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import pricing

    resolved = _resolved("glm-5.3", input_usd_per_mtok=8, background_model="glm-5.3-flash")
    auxiliary = replace(resolved.model, id="aux", name="glm-5.3-flash", input_usd_per_mtok=4)
    launch = _launch(resolved, (auxiliary,))
    price = launch.price
    assert price is launch.price
    assert price("glm-5.3", Usage(input_tokens=1_000_000)) == 8
    assert price("glm-5.3-flash", Usage(input_tokens=1_000_000)) == 4
    monkeypatch.setitem(pricing.BUILTIN_PRICES, "deepseek-flash", pricing.ModelPrice(99, 99, 99, 99))
    assert price("deepseek-flash", Usage(input_tokens=1_000_000)) == 0.3
    # Removing overrides must recompute field fallbacks, rather than carry forward the old effective price.
    next_launch = _launch(replace(resolved, model=replace(resolved.model, input_usd_per_mtok=None)),
                          (replace(auxiliary, input_usd_per_mtok=None),))
    assert next_launch.price("glm-5.3", Usage(input_tokens=1_000_000)) == 1.4
    assert next_launch.price("glm-5.3-flash", Usage(input_tokens=1_000_000)) == 0.15
    assert next_launch.price("deepseek-flash", Usage(input_tokens=1_000_000)) == 99
    assert price("glm-5.3", Usage(input_tokens=1_000_000)) == 8


@pytest.mark.parametrize(("runtime", "vendor", "protocol", "base_url", "source"), [
    ("claude-code", VendorType.CLAUDE_SUBSCRIPTION, ApiProtocol.ANTHROPIC_MESSAGES, "", CostSource.RUNTIME),
    ("claude-code", VendorType.CUSTOM, ApiProtocol.ANTHROPIC_MESSAGES, "https://api.anthropic.com", CostSource.RUNTIME),
    ("claude-code", VendorType.CUSTOM, ApiProtocol.ANTHROPIC_MESSAGES, "https://example.invalid", CostSource.PROVIDER_PRICE),
    ("codex", VendorType.CODEX_LOGIN, ApiProtocol.OPENAI_RESPONSES, "", CostSource.PROVIDER_PRICE),
    ("codex", VendorType.CUSTOM, ApiProtocol.OPENAI_RESPONSES, "https://example.invalid", CostSource.PROVIDER_PRICE),
    ("omp", VendorType.CUSTOM, ApiProtocol.OPENAI_CHAT, "https://example.invalid", CostSource.PROVIDER_PRICE),
    ("grok", VendorType.GROK_LOGIN, ApiProtocol.OPENAI_RESPONSES, "", CostSource.RUNTIME),
])
def test_launch_keeps_existing_cost_source_selection(runtime, vendor, protocol, base_url, source) -> None:
    launch = _launch(_resolved(vendor=vendor, protocol=protocol, base_url=base_url), runtime=runtime)
    assert launch.cost_source is source
    assert (launch.price is None) is (source is CostSource.RUNTIME)


def test_normalized_aliases_are_only_for_claude_login() -> None:
    for vendor, protocol in ((VendorType.CLAUDE_SUBSCRIPTION, ApiProtocol.ANTHROPIC_MESSAGES),
                             (VendorType.CODEX_LOGIN, ApiProtocol.OPENAI_RESPONSES),
                             (VendorType.GROK_LOGIN, ApiProtocol.OPENAI_RESPONSES),
                             (VendorType.CUSTOM, ApiProtocol.ANTHROPIC_MESSAGES)):
        price = pricer(_resolved("opus", vendor=vendor, protocol=protocol))
        assert price("opus[1m]", Usage(input_tokens=1_000_000)) == (
            4 if vendor is VendorType.CLAUDE_SUBSCRIPTION else None)


def test_recorder_soft_cap_and_result_share_the_frozen_pricer() -> None:
    from looper.agents.claude_cli import ClaudeRunRecorder

    resolved = _resolved(input_usd_per_mtok=2, background_model="aux")
    auxiliary = replace(resolved.model, id="aux", name="aux", input_usd_per_mtok=3)
    launch = _launch(resolved, (auxiliary,))
    recorder = ClaudeRunRecorder(agent_id="worker", task_id=None, command=[], cwd=".", input_text="", launch=launch)
    spend = recorder._spend
    assert spend is not None
    assert not spend.add(usage_event(None, Usage(input_tokens=100_000)))  # primary fallback, $0.2
    assert not spend.add(usage_event("aux", Usage(input_tokens=100_000)))  # exactly the cap, $0.5
    assert spend.add(usage_event("aux", Usage(input_tokens=100_000)))  # $0.8 crosses it
    result = {"modelUsage": {"m-main": {"inputTokens": 100_000}, "aux": {"inputTokens": 200_000}},
              "total_cost_usd": 99}
    totals = reported_totals(result, launch.price)
    assert totals.cost_usd == spend.usage.cost_usd == 0.8
    result["modelUsage"]["aux"]["inputTokens"] = 300_000
    assert run_usage(result, before=totals, price=launch.price).cost_usd == 0.3
    spend.add(usage_event("mystery", Usage(input_tokens=1)))
    spend.add(usage_event(None, Usage(input_tokens=100_000)))
    result["modelUsage"]["mystery"] = {"inputTokens": 1}
    assert spend.usage.cost_usd is None and not spend.exceeded
    assert reported_totals(result, launch.price).cost_usd is None


@pytest.mark.parametrize("fail_catalog", [False, True])
def test_settings_loads_a_fresh_protocol_catalog_or_reports_failure(
    monkeypatch: pytest.MonkeyPatch, fail_catalog: bool,
) -> None:
    from unittest.mock import AsyncMock
    from looper.agents.agent_settings import Launch, load_agent_settings
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.db import agent_setting_layers, profile_registry as db

    resolved = _resolved("glm-5.3", input_usd_per_mtok=8, background_model="aux")
    auxiliary = replace(resolved.model, id="aux", name="aux", input_usd_per_mtok=4)
    foreign = replace(auxiliary, id="other-api", protocol=ApiProtocol.OPENAI_CHAT, input_usd_per_mtok=99)
    rows = [asdict(auxiliary), asdict(foreign)]
    row = {"id": "profile-x", "name": "test", "model_id": resolved.model.id,
           "runtime_id": "rt-x", "runtime_name": "test", "runtime_type": "claude-code",
           "runtime_capabilities": [cap.value for cap in CLAUDE_CODE.capabilities],
           "model": asdict(resolved.model), "provider": asdict(resolved.provider),
           "endpoint": asdict(resolved.endpoint)}
    monkeypatch.setattr(agent_setting_layers, "list_layers", AsyncMock(side_effect=lambda *_a, **_kw: [
        {"scope": "system", "role": None, "profile": row}]))
    monkeypatch.setattr(db, "get_api_key", AsyncMock(return_value="test-key"))
    catalog_read = AsyncMock(side_effect=RuntimeError("catalog unavailable") if fail_catalog
                             else lambda **_kw: list(rows))
    monkeypatch.setattr(db, "list_models", catalog_read)

    async def run() -> None:
        first = await load_agent_settings("worker")
        if fail_catalog:
            assert first.profile_error and "model prices" in first.profile_error
            from looper.agents.runtimes import RuntimeUnsupported
            with pytest.raises(RuntimeUnsupported, match="model prices"):
                Launch(first).args()
            return
        assert first.profile_error is None
        catalog_read.assert_awaited_once_with(provider_id=resolved.provider.id)
        assert first.pricing_catalog == (auxiliary,)
        launch = Launch(first)
        assert launch.price("aux", Usage(input_tokens=1_000_000)) == 4
        rows[0]["input_usd_per_mtok"] = 0
        row["model"]["input_usd_per_mtok"] = None
        second = Launch(await load_agent_settings("worker"))
        assert second.price("aux", Usage(input_tokens=1_000_000)) == 0
        assert second.price("glm-5.3", Usage(input_tokens=1_000_000)) == 1.4
        assert launch.price("aux", Usage(input_tokens=1_000_000)) == 4
        assert launch.price("glm-5.3", Usage(input_tokens=1_000_000)) == 8
        assert launch.binding_sha256 != second.binding_sha256
        auxiliary_only = Launch(replace(first, pricing_catalog=(replace(auxiliary, input_usd_per_mtok=0),)))
        assert launch.binding_sha256 != auxiliary_only.binding_sha256

    asyncio.run(run())


def test_snapshot_prices_are_immutable_and_field_fallbacks_are_independent() -> None:
    from dataclasses import FrozenInstanceError
    from looper.agents.runtimes.pricing import ModelPrice

    resolved = _resolved("glm-5.3", output_usd_per_mtok=0, cache_write_usd_per_mtok=3)
    price = pricer(resolved)
    assert model_price(resolved, "glm-5.3") == ModelPrice(1.4, 0, 0.26, 3)
    assert price_cost(Usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000,
                            cache_creation_tokens=4_000), resolved, "glm-5.3").usage.cost_usd == 0.0212
    with pytest.raises(TypeError):
        price.exact["glm-5.3"] = ModelPrice(99)
    with pytest.raises(FrozenInstanceError):
        price.model_price("glm-5.3").input_usd_per_mtok = 99


@pytest.mark.parametrize("unknown", [False, True])
def test_recorder_writes_snapshot_cost_or_null_for_an_unpriced_request(agent_db, tmp_path, unknown: bool) -> None:
    from looper.agents.claude_cli import ClaudeRunRecorder

    resolved = _resolved(input_usd_per_mtok=2, background_model="aux")
    auxiliary = replace(resolved.model, id="aux", name="aux", input_usd_per_mtok=3)
    launch = _launch(resolved, (auxiliary,))

    async def run() -> None:
        recorder = ClaudeRunRecorder(agent_id="worker", task_id=None, command=["claude"], cwd=str(tmp_path),
                                     input_text="test", launch=launch)
        await recorder.start()
        await recorder.record(usage_event(None, Usage(input_tokens=100_000)))
        await recorder.record(usage_event("aux", Usage(input_tokens=200_000)))
        assert recorder.cap_stop is not None
        assert recorder.cap_stop.usage.cost_usd == 0.8
        models = {"m-main": {"inputTokens": 100_000}, "aux": {"inputTokens": 200_000}}
        if unknown:
            await recorder.record(usage_event("unknown", Usage(output_tokens=1)))
            models["unknown"] = {"outputTokens": 1}
            assert recorder.cap_stop is None
        await recorder.finish(output_text="done", result_event={
            "type": "result", "subtype": "success", "modelUsage": models, "total_cost_usd": 99})

    asyncio.run(run())
    saved = agent_db.finish_record.await_args.kwargs
    assert saved["cost_source"] == "provider_price"
    assert saved["cost_usd"] == (None if unknown else 0.8)
    assert saved["input_tokens"] == (None if unknown else 300_000)
