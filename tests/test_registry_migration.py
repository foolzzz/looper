"""Legacy registry conversion preserves the patched adapters' observable choices."""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
from looper.agents.registry_migration import LEGACY_RULES, MigrationBlocked, convert
from looper.agents.runtimes import ApiProtocol as P, AuthKind, VendorType
from looper.agents.runtimes import codex, omp


def provider(**changes):
    return {"id": "prov-test", "name": "account", "auth": "api_key", "oauth_runtime_type": None,
            "protocols": [p.value for p in P], "base_url": "https://example.invalid/v1",
            "models": [], "env": [], "max_concurrency": None, **changes}


def profile(runtime="omp", **changes):
    return {"id": "agent-test", "name": "test", "runtime_id": f"rt-{runtime}",
            "provider_id": "prov-test", "model": "Exact.Model[1m]", "effort": "high",
            "agent_teams": runtime == "claude-code", "custom_prompt": "instructions", **changes}


RUNTIMES = [RuntimeSpec(f"rt-{name}", name, name) for name in ("omp", "claude-code", "codex", "grok")]


def run(account=None, profiles=None, **kwargs):
    return convert(RUNTIMES, [provider() if account is None else account],
                   [profile()] if profiles is None else profiles, **kwargs)


@pytest.mark.parametrize("protocols,expected", [
    (list(P), P.OPENAI_RESPONSES),
    ([P.ANTHROPIC_MESSAGES, P.OPENAI_CHAT], P.OPENAI_CHAT),
    ([P.ANTHROPIC_MESSAGES], P.ANTHROPIC_MESSAGES),
])
def test_omp_uses_the_old_priority_not_input_or_enum_order(protocols, expected):
    result = run(provider(protocols=protocols))
    assert result.models[result.profiles["agent-test"].model_id].protocol is expected
    old = LegacyProviderSpec("prov-test", "account", AuthKind.API_KEY, frozenset(protocols))
    assert dict(omp.API_NAMES)[expected] == omp.provider_api(old)


def test_default_rules_match_patched_adapter_constants():
    assert LEGACY_RULES.protocol_priority["omp"] == tuple(p for p, _ in omp.API_NAMES)
    for protocol, api in omp.API_NAMES:
        assert LEGACY_RULES.default_urls[("omp", protocol)] == omp.DEFAULT_BASE_URLS[api]
    assert LEGACY_RULES.default_urls[("codex", P.OPENAI_RESPONSES)] == codex.DEFAULT_BASE_URL


def test_finite_catalog_copies_every_field_and_order_to_every_protocol():
    catalog = [
        {"name": "z[1m]", "context_window": 1_000_000, "background_model": "outside[1m]",
         "input_usd_per_mtok": 0, "output_usd_per_mtok": 2.5, "cache_read_usd_per_mtok": None,
         "cache_write_usd_per_mtok": 1, "efforts": []},
        {"name": "a", "input_usd_per_mtok": 9, "efforts": ["high", "low"]},
        {"name": "unused"},
    ]
    result = run(provider(models=catalog), [profile(model="z[1m]")])
    assert len(result.models) == len(catalog) * len(P)
    for protocol in P:
        models = [m for m in result.models.values() if m.protocol is protocol]
        assert [m.name for m in models] == [m["name"] for m in catalog]
        assert [m.catalog_order for m in models] == [0, 1, 2]
        for actual, expected in zip(models, catalog):
            for key, value in expected.items():
                assert getattr(actual, key) == (tuple(value) if key == "efforts" else value)


def test_open_catalog_materializes_only_exact_references_and_deduplicates():
    profiles = [profile(), profile(id="agent-second"), profile("claude-code", id="agent-claude"),
                profile("codex", id="agent-codex", model="other")]
    result = run(profiles=profiles)
    assert {(m.name, m.protocol) for m in result.models.values()} == {
        ("Exact.Model[1m]", P.OPENAI_RESPONSES), ("Exact.Model[1m]", P.ANTHROPIC_MESSAGES),
        ("other", P.OPENAI_RESPONSES)}
    assert result.profiles["agent-test"].model_id == result.profiles["agent-second"].model_id
    for model in result.models.values():
        assert all(getattr(model, key) is None for key in (
            "context_window", "background_model", "efforts", "input_usd_per_mtok", "output_usd_per_mtok",
            "cache_read_usd_per_mtok", "cache_write_usd_per_mtok"))
    assert not run(profiles=[]).models


def test_profiles_keep_identity_runtime_and_all_options_and_conversion_is_pure_stable():
    account = provider(env=[{"key": "EXTRA_SETTING", "value": "yes"}], max_concurrency=7, api_key="test-key")
    profiles = [profile("claude-code"), profile(id="agent-omp")]
    before = deepcopy((account, profiles))
    result = run(account, profiles)
    assert (account, profiles) == before
    assert result == run(account, list(reversed(profiles)))
    for old in profiles:
        new = result.profiles[old["id"]]
        for key in ("id", "name", "effort", "agent_teams", "custom_prompt"):
            assert getattr(new, key) == old[key]
        assert new.runtime.id == old["runtime_id"]
        assert result.models[new.model_id].name == old["model"]
    assert result.providers[account["id"]].env == (("EXTRA_SETTING", "yes"),)
    assert result.providers[account["id"]].max_concurrency == 7
    assert result.providers[account["id"]].api_key == "test-key"
    assert "test-key" not in repr(result)
    assert all(UUID(model.id.removeprefix("model-")).version == 5 for model in result.models.values())


def test_model_id_is_pinned_to_the_complete_natural_key():
    result = run()
    assert result.profiles["agent-test"].model_id == "model-7d1a3f1d-6409-5322-94ac-0cbfd0297b59"
    account = provider(id="prov-other")
    rows = [profile(id="agent-other", provider_id="prov-other")]
    other = convert(RUNTIMES, [account], rows)
    assert set(result.models).isdisjoint(other.models)


def test_accounts_stay_separate_and_open_catalog_order_is_local():
    first = provider(id="prov-first", api_key="same-key")
    second = provider(api_key="same-key")
    profiles = [profile(), profile(id="agent-first", provider_id="prov-first")]
    result = convert(RUNTIMES, [first, second], profiles)
    assert len(result.providers) == 2 and len(result.models) == 2
    assert set(run().models) <= set(result.models)
    assert all(model.catalog_order == 0 for model in result.models.values())
    assert result.report["profiles_mapped"] == 2
    assert result.report["models_from_open_catalog"] == 2
    assert all(isinstance(value, int) for value in result.report.values())


@pytest.mark.parametrize("url,protocols,vendor", [
    ("https://api.deepseek.com", [P.OPENAI_CHAT, P.OPENAI_RESPONSES], VendorType.DEEPSEEK),
    ("https://api.deepseek.com/anthropic", [P.ANTHROPIC_MESSAGES], VendorType.DEEPSEEK),
    ("https://api.deepseek.com/", [P.OPENAI_CHAT], VendorType.CUSTOM),
    ("https://api.deepseek.com", [P.ANTHROPIC_MESSAGES], VendorType.CUSTOM),
    ("https://api.deepseek.com.evil.invalid", [P.OPENAI_CHAT], VendorType.CUSTOM),
    ("https://example.invalid/v1", [P.OPENAI_CHAT], VendorType.CUSTOM),
])
def test_vendor_recognition_uses_exact_endpoint_and_protocol_not_name(url, protocols, vendor):
    result = run(provider(name="deepseek", base_url=url, protocols=protocols))
    assert result.providers["prov-test"].vendor_type is vendor
    assert len(result.models) == 1  # no vendor suggestions injected
    assert all(e.base_url == url and not e.use_runtime_default for e in result.endpoints.values())


def test_explicit_vendor_wins_over_endpoint_and_name():
    assert run(provider(vendor_type="glm-zai")).providers["prov-test"].vendor_type is VendorType.GLM_ZAI


@pytest.mark.parametrize("runtime,protocol,vendor", [
    ("claude-code", P.ANTHROPIC_MESSAGES, VendorType.CLAUDE_SUBSCRIPTION),
    ("codex", P.OPENAI_RESPONSES, VendorType.CODEX_LOGIN),
    ("grok", P.OPENAI_RESPONSES, VendorType.GROK_LOGIN),
])
def test_logins_are_identified_by_auth_and_runtime(runtime, protocol, vendor):
    result = run(provider(auth="oauth", oauth_runtime_type=runtime, protocols=[protocol], base_url=""),
                 [profile(runtime)])
    assert result.providers["prov-test"].vendor_type is vendor
    endpoint = result.endpoints[("prov-test", protocol)]
    assert endpoint.base_url == "" and not endpoint.use_runtime_default


@pytest.mark.parametrize("protocol,runtime,url", [
    (P.ANTHROPIC_MESSAGES, "claude-code", "https://api.anthropic.com"),
    (P.OPENAI_RESPONSES, "codex", "https://api.openai.com/v1"),
    (P.OPENAI_CHAT, "omp", "https://api.openai.com/v1"),
])
def test_empty_url_keeps_omission_distinct_from_explicit_default(protocol, runtime, url):
    omitted = run(provider(protocols=[protocol], base_url=""), [profile(runtime)])
    explicit = run(provider(protocols=[protocol], base_url=url), [profile(runtime)])
    endpoint = omitted.endpoints[("prov-test", protocol)]
    assert endpoint.base_url == "" and endpoint.use_runtime_default
    assert omitted.default_urls[("prov-test", protocol)] == url
    assert explicit.endpoints[("prov-test", protocol)].base_url == url
    assert not explicit.endpoints[("prov-test", protocol)].use_runtime_default
    assert next(iter(omitted.models)) == next(iter(explicit.models))


def test_default_addresses_must_agree_for_the_same_protocol():
    defaults = dict(LEGACY_RULES.default_urls)
    defaults[("codex", P.OPENAI_RESPONSES)] = "https://different.invalid/v1"
    rules = replace(LEGACY_RULES, default_urls=defaults)
    with pytest.raises(MigrationBlocked, match="default_endpoint_conflict"):
        run(provider(base_url=""), [profile(), profile("codex", id="agent-codex")], rules=rules)
    # The conflict is relevant only when the old URL was omitted.
    run(profiles=[profile(), profile("codex", id="agent-codex")], rules=rules)


def test_missing_default_rule_blocks_instead_of_guessing():
    with pytest.raises(MigrationBlocked, match="unknown_default_endpoint"):
        run(provider(base_url=""), rules=replace(LEGACY_RULES, default_urls={}))


def test_caller_supplied_old_rules_control_protocol_choice():
    priorities = {**LEGACY_RULES.protocol_priority, "omp": (P.ANTHROPIC_MESSAGES, P.OPENAI_CHAT)}
    result = run(rules=replace(LEGACY_RULES, protocol_priority=priorities))
    assert result.models[result.profiles["agent-test"].model_id].protocol is P.ANTHROPIC_MESSAGES


def test_conversion_failure_does_not_mutate_inputs_or_expose_credentials():
    account = provider(api_key="test-secret", base_url="https://test-secret@example.invalid")
    before = deepcopy(account)
    with pytest.raises(MigrationBlocked) as exc:
        run(account)
    assert account == before
    assert "test-secret" not in str(exc.value)


@pytest.mark.parametrize("changes", [
    {"vendor_type": "unknown"}, {"models": [{"name": "duplicate"}, {"name": "duplicate"}]},
    {"models": [{"name": "model", "input_usd_per_mtok": -1}]},
    {"protocols": [P.OPENAI_CHAT, P.OPENAI_CHAT]},
    {"auth": "oauth", "oauth_runtime_type": "unknown", "base_url": ""},
])
def test_invalid_account_data_is_a_blocker(changes):
    with pytest.raises(MigrationBlocked):
        run(provider(**changes))


@pytest.mark.parametrize("account,profiles,code", [
    (provider(protocols=["unknown"]), [profile()], "unknown_protocol"),
    (provider(protocols=[]), [profile()], "invalid_protocols"),
    (provider(), [profile(runtime_id="missing")], "bad_runtime_reference"),
    (provider(), [profile(provider_id="missing")], "bad_provider_reference"),
    (provider(models=[{"name": "allowed"}]), [profile(model="other")], "model_outside_catalog"),
    (provider(protocols=[P.OPENAI_CHAT]), [profile("codex")], "incompatible_protocol"),
    (provider(), [profile("grok")], "incompatible_auth"),
    (provider(auth="oauth", oauth_runtime_type="codex", protocols=[P.OPENAI_RESPONSES], base_url=""),
     [profile()], "incompatible_login"),
])
def test_invalid_legacy_pairings_block_the_whole_conversion(account, profiles, code):
    with pytest.raises(MigrationBlocked, match=code):
        run(account, profiles)


def test_unknown_runtime_blocks_even_without_a_profile():
    with pytest.raises(MigrationBlocked, match="unknown_runtime"):
        convert([RuntimeSpec("rt-unknown", "unknown", "unknown")], [], [])


@pytest.mark.parametrize("kind", ["runtime", "provider", "profile"])
def test_duplicate_ids_are_not_silently_overwritten(kind):
    with pytest.raises(MigrationBlocked, match="duplicate_id"):
        convert(RUNTIMES * (2 if kind == "runtime" else 1),
                [provider()] * (2 if kind == "provider" else 1),
                [profile()] * (2 if kind == "profile" else 1))
