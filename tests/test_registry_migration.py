"""Legacy registry conversion preserves the patched adapters' observable choices."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from looper.agents.profile_registry import (
    MODEL_FIELDS, CatalogModel, Endpoint, LegacyProviderSpec, ModelSpec, ProviderSpec, ResolvedModel, RuntimeSpec,
)
from looper.agents.registry_migration import LEGACY_RULES, MigrationBlocked, convert
from looper.agents.runtimes import ApiProtocol as P, AuthKind, VendorType
from looper.agents.runtimes import codex, dsh, omp, pi


def provider(**changes):
    return {"id": "prov-test", "name": "account", "auth": "api_key", "oauth_runtime_type": None,
            "protocols": [p.value for p in P], "base_url": "https://example.invalid/v1",
            "models": [], "env": [], "max_concurrency": None, **changes}


def profile(runtime="omp", **changes):
    return {"id": "agent-test", "name": "test", "runtime_id": f"rt-{runtime}",
            "provider_id": "prov-test", "model": "Exact.Model[1m]", "effort": "high",
            "agent_teams": runtime == "claude-code", "custom_prompt": "instructions", **changes}


RUNTIMES = [RuntimeSpec(f"rt-{name}", name, name) for name in ("omp", "claude-code", "codex", "grok", "pi", "dsh")]


def launch_for(provider, model, runtime):
    """Build the exact resolved input consumed by current runtime config writers."""
    from looper.agents.runtimes import ADAPTERS

    entry = provider.catalog_model(model) or CatalogModel(model)
    protocol, = provider.protocols
    account = ProviderSpec(provider.id, provider.name, VendorType.CUSTOM, provider.auth,
                           env=provider.env, api_key=provider.api_key)
    resolved = ResolvedModel(
        ModelSpec("model-test", account.id, model, protocol, **{k: getattr(entry, k) for k in MODEL_FIELDS[1:]}),
        account, Endpoint(account.id, protocol, provider.base_url, use_runtime_default=not provider.base_url))
    return SimpleNamespace(profile=SimpleNamespace(provider=resolved.binding_provider()), model=model, effort="high",
                           settings=SimpleNamespace(resolved_model=resolved), runtime=ADAPTERS[runtime],
                           library=SimpleNamespace(mcp_config=None))


REPORT_FIELDS = {
    "providers_before", "providers_after", "endpoints_after", "models_after", "profiles_before",
    "profiles_after", "profiles_mapped", "models_from_open_catalog", "providers_merged",
    "providers_kept_separate_by_reason", "legacy_default_endpoints", "unresolved_profiles",
    "binding_mismatches", "pricing_mismatches", "session_references_remapped",
}


def test_count_only_plan_has_a_nonempty_positive_control_and_no_identity_values():
    from looper.agents.registry_migration import RetentionReason, plan, snapshot

    old = snapshot(RUNTIMES, [provider(api_key="private-test-key")], [profile()])
    result = plan(old)
    assert result.applicable
    assert result.report.keys() == REPORT_FIELDS
    assert result.report["profiles_before"] == result.report["profiles_after"] == result.report["profiles_mapped"] == 1
    assert result.report["models_from_open_catalog"] == 1
    assert result.report["providers_kept_separate_by_reason"].keys() == {r.value for r in RetentionReason}

    def counts_only(value):
        if isinstance(value, dict):
            for item in value.values():
                counts_only(item)
        else:
            assert type(value) is int and value >= 0

    counts_only(result.report)
    assert old.bindings and old.pricing  # A saved old baseline, not two reads of the target.


@pytest.mark.parametrize("changes", [{"runtime_id": "missing"}, {"provider_id": "missing"},
                                         {"effort": "invalid"}, {"model": "outside"}])
def test_bad_profile_is_counted_and_never_silently_dropped(changes):
    from looper.agents.registry_migration import plan, snapshot

    rows = [profile(model="allowed"), profile(**{"id": "agent-bad", "model": "allowed", **changes})]
    result = plan(snapshot(RUNTIMES, [provider(models=[{"name": "allowed"}])], rows))
    assert not result.applicable
    assert result.report["profiles_before"] == 2
    assert result.report["profiles_mapped"] == 1
    assert result.report["unresolved_profiles"] == 1


@pytest.mark.parametrize("kind", ["binding", "pricing"])
def test_plan_detects_deliberate_target_mismatch_against_saved_old_inputs(monkeypatch, kind):
    from looper.agents import registry_migration as migration

    old = migration.snapshot(RUNTIMES, [provider(api_key="private-test-key")], [profile()])
    original = migration.convert

    def broken(*args, **kwargs):
        result = original(*args, **kwargs)
        if kind == "binding":
            key = ("prov-test", P.OPENAI_RESPONSES)
            result.endpoints[key] = replace(result.endpoints[key], base_url="https://wrong.invalid")
        else:
            identity = result.profiles["agent-test"].model_id
            result.models[identity] = replace(result.models[identity], input_usd_per_mtok=123)
        return result

    monkeypatch.setattr(migration, "convert", broken)
    result = migration.plan(old)
    assert not result.applicable
    assert result.report[f"{kind}_mismatches"] == 1
    assert result.report["profiles_mapped"] == 1


def test_plan_recomputes_and_counts_both_session_reference_kinds():
    from looper.agents.registry_migration import plan, snapshot

    accounts = [provider(id="prov-a", vendor_type="deepseek", api_key="same"),
                provider(id="prov-b", vendor_type="deepseek", api_key="same")]
    rows = [profile(provider_id="prov-b")]
    old = snapshot(RUNTIMES, accounts, rows, session_provider_ids=["prov-b", "prov-b", "prov-a", None])
    assert plan(old).applicable
    assert plan(old).report["session_references_remapped"] == 2
    accounts[1]["api_key"] = "changed"
    assert plan(snapshot(RUNTIMES, accounts, rows)).report["providers_merged"] == 0
    assert plan(old).report["providers_merged"] == 1  # The old snapshot is isolated from caller mutations.


@pytest.mark.parametrize("runtime,protocol,auth", [
    ("claude-code", P.ANTHROPIC_MESSAGES, "api_key"),
    ("codex", P.OPENAI_RESPONSES, "api_key"),
    ("omp", P.OPENAI_CHAT, "api_key"),
    ("claude-code", P.ANTHROPIC_MESSAGES, "oauth"),
    ("codex", P.OPENAI_RESPONSES, "oauth"),
    ("grok", P.OPENAI_RESPONSES, "oauth"),
])
@pytest.mark.parametrize("key", [None, "***", "synthetic-private-key"])
def test_static_preflight_accepts_unchanged_defaults_and_logins_without_discovery(monkeypatch, runtime, protocol, auth, key):
    import subprocess
    from looper.agents.registry_migration import plan, snapshot

    def forbidden(*args, **kwargs):
        pytest.fail("static preflight must not launch any CLI")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    account = provider(auth=auth, oauth_runtime_type=runtime if auth == "oauth" else None,
                       protocols=[protocol], base_url="", api_key=key if auth == "api_key" else None)
    result = plan(snapshot(RUNTIMES, [account], [profile(runtime)]))
    assert result.applicable
    assert result.report["legacy_default_endpoints"] == int(auth == "api_key")


def test_static_binding_comparison_checks_omp_generated_api_not_only_flags():
    from looper.agents.registry_migration import plan, snapshot

    # An intentionally incorrect conversion priority changes models.yml even
    # though OMP's --model/--thinking flags and key environment stay equal.
    rules = replace(LEGACY_RULES, protocol_priority={**LEGACY_RULES.protocol_priority,
                    "omp": (P.OPENAI_CHAT, P.OPENAI_RESPONSES, P.ANTHROPIC_MESSAGES)})
    result = plan(snapshot(RUNTIMES, [provider()], [profile()], rules=rules))
    assert not result.applicable
    assert result.report["binding_mismatches"] == 1


@pytest.mark.parametrize("runtime,protocol", [("claude-code", P.ANTHROPIC_MESSAGES),
                                             ("omp", P.OPENAI_CHAT), ("codex", P.OPENAI_RESPONSES)])
def test_preflight_projects_the_selected_model_without_losing_legacy_catalog(runtime, protocol):
    from looper.agents.registry_migration import plan, snapshot

    account = provider(protocols=[protocol], vendor_type="custom", api_key="synthetic-key", models=[
        {"name": "unused", "input_usd_per_mtok": 9},
        {"name": "selected", "context_window": 4096, "efforts": ["high"], "input_usd_per_mtok": 2},
    ])
    old = snapshot(RUNTIMES, [account], [profile(runtime, model="selected")])
    assert old.bindings
    assert isinstance(old.pricing["agent-test"], LegacyProviderSpec)
    assert old.pricing["agent-test"].models == ("unused", "selected")
    result = plan(old)
    assert result.applicable
    assert result.report["binding_mismatches"] == result.report["pricing_mismatches"] == 0


def test_missing_profile_identity_is_unresolved_and_invalid_empty_input_cannot_apply():
    from looper.agents.registry_migration import plan, snapshot

    row = profile()
    del row["id"]
    result = plan(snapshot(RUNTIMES, [provider()], [row]))
    assert not result.applicable
    assert result.report["unresolved_profiles"] == 1
    assert not plan(snapshot(RUNTIMES, [provider(protocols=["invalid"])], [])).applicable


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
    model = result.models[result.profiles["agent-test"].model_id]
    bound = LegacyProviderSpec("prov-test", "account", AuthKind.API_KEY, frozenset({model.protocol}),
                               base_url="https://example.invalid/v1")
    config = omp.models_yml(launch_for(bound, model.name, "omp"))
    assert config["providers"]["looper"]["api"] == dict(omp.API_NAMES)[expected]


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


# Account merging uses only synthetic credentials and never touches persistence.
def merge_account(identity="prov-a", **changes):
    return provider(**{"id": identity, "name": identity, "base_url": "https://api.deepseek.com",
                       "protocols": [P.OPENAI_CHAT, P.OPENAI_RESPONSES], "api_key": "synthetic-key",
                       "created_at": "2026-01-01T00:00:00Z", **changes})


def merge_pair(left=None, right=None, profiles=()):
    return convert(RUNTIMES, [merge_account() if left is None else left,
                             merge_account("prov-b") if right is None else right], profiles)


@pytest.mark.parametrize("left_key,right_key,reason", [
    ("synthetic-key", "synthetic-key", None),
    ("synthetic-key", "other-synthetic-key", "different_key"),
    (None, None, "missing_key"), ("", "", "missing_key"), ("  ", "  ", "missing_key"),
    (None, "synthetic-key", "missing_key"),
    ({"secret": "omitted"}, {"secret": "omitted"}, "missing_key"),
    ("***", "***", "missing_key"),
    ({"secret": "omitted"}, "synthetic-key", "missing_key"),
    ("***", "synthetic-key", "missing_key"),
    ("synthetic-key", " synthetic-key ", "different_key"),
])
def test_only_saved_nonempty_equal_keys_prove_account_identity(left_key, right_key, reason):
    from looper.agents.registry_migration import RetentionReason

    result = merge_pair(merge_account(api_key=left_key), merge_account("prov-b", api_key=right_key))
    if reason is None:
        assert result.provider_map == {"prov-a": "prov-a", "prov-b": "prov-a"}
        assert set(result.providers) == {"prov-a"}
        assert not result.retained
    else:
        assert result.provider_map == {"prov-a": "prov-a", "prov-b": "prov-b"}
        assert all(RetentionReason(reason) in result.retained[p] for p in result.providers)
        assert result.report[f"retained_{reason}"] == 2


@pytest.mark.parametrize("changes,reason", [
    ({"base_url": "https://unrecognized.invalid"}, "unknown_vendor"),
    ({"vendor_type": "custom"}, "unknown_vendor"),
    ({"vendor_type": "glm-zai"}, "vendor_conflict"),
    ({"base_url": "https://api.deepseek.com/", "vendor_type": "deepseek"}, "endpoint_conflict"),
    ({"base_url": "https://api.deepseek.com/v1", "vendor_type": "deepseek"}, "endpoint_conflict"),
    ({"base_url": "https://api.deepseek.com?route=other", "vendor_type": "deepseek"}, "endpoint_conflict"),
    ({"env": [{"key": "EXTRA_SETTING", "value": "different"}]}, "env_conflict"),
    ({"builtin": True}, "protected_account"),
])
def test_unproven_equivalence_retains_original_ids_and_typed_reasons(changes, reason):
    from looper.agents.registry_migration import RetentionReason

    result = merge_pair(right=merge_account("prov-b", **changes))
    assert set(result.providers) == {"prov-a", "prov-b"}
    assert RetentionReason(reason) in result.retained["prov-b"]
    assert all(isinstance(r, RetentionReason) for reasons in result.retained.values() for r in reasons)


@pytest.mark.parametrize("left_limit,right_limit", [(1, None), (None, 1), (2, 2), (2, 3)])
def test_any_finite_limit_keeps_independent_scheduling_pools(left_limit, right_limit):
    result = merge_pair(merge_account(max_concurrency=left_limit),
                        merge_account("prov-b", max_concurrency=right_limit))
    assert len(result.providers) == 2
    assert "finite_concurrency" in result.retained["prov-b"]
    assert [p.max_concurrency for p in result.providers.values()] == [left_limit, right_limit]


@pytest.mark.parametrize("right_url,merged", [("", True), ("https://api.openai.com/v1", False)])
def test_endpoint_default_flag_is_part_of_equivalence(right_url, merged):
    result = merge_pair(merge_account(base_url="", vendor_type="deepseek"),
                        merge_account("prov-b", base_url=right_url, vendor_type="deepseek"))
    assert len(result.providers) == (1 if merged else 2)
    if not merged:
        assert "endpoint_conflict" in result.retained["prov-b"]


def test_different_protocol_endpoints_coexist_and_profiles_keep_old_protocol_choice():
    left = merge_account(protocols=[P.ANTHROPIC_MESSAGES], base_url="https://api.deepseek.com/anthropic")
    right = merge_account("prov-b")
    rows = [profile("claude-code", provider_id="prov-a"),
            profile(id="agent-omp", provider_id="prov-b")]
    original = deepcopy((left, right, rows))
    result = merge_pair(left, right, rows)
    assert (left, right, rows) == original
    assert result.provider_map == {"prov-a": "prov-a", "prov-b": "prov-a"}
    assert len(result.endpoints) == 3
    for row, protocol in zip(rows, (P.ANTHROPIC_MESSAGES, P.OPENAI_RESPONSES)):
        target_provider, target_model = result.model_map[(row["provider_id"], row["model"], protocol)]
        assert target_provider == "prov-a"
        assert result.profiles[row["id"]].model_id == target_model
        assert result.models[target_model].protocol is protocol
        assert result.models[target_model].provider_id == target_provider
    assert result.report["providers_merged"] == 1


def test_env_order_is_irrelevant_but_values_must_be_equal():
    env = [{"key": "FIRST", "value": "1"}, {"key": "SECOND", "value": "2"}]
    assert len(merge_pair(merge_account(env=env), merge_account("prov-b", env=env[::-1])).providers) == 1


@pytest.mark.parametrize("setting,value", [
    ("context_window", 4096), ("background_model", "helper"), ("efforts", []),
    ("efforts", ["high", "low"]), ("input_usd_per_mtok", 1), ("output_usd_per_mtok", 2),
    ("cache_read_usd_per_mtok", 0), ("cache_write_usd_per_mtok", 3),
])
def test_every_overlapping_model_setting_must_match(setting, value):
    result = merge_pair(merge_account(models=[{"name": "model"}]),
                        merge_account("prov-b", models=[{"name": "model", setting: value}]))
    assert len(result.providers) == 2
    assert "model_pricing_conflict" in result.retained["prov-b"]


@pytest.mark.parametrize("same_prices,merged", [(False, False), (True, True)])
def test_normalized_pricing_retains_legacy_first_candidate_order(same_prices, merged):
    models = [{"name": "Model[1m]", "input_usd_per_mtok": 1},
              {"name": "model-20261001", "input_usd_per_mtok": 1 if same_prices else 9}]
    result = merge_pair(merge_account(models=models), merge_account("prov-b", models=models[::-1]))
    assert len(result.providers) == (1 if merged else 2)
    if not merged:
        assert "model_pricing_conflict" in result.retained["prov-b"]


def test_union_cannot_add_an_exact_or_normalized_price_override_to_another_account():
    result = merge_pair(merge_account(models=[{"name": "first"}]),
                        merge_account("prov-b", models=[{"name": "model", "input_usd_per_mtok": 9}]))
    assert len(result.providers) == 2
    assert "model_pricing_conflict" in result.retained["prov-b"]


def test_union_preserves_distinct_models_and_maps_duplicate_natural_keys():
    result = merge_pair(merge_account(models=[{"name": "shared"}, {"name": "left"}]),
                        merge_account("prov-b", models=[{"name": "shared"}, {"name": "right"}]))
    assert len(result.providers) == 1
    for protocol in (P.OPENAI_CHAT, P.OPENAI_RESPONSES):
        assert result.model_map[("prov-a", "shared", protocol)] == result.model_map[("prov-b", "shared", protocol)]
        models = sorted((m for m in result.models.values() if m.protocol is protocol),
                        key=lambda m: m.catalog_order)
        assert [m.name for m in models] == ["shared", "left", "right"]
        assert len({m.catalog_order for m in models}) == 3
    assert len(result.model_map) == 8


@pytest.mark.parametrize("left_date,right_date,canonical", [
    ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", "prov-b"),
    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "prov-a"),
    ("2026-01-01T01:00:00+01:00", "2026-01-01T00:00:00Z", "prov-a"),
    (None, None, "prov-a"),
])
def test_canonical_is_oldest_then_id_independent_of_input_order(left_date, right_date, canonical):
    left = merge_account(created_at=left_date)
    right = merge_account("prov-b", created_at=right_date)
    forward = merge_pair(left, right)
    backward = merge_pair(right, left)
    assert forward == backward
    assert set(forward.providers) == {canonical}
    assert forward.providers[canonical].name == canonical


@pytest.mark.parametrize("runtime,protocol", [("claude-code", P.ANTHROPIC_MESSAGES),
                                               ("codex", P.OPENAI_RESPONSES), ("grok", P.OPENAI_RESPONSES)])
def test_login_accounts_keep_fixed_identity(runtime, protocol):
    left = provider(id="login-a", auth="oauth", oauth_runtime_type=runtime, base_url="", protocols=[protocol],
                    builtin=True)
    right = {**left, "id": "login-b", "builtin": False}
    result = convert(RUNTIMES, [left, right], [])
    assert result.provider_map == {"login-a": "login-a", "login-b": "login-b"}
    assert all("protected_account" in result.retained[p] for p in result.providers)


def test_codex_generated_provider_name_change_prevents_merge():
    result = merge_pair(profiles=[profile("codex", provider_id="prov-b")])
    assert len(result.providers) == 2
    assert "launch_mismatch" in result.retained["prov-b"]


def test_migration_diagnostics_contain_no_secrets_fingerprints_or_equivalence_groups(capsys, caplog):
    import hashlib

    secret = "synthetic-secret-never-report"
    result = merge_pair(merge_account(api_key=secret), merge_account("prov-b", api_key=secret))
    captured = capsys.readouterr()
    exposed = repr(result) + repr(result.report) + captured.out + captured.err + caplog.text
    assert secret not in exposed
    assert hashlib.sha256(secret.encode()).hexdigest() not in exposed
    assert not any("group" in key or "hash" in key or "key" in key for key in vars(result))
    assert all(isinstance(value, int) for value in result.report.values())


@pytest.mark.parametrize("accounts", [
    [merge_account("prov-c"), merge_account("prov-b"), merge_account()],
    [merge_account(), merge_account("prov-b"), merge_account("prov-c")],
])
def test_three_equivalent_accounts_have_total_maps_without_dangling_references(accounts):
    rows = [profile(id=f"agent-{row['id']}", provider_id=row["id"]) for row in accounts]
    result = convert(RUNTIMES, accounts, rows)
    assert result.provider_map == {row["id"]: "prov-a" for row in accounts}
    assert len(result.models) == 1
    assert len(result.model_map) == 3
    assert len({row.model_id for row in result.profiles.values()}) == 1
    assert next(iter(result.models.values())).provider_id == "prov-a"
    assert all(pid == "prov-a" for pid, _ in result.endpoints)
    assert result.report["providers_merged"] == 2


def test_union_checks_original_members_and_cannot_bridge_conflicting_endpoints():
    accounts = [merge_account(protocols=[P.ANTHROPIC_MESSAGES], base_url="https://api.deepseek.com/anthropic"),
                merge_account("prov-b", protocols=[P.OPENAI_CHAT]),
                merge_account("prov-c", protocols=[P.OPENAI_CHAT], vendor_type="deepseek",
                              base_url="https://proxy.invalid")]
    result = convert(RUNTIMES, accounts, [])
    assert result.provider_map == {"prov-a": "prov-a", "prov-b": "prov-a", "prov-c": "prov-c"}
    assert result.endpoints[("prov-a", P.OPENAI_CHAT)].base_url == "https://api.deepseek.com"
    assert "endpoint_conflict" in result.retained["prov-c"]


def test_disjoint_protocols_keep_their_own_model_settings_prices_and_catalog_order():
    left = merge_account(protocols=[P.ANTHROPIC_MESSAGES], base_url="https://api.deepseek.com/anthropic",
                         models=[{"name": "Model[1m]", "input_usd_per_mtok": 1}, {"name": "helper"}])
    right = merge_account("prov-b", protocols=[P.OPENAI_CHAT],
                          models=[{"name": "Model[1m]", "input_usd_per_mtok": 9, "efforts": []}])
    result = merge_pair(left, right)
    assert len(result.providers) == 1
    for old, protocol in ((left, P.ANTHROPIC_MESSAGES), (right, P.OPENAI_CHAT)):
        for order, entry in enumerate(old["models"]):
            _, identity = result.model_map[(old["id"], entry["name"], protocol)]
            model = result.models[identity]
            assert model.catalog_order == order
            assert model.input_usd_per_mtok == entry.get("input_usd_per_mtok")


@pytest.mark.parametrize("dates", [(None, "2026-01-01T00:00:00Z"), ("not-a-time", "not-a-time")])
def test_unprovable_creation_order_keeps_accounts(dates):
    result = merge_pair(merge_account(created_at=dates[0]), merge_account("prov-b", created_at=dates[1]))
    assert len(result.providers) == 2
    assert "creation_order_unknown" in result.retained["prov-b"]


def test_only_remaining_independent_accounts_receive_retention_reasons():
    accounts = [merge_account(), merge_account("prov-b"), merge_account("prov-c", api_key="other-key")]
    result = convert(RUNTIMES, accounts, [])
    assert result.provider_map == {"prov-a": "prov-a", "prov-b": "prov-a", "prov-c": "prov-c"}
    assert set(result.retained) == {"prov-a", "prov-c"}
    assert result.report["retained_different_key"] == 2


def test_all_unmerged_model_natural_keys_map_to_their_original_ids():
    result = merge_pair(merge_account(models=[{"name": "model"}]),
                        merge_account("prov-b", api_key="other-key", models=[{"name": "model"}]))
    for model in result.models.values():
        assert result.model_map[(model.provider_id, model.name, model.protocol)] == (model.provider_id, model.id)


@pytest.mark.parametrize("runtime,protocol", [("claude-code", P.ANTHROPIC_MESSAGES),
                                             ("omp", P.OPENAI_CHAT), ("codex", P.OPENAI_RESPONSES)])
def test_merged_profiles_keep_real_legacy_binding_and_generated_config(runtime, protocol):
    from looper.agents.runtimes import ADAPTERS

    catalog = [{"name": "model", "context_window": 4096, "efforts": [], "background_model": "helper"}]
    url = "https://api.deepseek.com/anthropic" if protocol is P.ANTHROPIC_MESSAGES else "https://api.deepseek.com"
    left = merge_account(protocols=[protocol], models=catalog, base_url=url)
    right = merge_account("prov-b", protocols=[protocol], models=catalog, base_url=url)
    # A canonical Codex profile retains the name embedded in its config.toml;
    # Claude/OMP profiles can move from a differently named account.
    selected = left if runtime == "codex" else right
    row = profile(runtime, provider_id=selected["id"], model="model")
    result = merge_pair(left, right, [row])
    assert len(result.providers) == 1
    before = LegacyProviderSpec(selected["id"], selected["name"], AuthKind.API_KEY, frozenset({protocol}),
                                base_url=selected["base_url"], api_key=selected["api_key"],
                                catalog=tuple(CatalogModel.from_entry(m) for m in catalog))
    model = result.models[result.profiles[row["id"]].model_id]
    account = result.providers[model.provider_id]
    after = LegacyProviderSpec(account.id, account.name, account.auth, frozenset({model.protocol}),
                               base_url=result.endpoints[(account.id, model.protocol)].base_url,
                               api_key=account.api_key, env=account.env,
                               catalog=(CatalogModel.from_entry({key: getattr(model, key) for key in catalog[0]}),))
    adapter = ADAPTERS[runtime]
    assert adapter.bind_provider(before, "model", "high") == adapter.bind_provider(after, "model", "high")
    old_launch = launch_for(before, "model", runtime)
    new_launch = launch_for(after, "model", runtime)
    if runtime == "omp":
        assert omp.models_yml(old_launch) == omp.models_yml(new_launch)
    elif runtime == "codex":
        assert codex.config_toml(old_launch) == codex.config_toml(new_launch)


@pytest.mark.parametrize("runtime,protocol,model,effort", [
    ("pi", P.OPENAI_RESPONSES, "Exact.Model[1m]", "xhigh"),
    ("pi", P.OPENAI_CHAT, "Exact.Model[1m]", "low"),
    ("dsh", P.ANTHROPIC_MESSAGES, "deepseek-flash[1m]", "max"),
])
def test_new_runtime_explicit_bindings_preserve_exact_model_effort_and_metadata(runtime, protocol, model, effort):
    from looper.agents.registry_migration import plan, snapshot

    catalog = [{"name": model, "context_window": 1_000_000, "efforts": [effort],
                "input_usd_per_mtok": 0, "output_usd_per_mtok": 2}]
    row = profile(runtime, model=model, effort=effort)
    old = snapshot(RUNTIMES, [provider(protocols=[protocol], models=catalog, api_key="test-key")], [row])
    assert old.bindings
    result = plan(old)
    assert result.applicable
    assert result.report["binding_mismatches"] == result.report["pricing_mismatches"] == 0
    target = result.conversion
    saved = target.profiles[row["id"]]
    selected = target.models[saved.model_id]
    assert (saved.effort, selected.name, selected.protocol, selected.context_window, selected.efforts) == (
        effort, model, protocol, 1_000_000, (effort,))
    assert target.endpoints[(selected.provider_id, protocol)].base_url == "https://example.invalid/v1"


def test_pi_legacy_priority_and_generated_config_are_independent_of_conversion_rules():
    from looper.agents.registry_migration import plan, snapshot

    assert LEGACY_RULES.protocol_priority["pi"] == tuple(p for p, _ in pi.API_NAMES)
    account = provider(protocols=[P.OPENAI_CHAT, P.OPENAI_RESPONSES], api_key="test-key")
    result = run(account, [profile("pi")])
    assert result.models[result.profiles["agent-test"].model_id].protocol is P.OPENAI_RESPONSES
    rules = replace(LEGACY_RULES, protocol_priority={**LEGACY_RULES.protocol_priority,
                    "pi": (P.OPENAI_CHAT, P.OPENAI_RESPONSES)})
    comparison = plan(snapshot(RUNTIMES, [account], [profile("pi")], rules=rules))
    assert not comparison.applicable
    assert comparison.report["binding_mismatches"] == 1


def test_dsh_omitted_url_preserves_its_own_default_and_cross_runtime_conflicts_block():
    from looper.agents.registry_migration import plan, snapshot

    account = provider(protocols=[P.ANTHROPIC_MESSAGES], base_url="", api_key="test-key")
    row = profile("dsh", model="deepseek-flash", effort="max")
    converted = run(account, [row])
    assert converted.default_urls[("prov-test", P.ANTHROPIC_MESSAGES)] == dsh.DEFAULT_BASE_URL
    assert converted.endpoints[("prov-test", P.ANTHROPIC_MESSAGES)].use_runtime_default
    assert plan(snapshot(RUNTIMES, [account], [row])).applicable
    with pytest.raises(MigrationBlocked, match="default_endpoint_conflict"):
        run(account, [row, profile("claude-code", id="agent-claude", model="deepseek-flash")])


def test_pi_omitted_endpoint_blocks_without_changing_existing_runtime_defaults():
    with pytest.raises(MigrationBlocked, match="unknown_default_endpoint"):
        run(provider(protocols=[P.OPENAI_RESPONSES], base_url=""), [profile("pi")])
    assert run(provider(protocols=[P.OPENAI_RESPONSES], base_url=""), [profile("codex")]).profiles


@pytest.mark.parametrize("runtime,model,effort", [("dsh", "deepseek-flash", "xhigh"),
                                                  ("dsh", "unsupported", "high"),
                                                  ("pi", "exact", "unsupported")])
def test_new_runtime_invalid_model_or_effort_blocks_conversion(runtime, model, effort):
    protocol = P.ANTHROPIC_MESSAGES if runtime == "dsh" else P.OPENAI_CHAT
    with pytest.raises(MigrationBlocked):
        run(provider(protocols=[protocol]), [profile(runtime, model=model, effort=effort)])


def test_dsh_unused_chat_endpoint_keeps_the_proven_legacy_default():
    from looper.agents.registry_migration import plan, snapshot

    account = provider(protocols=[P.ANTHROPIC_MESSAGES, P.OPENAI_CHAT], base_url="", api_key="test-key")
    row = profile("dsh", model="deepseek-flash")
    result = plan(snapshot(RUNTIMES, [account], [row]))
    assert result.applicable
    converted = result.conversion
    assert converted.default_urls[("prov-test", P.ANTHROPIC_MESSAGES)] == dsh.DEFAULT_BASE_URL
    assert converted.default_urls[("prov-test", P.OPENAI_CHAT)] == "https://api.openai.com/v1"
