"""The provider / single-protocol model / profile contract (LLM models RFC §3)."""
from dataclasses import FrozenInstanceError, fields

import pytest

from looper.agents.profile_registry import (
    AgentProfile, Endpoint, ModelSpec, ProviderSpec, RegistryError, ResolvedModel, RuntimeSpec,
)
from looper.agents.provider_presets import PRESETS, vendor_defaults
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType


PRICES = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")
LOGIN_VENDORS = {
    VendorType.CLAUDE_SUBSCRIPTION: ("claude-code", ApiProtocol.ANTHROPIC_MESSAGES),
    VendorType.CODEX_LOGIN: ("codex", ApiProtocol.OPENAI_RESPONSES),
    VendorType.GROK_LOGIN: ("grok", ApiProtocol.OPENAI_RESPONSES),
}


def provider(**changes):
    return ProviderSpec(**{"id": "prov-test", "name": "test", "vendor_type": VendorType.CUSTOM,
                           "auth": AuthKind.API_KEY, **changes})


def model(**changes):
    return ModelSpec(**{"id": "model-test", "provider_id": "prov-test", "name": "Exact.Model[1m]",
                        "protocol": ApiProtocol.OPENAI_CHAT, **changes})


@pytest.mark.parametrize("protocol", list(ApiProtocol))
def test_single_protocol_models_resolve_exactly(protocol):
    spec = model(protocol=protocol, background_model="background[1m]", catalog_order=7)
    endpoint = Endpoint("prov-test", protocol, "https://example.invalid/v1")
    resolved = ResolvedModel(spec, provider(), endpoint)
    assert resolved.model is spec and resolved.endpoint.protocol is protocol
    assert spec.name == "Exact.Model[1m]" and spec.background_model == "background[1m]"
    assert spec.catalog_order == 7


@pytest.mark.parametrize("field,value", [("provider_id", "another"), ("name", "other"),
                                         ("protocol", ApiProtocol.OPENAI_RESPONSES)])
def test_model_identity_is_immutable(field, value):
    with pytest.raises(FrozenInstanceError):
        setattr(model(), field, value)
    with pytest.raises(RegistryError):
        model().updated(**{field: value})
    assert model().updated(context_window=42).context_window == 42


@pytest.mark.parametrize("protocol", [None, [], ["openai-chat"], "unknown", True])
def test_model_requires_one_known_protocol(protocol):
    with pytest.raises(RegistryError):
        model(protocol=protocol)


@pytest.mark.parametrize("vendor", list(VendorType))
def test_vendor_auth_and_login_runtime_are_explicit(vendor):
    login = LOGIN_VENDORS.get(vendor)
    auth, runtime = (AuthKind.OAUTH, login[0]) if login else (AuthKind.API_KEY, None)
    assert provider(vendor_type=vendor, auth=auth, oauth_runtime_type=runtime).vendor_type is vendor
    with pytest.raises(RegistryError):
        provider(vendor_type=vendor, auth=AuthKind.API_KEY if login else AuthKind.OAUTH,
                 oauth_runtime_type=runtime)
    with pytest.raises(RegistryError):
        provider(vendor_type=vendor, auth=auth, oauth_runtime_type="omp")


@pytest.mark.parametrize("vendor", list(LOGIN_VENDORS))
def test_login_endpoint_is_owned_by_runtime(vendor):
    runtime, protocol = LOGIN_VENDORS[vendor]
    account = provider(vendor_type=vendor, auth=AuthKind.OAUTH, oauth_runtime_type=runtime)
    ResolvedModel(model(protocol=protocol), account, Endpoint(account.id, protocol, ""))
    with pytest.raises(RegistryError):
        ResolvedModel(model(protocol=protocol), account, Endpoint(account.id, protocol, "https://example.invalid"))
    other = next(p for p in ApiProtocol if p is not protocol)
    with pytest.raises(RegistryError):
        ResolvedModel(model(protocol=other), account, Endpoint(account.id, other, ""))


@pytest.mark.parametrize("url", [
    "https://user:secret@example.invalid/v1", "https://secret@example.invalid",
    "https://example.invalid/v1?api_key=secret", "https://example.invalid?API-KEY=secret",
    "https://example.invalid?access_token=secret", "https://example.invalid?%74oken=secret",
    "https://example.invalid?Authorization=secret", "https://example.invalid#api_key=secret",
    "https://example.invalid/v1\n", "https://example.invalid/has space", "ftp://example.invalid",
    "https://", "https://example.invalid:bad", "https://example.invalid:99999", "https://[broken",
    "https://example.invalid/" + "x" * 2_000,
])
def test_url_rejects_secrets_and_malformed_inputs_without_echoing_them(url):
    with pytest.raises(RegistryError) as error:
        Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/v1", "https://example.invalid/v1",
                                  "https://example.invalid/v1?api-version=2026-01-01"])
def test_valid_endpoint_urls(url):
    assert Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, url).base_url == url


def test_api_key_empty_endpoint_requires_explicit_legacy_default():
    with pytest.raises(RegistryError):
        ResolvedModel(model(), provider(), Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, ""))
    ResolvedModel(model(), provider(), Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, "", True))
    with pytest.raises(RegistryError):
        Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, "https://example.invalid", True)
    with pytest.raises(RegistryError):
        Endpoint("prov-test", ApiProtocol.OPENAI_CHAT, "", "false")


@pytest.mark.parametrize("context", [None, 1, 100_000_000])
def test_context_bounds(context):
    assert model(context_window=context).context_window == context


@pytest.mark.parametrize("context", [0, -1, 100_000_001, 1.5, True, "1000"])
def test_invalid_context(context):
    with pytest.raises(RegistryError):
        model(context_window=context)


@pytest.mark.parametrize("price_field", PRICES)
@pytest.mark.parametrize("value", [None, 0, 1.25, 10_000])
def test_price_null_and_explicit_free(price_field, value):
    assert getattr(model(**{price_field: value}), price_field) == value


# One value per way ``_price`` refuses (not a number, below 0, above the maximum, nan, which no range holds), each
# kind of value on a different price field so that every field is shown to be checked.
@pytest.mark.parametrize("price_field,value", [
    (PRICES[0], float("nan")), (PRICES[1], -0.01), (PRICES[2], 10_000.01), (PRICES[3], True),
    (PRICES[0], "0"), (PRICES[1], -float("inf")), (PRICES[2], 10**1000),
])
def test_invalid_prices(price_field, value):
    with pytest.raises(RegistryError):
        model(**{price_field: value})


@pytest.mark.parametrize("efforts,expected", [(None, None), ([], ()), (["low", "high"], ("low", "high"))])
def test_efforts_unknown_and_unsupported_are_distinct(efforts, expected):
    assert model(efforts=efforts).efforts == expected


@pytest.mark.parametrize("efforts", [["high", "high"], ["High"], [""], [1], "high", ["e" * 33],
                                     [f"level{i}" for i in range(11)]])
def test_invalid_efforts(efforts):
    with pytest.raises(RegistryError):
        model(efforts=efforts)


@pytest.mark.parametrize("name", ["DeepSeek-Flash[1m]", "deepseek-flash", "org/Model:20261020"])
def test_exact_model_names_are_preserved(name):
    assert model(name=name).name == name


@pytest.mark.parametrize("name", [None, "", "-flag", "a b", "a\nmodel", "a" * 201])
def test_invalid_names(name):
    with pytest.raises(RegistryError):
        model(name=name)
    if name:
        with pytest.raises(RegistryError):
            model(background_model=name)


@pytest.mark.parametrize("order", [-1, True, 0.5, None])
def test_invalid_catalog_order(order):
    with pytest.raises(RegistryError):
        model(catalog_order=order)


@pytest.mark.parametrize("changed", ["provider", "endpoint-provider", "endpoint-protocol"])
def test_resolved_model_refuses_mismatched_references(changed):
    account = provider(id="prov-other") if changed == "provider" else provider()
    endpoint = Endpoint("prov-other" if changed == "endpoint-provider" else "prov-test",
                        ApiProtocol.OPENAI_RESPONSES if changed == "endpoint-protocol" else ApiProtocol.OPENAI_CHAT,
                        "https://example.invalid")
    with pytest.raises(RegistryError):
        ResolvedModel(model(), account, endpoint)


def test_provider_only_has_account_properties_and_hides_key():
    names = {f.name for f in fields(ProviderSpec)}
    assert not names & {"protocols", "base_url", "catalog", "models"}
    assert {"vendor_type", "max_concurrency", "env", "auth", "oauth_runtime_type"} <= names
    assert "test-secret-value" not in repr(provider(api_key="test-secret-value"))
    with pytest.raises(RegistryError):
        provider(vendor_type="mystery-vendor")
    with pytest.raises(RegistryError):
        provider().updated(vendor_type=VendorType.DEEPSEEK)


def test_profile_references_model_id_and_has_only_runtime_options():
    profile = AgentProfile("agent-test", "test", RuntimeSpec("rt-test", "test", "omp"), "model-test",
                           effort="high", custom_prompt="instructions")
    assert profile.model_id == "model-test"
    assert {f.name for f in fields(AgentProfile)} == {
        "id", "name", "runtime", "model_id", "effort", "agent_teams", "custom_prompt"}
    assert profile.record()["model_id"] == "model-test"


@pytest.mark.parametrize("kind,payload", [
    (ModelSpec, {"id": "model-test", "provider_id": "prov-test", "name": "m", "protocol": "openai-chat"}),
    (Endpoint, {"provider_id": "prov-test", "protocol": "openai-chat", "base_url": "https://example.invalid"}),
    (ProviderSpec, {"id": "prov-test", "name": "test", "vendor_type": "custom", "auth": "api_key"}),
])
def test_entry_readback_is_typed_and_rejects_unknown_fields(kind, payload):
    assert isinstance(kind.from_entry(payload), kind)
    with pytest.raises(RegistryError):
        kind.from_entry({**payload, "typo": True})


@pytest.mark.parametrize("field", ["system_prompt_mode", "system_prompt_mode_choice", "provider_id", "model"])
def test_profile_refuses_obsolete_and_unknown_fields(field):
    with pytest.raises(RegistryError):
        AgentProfile.from_entry({"id": "agent-test", "name": "test", "runtime": RuntimeSpec("rt", "omp", "omp"),
                                 "model_id": "model-test", field: "legacy"})


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.id)
def test_vendor_suggestions_use_each_api_catalog(preset):
    defaults = vendor_defaults(VendorType(preset.id))
    expected = [(m.name, p, m.context_window, m.background_model)
                for api in preset.apis for p in api.protocols for m in preset.models_for(api)]
    assert [(m.name, m.protocol, m.context_window, m.background_model) for m in defaults.models] == expected
    assert [(e.protocol, e.base_url) for e in defaults.endpoints] == [
        (p, api.base_url) for api in preset.apis for p in api.protocols]
    assert [m.catalog_order for m in defaults.models] == list(range(len(expected)))
    assert defaults.env == preset.env


def test_opencode_model_families_do_not_cross_protocols():
    defaults = vendor_defaults(VendorType.OPENCODE)
    assert {m.name for m in defaults.models if m.protocol is ApiProtocol.OPENAI_CHAT} == {
        "deepseek-v4-pro", "glm-5.3", "kimi-k3"}
    assert all(m.name.startswith("gpt-") for m in defaults.models if m.protocol is ApiProtocol.OPENAI_RESPONSES)
    assert all(m.name.startswith("claude-") for m in defaults.models if m.protocol is ApiProtocol.ANTHROPIC_MESSAGES)


@pytest.mark.parametrize("runtime_type,protocols", [
    ("claude-code", {ApiProtocol.ANTHROPIC_MESSAGES}),
    ("codex", {ApiProtocol.OPENAI_RESPONSES}),
    ("omp", set(ApiProtocol)),
    ("grok", set()),  # Grok currently accepts only its own login.
])
@pytest.mark.parametrize("protocol", list(ApiProtocol))
def test_runtime_requires_the_models_protocol_and_auth_kind(runtime_type, protocols, protocol):
    resolved = ResolvedModel(model(protocol=protocol), provider(),
                             Endpoint("prov-test", protocol, "https://example.invalid"))
    runtime = RuntimeSpec("rt-test", "test", runtime_type)
    if protocol in protocols:
        resolved.check_runtime(runtime)
    else:
        with pytest.raises(RegistryError):
            resolved.check_runtime(runtime)


@pytest.mark.parametrize("vendor", list(LOGIN_VENDORS))
@pytest.mark.parametrize("runtime_type", ["claude-code", "codex", "omp", "grok"])
def test_login_pairing_cannot_cross_runtimes(vendor, runtime_type):
    owner, protocol = LOGIN_VENDORS[vendor]
    account = provider(vendor_type=vendor, auth=AuthKind.OAUTH, oauth_runtime_type=owner)
    resolved = ResolvedModel(model(protocol=protocol), account, Endpoint(account.id, protocol, ""))
    runtime = RuntimeSpec("rt-test", "test", runtime_type)
    if runtime_type == owner:
        resolved.check_runtime(runtime)
    else:
        with pytest.raises(RegistryError):
            resolved.check_runtime(runtime)


def test_json_provider_environment_readback_and_key_isolation():
    payload = {"id": "prov-test", "name": "test", "vendor_type": "custom", "auth": "api_key",
               "env": [{"key": "ACCOUNT_REGION", "value": "test"}]}
    account = ProviderSpec.from_entry(payload)
    assert account.env == (("ACCOUNT_REGION", "test"),)
    for key in ("API_KEY", "ANTHROPIC_BASE_URL"):
        with pytest.raises(RegistryError):
            ProviderSpec.from_entry({**payload, "env": [{"key": key, "value": "test"}]})
    with pytest.raises(RegistryError):
        provider(env=[{"key": "ACCOUNT_REGION", "value": "test"}])


@pytest.mark.parametrize("cls", [ProviderSpec, Endpoint, ModelSpec, AgentProfile])
def test_missing_required_fields_are_registry_errors(cls):
    with pytest.raises(RegistryError):
        cls.from_entry({})
    with pytest.raises(RegistryError):
        cls.from_entry([])


def test_model_mutable_inputs_are_copied_to_immutable_state():
    efforts = ["low", "high"]
    spec = model(efforts=efforts)
    efforts.clear()
    assert spec.efforts == ("low", "high")
    assert spec.updated(efforts=[]).efforts == ()
    assert spec.updated(background_model="outside-catalog").background_model == "outside-catalog"


@pytest.mark.parametrize("vendor", list(VendorType))
def test_defaults_are_creation_only_with_unique_opaque_model_ids(vendor):
    defaults = vendor_defaults(vendor, provider_id="prov-test")
    assert all(e.provider_id == "prov-test" for e in defaults.endpoints)
    assert all(m.provider_id == "prov-test" and m.id.startswith("model-") for m in defaults.models)
    assert len({m.id for m in defaults.models}) == len(defaults.models)
    assert len({e.protocol for e in defaults.endpoints}) == len(defaults.endpoints)
    assert len({(m.name, m.protocol) for m in defaults.models}) == len(defaults.models)
    assert not {m.id for m in defaults.models} & {m.id for m in vendor_defaults(vendor).models}


@pytest.mark.parametrize("field,value", [("agent_teams", "false"), ("model_id", ""),
                                        ("effort", "unsupported"), ("custom_prompt", "bad\x00prompt")])
def test_profile_runtime_options_are_validated(field, value):
    payload = {"id": "agent-test", "name": "test", "runtime": RuntimeSpec("rt", "omp", "omp"),
               "model_id": "model-test", field: value}
    with pytest.raises(RegistryError):
        AgentProfile.from_entry(payload)
