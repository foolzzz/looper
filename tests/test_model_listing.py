"""Bounded, credential-safe provider discovery without a network or database."""

import asyncio
import json
import logging

import httpx
import pytest

from looper.agents import model_listing as listing
from looper.agents.profile_registry import ProviderSpec, RegistryNotFound, unset_price_defaults
from looper.agents.runtimes import ApiFamily, AuthKind, VendorType
from looper.agents.runtimes.pricing import BUILTIN_PRICES, PRICE_FIELDS, model_price


def _list(handler, *, family=ApiFamily.OPENAI, key="test-key"):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await listing.list_models_at(family, "https://example.invalid/v1/", key, client=client)
    return asyncio.run(run())


def test_openai_list_sorts_deduplicates_and_reads_context():
    def answer(request):
        assert str(request.url) == "https://example.invalid/v1/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "Alpha", "context_length": 8192},
                                                   {"id": "z", "context_length": 1024}]})
    result = _list(answer)
    assert result.models == (listing.ListedModel("Alpha", 8192), listing.ListedModel("z"))
    assert result.problem is None and not result.truncated


def test_no_key_sends_no_authentication():
    def answer(request):
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        return httpx.Response(200, json={"data": []})
    assert _list(answer, key=None).models == ()


def test_anthropic_pages_use_own_path_headers_and_cursor():
    def answer(request):
        assert request.url.path == "/v1/v1/models"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == listing.ANTHROPIC_VERSION
        assert request.url.params["limit"] == "1000"
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": True, "last_id": "a"})
        assert request.url.params["after_id"] == "a"
        return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
    assert [model.name for model in _list(answer, family=ApiFamily.ANTHROPIC).models] == ["a", "b"]


@pytest.mark.parametrize("status", [401, 403])
def test_anthropic_retries_once_with_bearer(status):
    requests = []

    def answer(request):
        requests.append(request)
        if len(requests) == 1:
            assert "x-api-key" in request.headers and "authorization" not in request.headers
            return httpx.Response(status)
        assert "x-api-key" not in request.headers and request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"id": "a"}]})
    assert _list(answer, family=ApiFamily.ANTHROPIC).models == (listing.ListedModel("a"),)
    assert len(requests) == 2


@pytest.mark.parametrize("status,problem", [
    (401, listing.ListingProblem.UNAUTHORIZED), (404, listing.ListingProblem.NOT_FOUND),
    (500, listing.ListingProblem.HTTP_ERROR), (302, listing.ListingProblem.REDIRECT),
])
def test_http_failure_and_redirect_not_followed(status, problem):
    requests = []

    def answer(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.invalid/models"})
    result = _list(answer, key=None)
    assert result.problem is problem and result.status == status and result.models == ()
    assert len(requests) == 1


@pytest.mark.parametrize("error,problem", [
    (httpx.ReadTimeout, listing.ListingProblem.TIMEOUT),
    (httpx.ConnectError, listing.ListingProblem.UNREACHABLE),
])
def test_transport_failure(error, problem):
    def answer(request):
        raise error("private transport diagnostics", request=request)
    assert _list(answer).problem is problem


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{}', b'{"data":[null]}', b'{"data":[{"id":1}]}',
                                   b" " * (listing.MAX_BYTES + 1)])
def test_malformed_or_oversized_response(body):
    assert _list(lambda _: httpx.Response(200, content=body)).problem is listing.ListingProblem.BAD_RESPONSE


def test_page_budget_truncates_openai_cursor():
    pages = []

    def answer(request):
        page = len(pages)
        pages.append(request)
        if page:
            assert request.url.params["after"] == str(page - 1)
        return httpx.Response(200, json={"data": [{"id": str(page)}], "has_more": True, "last_id": str(page)})
    result = _list(answer)
    assert len(pages) == listing.MAX_PAGES and len(result.models) == listing.MAX_PAGES and result.truncated


def test_model_budget_counts_distinct_names():
    data = [{"id": "repeated"}] * 20 + [{"id": f"m-{i}"} for i in range(listing.MAX_MODELS)]
    result = _list(lambda _: httpx.Response(200, json={"data": data}))
    assert len(result.models) == listing.MAX_MODELS and result.truncated


@pytest.mark.parametrize("fields,expected", [
    ({"context_window": 100, "context_length": 200}, 100),
    ({"context_window": True, "context_length": 200}, 200),
    ({"context_window": 0, "context_length": "200", "max_context_length": 300}, 300),
    ({"context_window": listing.CONTEXT_WINDOW_MAX + 1, "max_input_tokens": 400}, 400),
    ({"context_length": 1.5}, None),
])
def test_context_is_first_valid_integer_without_guessing(fields, expected):
    result = _list(lambda _: httpx.Response(200, json={"data": [{"id": "a", **fields}]}))
    assert result.models[0].context_window == expected


@pytest.mark.parametrize("variant", ["builtin", "long-context", "unknown"])
def test_unset_price_defaults_preserves_builtin_prices_per_field(variant):
    provider = ProviderSpec("p", "Provider", VendorType.CUSTOM, AuthKind.API_KEY)
    name = next(iter(BUILTIN_PRICES))
    if variant == "long-context":
        name += "[1m]"
    elif variant == "unknown":
        name = "unknown-listing-model"
    price = model_price(provider, name)
    assert unset_price_defaults(provider, name) == {
        field: None if getattr(price, field) is not None else 0.0 for field in PRICE_FIELDS}
    assert any(value is None for value in unset_price_defaults(provider, name).values()) == (variant != "unknown")


def _provider(monkeypatch, *, row=None, key="fake-private-model-listing-key", models=()):
    async def get_provider(_):
        return row or {"id": "p", "name": "Provider", "vendor_type": "custom", "auth": "api_key",
                       "endpoints": [{"protocol": "openai-chat", "base_url": "https://example.invalid/v1"},
                                     {"protocol": "openai-responses", "base_url": "https://example.invalid/v1"},
                                     {"protocol": "anthropic-messages", "base_url": ""}]}

    async def get_key(_):
        return key

    async def get_models(**_):
        return list(models)
    monkeypatch.setattr(listing.db, "get_provider", get_provider)
    monkeypatch.setattr(listing.db, "get_api_key", get_key)
    monkeypatch.setattr(listing.db, "list_models", get_models)


def test_fetch_groups_protocols_reports_registered_invalid_names_and_never_exposes_key(monkeypatch, caplog):
    key = "fake-private-model-listing-key"
    builtin = next(iter(BUILTIN_PRICES))
    _provider(monkeypatch, key=key, models=[{"name": builtin, "protocol": "openai-chat"}])
    requests = []

    def answer(request):
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {key}"
        return httpx.Response(200, json={"data": [{"id": builtin}, {"id": "unknown-model"},
                                                   {"id": "invalid@name"}, {"id": "  "}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await listing.fetch_provider_models("p", client=client)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(run())
    assert len(requests) == 1
    assert result["listings"][0]["state"] == "skipped"
    served = result["listings"][1]
    assert served["protocols"] == ["openai-responses", "openai-chat"]
    models = {item["name"]: item for item in served["models"]}
    assert models[builtin]["registered"] == ["openai-chat"] and models[builtin]["builtin_price"]
    assert not models["unknown-model"]["builtin_price"]
    assert not models["invalid@name"]["addable"] and models["invalid@name"]["problem"]
    assert not models["  "]["addable"] and models["  "]["problem"] == "a model needs a name"
    assert key not in json.dumps(result) and key not in caplog.text


def test_fetch_timeout_does_not_hide_another_endpoint(monkeypatch):
    row = {"id": "p", "name": "Provider", "vendor_type": "custom", "auth": "api_key", "endpoints": [
        {"protocol": "anthropic-messages", "base_url": "https://slow.invalid"},
        {"protocol": "openai-chat", "base_url": "https://ok.invalid/v1"}]}
    _provider(monkeypatch, row=row)
    monkeypatch.setattr(listing, "LISTING_BUDGET_S", 0.02)

    async def answer(request):
        if request.url.host == "slow.invalid":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"data": [{"id": "a"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await listing.fetch_provider_models("p", client=client)
    result = asyncio.run(run())
    assert [entry["state"] for entry in result["listings"]] == ["failed", "listed"]
    assert result["listings"][0]["problem"] == "no answer within 15 seconds"


def test_login_skipped_without_reading_key_or_models(monkeypatch):
    _provider(monkeypatch, row={"id": "p", "name": "Codex", "vendor_type": "codex-login", "auth": "oauth",
                                "oauth_runtime_type": "codex"})

    async def forbidden(*args, **kwargs):
        pytest.fail("login listing must not read secrets or registered models")
    monkeypatch.setattr(listing.db, "get_api_key", forbidden)
    monkeypatch.setattr(listing.db, "list_models", forbidden)
    result = asyncio.run(listing.fetch_provider_models("p"))
    assert len(result["listings"]) == 1
    assert result["listings"][0] == {"family": "openai", "base_url": "", "protocols": ["openai-responses"],
        "state": "skipped", "problem": listing.ListingProblem.LOGIN.text(), "truncated": False, "models": []}


def test_missing_provider(monkeypatch):
    async def missing(_):
        return None
    monkeypatch.setattr(listing.db, "get_provider", missing)
    with pytest.raises(RegistryNotFound):
        asyncio.run(listing.fetch_provider_models("missing"))


def test_unset_unknown_prices_count_tokens_as_free_without_exceeding_a_cap() -> None:
    from looper.agents.profile_registry import ModelSpec, ResolvedModel, Endpoint
    from looper.agents.runtimes import ApiProtocol
    from looper.agents.runtimes.pricing import RunSpend, usage_event
    from looper.agents.run_usage import Usage

    provider = ProviderSpec("prov-free", "Free account", VendorType.CUSTOM, AuthKind.API_KEY)
    model = ModelSpec("model-free", provider.id, "unknown-gateway-model", ApiProtocol.OPENAI_CHAT,
                      **unset_price_defaults(provider, "unknown-gateway-model"))
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "https://example.invalid/v1"))
    spend = RunSpend(resolved, model.name, cap=0.0)
    assert not spend.add(usage_event(model.name, Usage(input_tokens=1000, output_tokens=2000,
        cache_read_tokens=3000, cache_creation_tokens=4000)))
    assert spend.usage.cost_usd == 0.0 and spend.usage.input_tokens == 1000
    assert spend.budget_verifiable and not spend.exceeded
