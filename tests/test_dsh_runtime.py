"""DeepSeek policy, without registering an executable DSH adapter."""

from dataclasses import replace

import pytest

from looper.agents import runtimes
from looper.agents.profile_registry import CatalogModel, ProviderSpec, RegistryError, RuntimeSpec, check_pairing
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported


MODELS = ("deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]")
PROVIDER = ProviderSpec("gateway", "Messages gateway", AuthKind.API_KEY,
                        frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                        base_url="https://gateway.example/anthropic/v1/", api_key="test-dsh-key")


@pytest.fixture
def dsh_policy(monkeypatch: pytest.MonkeyPatch):
    """Only tests expose this policy as an adapter, exercising the real registry gates."""
    from looper.agents.runtimes import dsh
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    class PolicyRuntime(ClaudeCodeRuntime):
        type = "dsh-policy-test"
        label = "DSH policy test"
        auth_kinds = frozenset({AuthKind.API_KEY})

        def check_pairing(self, provider, model, effort=None):
            dsh.validate_provider(provider, model, effort)

        def bind_provider(self, provider, model, effort):
            return dsh.bind_provider(provider, model, effort)

    adapter = PolicyRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, adapter.type, adapter)
    return adapter


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("effort", [None, "", "low", "high", "max"])
def test_dsh_pairing_and_binding_accept_exact_models_without_reading_a_key(dsh_policy, model, effort):
    from looper.agents.runtimes.dsh import validate_provider

    provider = replace(PROVIDER, catalog=(CatalogModel(model),), api_key=None)
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model, effort)
    policy = validate_provider(provider, model, effort)
    assert policy.route == "deepseek-official"
    assert policy.model == {"deepseek-flash": "deepseek-flash", "deepseek-flash[1m]": "deepseek-flash",
                            "deepseek-v4-pro": "deepseek-v4-pro",
                            "deepseek-v4-pro[1m]": "deepseek-v4-pro"}[model]
    assert policy.effort == (effort or "high")
    assert policy.base_url == provider.base_url
    binding = dsh_policy.bind_provider(replace(provider, api_key=PROVIDER.api_key), model, effort)
    assert dict(binding.env)["DEEPSEEK_API_KEY"] == PROVIDER.api_key
    assert PROVIDER.api_key not in repr(binding)


@pytest.mark.parametrize(("changes", "model", "effort"), [
    ({"auth": AuthKind.OAUTH, "oauth_runtime_type": "dsh-policy-test"}, "deepseek-flash", None),
    ({"protocols": frozenset({ApiProtocol.OPENAI_CHAT})}, "deepseek-flash", None),
    ({"protocols": frozenset({ApiProtocol.OPENAI_RESPONSES})}, "deepseek-flash", None),
    ({}, "gpt-6.1-sol", None),
    ({}, "deepseek-chat", None),
    ({}, "deepseek-flash-20261020", None),
    ({}, "deepseek-flash[1m][1m]", None),
    ({}, "DeepSeek-flash", None),
    ({}, "deepseek-flash", "off"),
    ({}, "deepseek-flash", "medium"),
    ({}, "deepseek-flash", "xhigh"),
    ({"base_url": "ftp://gateway.example"}, "deepseek-flash", None),
    ({"base_url": "gateway.example/anthropic"}, "deepseek-flash", None),
    ({"base_url": "https://"}, "deepseek-flash", None),
    ({"base_url": "https://gateway.example/\nmessages"}, "deepseek-flash", None),
    ({"catalog": (CatalogModel("deepseek-flash[1m]"),)}, "deepseek-flash", None),
])
def test_dsh_rejections_reach_registry_and_launch(dsh_policy, changes, model, effort):
    from looper.agents.runtimes.dsh import validate_provider

    provider = replace(PROVIDER, **changes)
    with pytest.raises(RegistryError):
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model, effort)
    with pytest.raises(RuntimeUnsupported):
        validate_provider(provider, model, effort)
    with pytest.raises(RuntimeUnsupported):
        dsh_policy.bind_provider(provider, model, effort)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_dsh_missing_key_is_refused_only_at_launch(dsh_policy, key, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-inherit")
    provider = replace(PROVIDER, api_key=key)
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-flash")
    with pytest.raises(RuntimeUnsupported, match="API key"):
        dsh_policy.bind_provider(provider, "deepseek-flash", "")


@pytest.mark.parametrize("base_url", ["", "https://api.deepseek.com/anthropic",
                                     "https://gateway.example/custom/v1", "http://localhost:9999/messages/",
                                     "https://gateway.example/v1/chat/completions"])
def test_dsh_keeps_the_explicit_endpoint_without_rewriting(base_url):
    from looper.agents.runtimes.dsh import validate_provider

    policy = validate_provider(replace(PROVIDER, base_url=base_url), "deepseek-flash")
    assert policy.base_url == (base_url or "https://api.deepseek.com/anthropic")


def test_dsh_uses_protocol_and_model_instead_of_vendor_heuristics(dsh_policy):
    provider = replace(PROVIDER, id="other-vendor", name="Other vendor", base_url="https://other.example/api",
                       protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}))
    check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-v4-pro[1m]")


def test_dsh_policy_does_not_register_a_runtime_or_change_other_pairings():
    from looper.agents.runtimes import dsh  # noqa: F401

    with pytest.raises(RuntimeUnsupported):
        runtimes.adapter("dsh")
    # A default hook must leave existing runtime/model/effort combinations alone.
    check_pairing(RuntimeSpec("claude", "claude", "claude-code"), PROVIDER, "other-model", "xhigh")


def test_deepseek_messages_preset_still_pairs_without_changing_its_catalog(dsh_policy):
    from looper.agents.provider_presets import PRESETS

    preset = next(p for p in PRESETS if p.id == "deepseek")
    api = next(a for a in preset.apis if ApiProtocol.ANTHROPIC_MESSAGES in a.protocols)
    provider = replace(PROVIDER, base_url=api.base_url, protocols=frozenset(api.protocols),
                       catalog=tuple(CatalogModel(m.name) for m in preset.models), api_key=None)
    for model in provider.models:
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, model)
    with pytest.raises(RegistryError):
        check_pairing(RuntimeSpec("test", "test", dsh_policy.type), provider, "deepseek-flash")
