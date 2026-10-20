"""Pure v2 registry conversion shared by schema upgrade and legacy import.

Callers normalize file names to legacy row ids before calling ``convert``.
No database, runtime discovery, credentials lookup or vendor defaults are used.
Accounts merge only when their saved inputs prove launch and pricing equivalence.
Profile metadata not represented by AgentProfile (builtin, timestamps) stays
on the caller's original row; only its provider/model reference is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

from .profile_registry import (
    MODEL_FIELDS, SECRET_MASK, AgentProfile, CatalogModel, Endpoint, LegacyProviderSpec,
    ModelSpec, ProviderSpec, RegistryError, ResolvedModel,
    RuntimeSpec, _env, _models,
)
from .provider_presets import PRESETS
from .runtimes import ADAPTERS, ApiProtocol, AuthKind, RuntimeUnsupported, VendorType
from .runtimes.dsh import DEFAULT_BASE_URL as DSH_DEFAULT_BASE_URL
from .runtimes.pi import API_NAMES as PI_API_NAMES
from .runtimes.pricing import base_model, model_price


# Persisted identity contract: never change this namespace or the key encoding.
MODEL_NAMESPACE = UUID("d2819ed8-b51d-5e72-aeb9-a1f31c1e12ac")


class MigrationBlocked(RegistryError):
    """An input cannot be converted without changing legacy behavior."""

    def __init__(self, code: str) -> None:
        self.code = code
        # Codes are safe for dry-run reports: no account names, URLs or secrets.
        super().__init__(code)


@dataclass(frozen=True)
class LegacyRules:
    """Pinned old resolver rules, supplied explicitly when a patch changes them.

    Priority is per runtime, never inferred from model names. Default URLs are
    actual request addresses when the old provider left its URL empty; absence
    means equivalence cannot be established. The new binding must still honor
    each adapter's omission behavior when use_runtime_default is set.
    """

    protocol_priority: Mapping[str, tuple[ApiProtocol, ...]]
    default_urls: Mapping[tuple[str, ApiProtocol], str]


LEGACY_RULES = LegacyRules(
    MappingProxyType({
        "claude-code": (ApiProtocol.ANTHROPIC_MESSAGES,),
        "codex": (ApiProtocol.OPENAI_RESPONSES,),
        "omp": (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES),
        "grok": (ApiProtocol.OPENAI_RESPONSES,),
        "pi": tuple(protocol for protocol, _ in PI_API_NAMES),
        "dsh": tuple(ADAPTERS["dsh"].protocols),
    }),
    MappingProxyType({
        ("claude-code", ApiProtocol.ANTHROPIC_MESSAGES): "https://api.anthropic.com",
        ("codex", ApiProtocol.OPENAI_RESPONSES): "https://api.openai.com/v1",
        ("omp", ApiProtocol.OPENAI_RESPONSES): "https://api.openai.com/v1",
        ("omp", ApiProtocol.OPENAI_CHAT): "https://api.openai.com/v1",
        ("omp", ApiProtocol.ANTHROPIC_MESSAGES): "https://api.anthropic.com",
        ("dsh", ApiProtocol.ANTHROPIC_MESSAGES): DSH_DEFAULT_BASE_URL,
    }),
)


class RetentionReason(str, Enum):
    UNKNOWN_VENDOR = "unknown_vendor"
    MISSING_KEY = "missing_key"
    DIFFERENT_KEY = "different_key"
    VENDOR_CONFLICT = "vendor_conflict"
    ENDPOINT_CONFLICT = "endpoint_conflict"
    ENV_CONFLICT = "env_conflict"
    MODEL_PRICING_CONFLICT = "model_pricing_conflict"
    FINITE_CONCURRENCY = "finite_concurrency"
    PROTECTED_ACCOUNT = "protected_account"
    LAUNCH_MISMATCH = "launch_mismatch"
    CREATION_ORDER_UNKNOWN = "creation_order_unknown"


@dataclass
class Conversion:
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    endpoints: dict[tuple[str, ApiProtocol], Endpoint] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    # Endpoint's v3 contract stores empty URL + flag. Retain the verified
    # actual addresses separately for callers' binding equivalence checks.
    default_urls: dict[tuple[str, ApiProtocol], str] = field(default_factory=dict)
    provider_map: dict[str, str] = field(default_factory=dict)
    # Exactly the five non-secret fields of the migration history snapshot.
    model_map: dict[tuple[str, str, ApiProtocol], tuple[str, str]] = field(default_factory=dict)
    retained: dict[str, tuple[RetentionReason, ...]] = field(default_factory=dict)
    report: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LegacySnapshot:
    """Private in-memory input, including the old effective binding and price inputs.

    Never serialize this object. Only ``ConversionPlan.report`` may leave the
    preflight boundary. Capturing does not discover runtimes or read logins.
    """

    runtimes: tuple[RuntimeSpec, ...] = field(repr=False)
    providers: tuple[Mapping[str, Any], ...] = field(repr=False)
    profiles: tuple[Mapping[str, Any], ...] = field(repr=False)
    session_provider_ids: tuple[str | None, ...] = field(repr=False)
    bindings: Mapping[str, tuple[Any, ...]] = field(repr=False)
    pricing: Mapping[str, LegacyProviderSpec] = field(repr=False)
    rules: LegacyRules = field(repr=False)


@dataclass(frozen=True)
class ConversionPlan:
    """A shared DB/importer preflight, not authorization to reuse it for apply.

    An applying caller must capture a fresh snapshot and recompute this plan
    inside its write transaction; this module does not apply any changes.
    """

    conversion: Conversion | None = field(repr=False)
    report: dict[str, Any]
    _blocked: bool = field(repr=False)

    @property
    def applicable(self) -> bool:
        r = self.report
        return (not self._blocked and self.conversion is not None
                and r["profiles_before"] == r["profiles_after"] == r["profiles_mapped"]
                and all(r[key] == 0 for key in ("unresolved_profiles", "binding_mismatches", "pricing_mismatches")))


def _binding_provider(provider: LegacyProviderSpec, name: str, protocol: ApiProtocol) -> LegacyProviderSpec:
    """Project a legacy choice to the exact model input required by v3 adapters."""
    entry = provider.catalog_model(name)
    if entry is None and provider.catalog:
        raise RegistryError("model outside legacy catalog")
    return replace(provider, protocols=frozenset({protocol}), catalog=(entry or CatalogModel(name),))


def _launch_inputs(runtime: RuntimeSpec, provider: LegacyProviderSpec, row: Mapping[str, Any],
                   protocol: ApiProtocol, actual_url: str | None, *,
                   resolved: ResolvedModel | None = None) -> tuple[Any, ...]:
    # Missing credentials are not permission to discover a key or login. A
    # local sentinel compares binding structure while preserving missingness.
    adapter = ADAPTERS[runtime.type]
    if resolved is None and runtime.type in {"omp", "pi"}:
        from .runtimes.omp import API_NAMES as OMP_API_NAMES

        priorities = OMP_API_NAMES if runtime.type == "omp" else PI_API_NAMES

        # These adapters selected their own priority, independently of conversion
        # rules. Keep that baseline so a changed conversion is detectable.
        protocol = next(p for p, _ in priorities if p in provider.protocols)
    bound = resolved.binding_provider() if resolved is not None else _binding_provider(provider, row["model"], protocol)
    launch_provider = replace(bound, api_key=bound.api_key or "migration-static-missing")
    binding = adapter.bind_provider(launch_provider, row["model"], row.get("effort") or "")
    config = None
    if runtime.type in {"omp", "pi"}:
        from .runtimes.omp import models_yml
        from .runtimes.pi import models_json

        # Pure config generation also checks the old multi-protocol selection;
        # binding flags alone do not contain the API or context window.
        if resolved is None:
            entry, = bound.catalog
            model = ModelSpec("migration-static", provider.id, entry.name, protocol,
                              **{k: getattr(entry, k) for k in MODEL_FIELDS[1:]})
            account = ProviderSpec(provider.id, provider.name, VendorType.CUSTOM, AuthKind.API_KEY,
                                   env=provider.env, api_key=provider.api_key)
            resolved = ResolvedModel(model, account, Endpoint(provider.id, protocol, provider.base_url,
                                                            use_runtime_default=not provider.base_url))
        launch = SimpleNamespace(settings=SimpleNamespace(resolved_model=resolved), model=row["model"],
                                 profile=SimpleNamespace(provider=launch_provider))
        config = models_yml(launch) if runtime.type == "omp" else models_json(launch)
    elif runtime.type == "dsh":
        from .runtimes.dsh import validate_provider

        # DSH provider flags contain only the key; the overlay chooses the wire
        # model, endpoint and effort. Compare those without materializing a CLI.
        config = validate_provider(bound, row["model"], row.get("effort"))
    entry = provider.catalog_model(row["model"])
    return (runtime, protocol, provider.auth, provider.oauth_runtime_type, actual_url,
            provider.base_url, provider.api_key, dict(provider.env),
            provider.name if runtime.type == "codex" and provider.auth is AuthKind.API_KEY else None,
            row["model"], row.get("effort"), row.get("agent_teams", False), row.get("custom_prompt", ""),
            tuple(getattr(entry, k) if entry else None for k in ("context_window", "efforts", "background_model")),
            binding.args, dict(binding.env), frozenset(binding.unset), config)


def snapshot(runtimes: Iterable[RuntimeSpec], providers: Iterable[Mapping[str, Any]],
             profiles: Iterable[Mapping[str, Any]], *, session_provider_ids: Iterable[str | None] = (),
             rules: LegacyRules = LEGACY_RULES) -> LegacySnapshot:
    """Capture old effective inputs independently of the target converter."""
    runtime_rows = tuple(runtimes)
    accounts, rows = deepcopy(tuple(providers)), deepcopy(tuple(profiles))
    runtime_by_id = {r.id: r for r in runtime_rows}
    account_by_id = {r.get("id"): r for r in accounts}
    bindings, pricing = {}, {}
    for row in rows:
        try:
            runtime = runtime_by_id[row["runtime_id"]]
            saved = account_by_id[row["provider_id"]]
            account = LegacyProviderSpec(
                saved["id"], saved["name"], AuthKind(saved["auth"]), frozenset(_protocols(saved)),
                saved.get("oauth_runtime_type"), saved.get("base_url", ""),
                tuple(CatalogModel.from_entry(entry) for entry in _models(saved.get("models", []))),
                _env(saved.get("env")), _saved_key(saved.get("api_key")))
            protocol = next(p for p in rules.protocol_priority[runtime.type] if p in account.protocols)
            actual_url = account.base_url or (rules.default_urls.get((runtime.type, protocol))
                                              if account.auth is AuthKind.API_KEY else None)
            bindings[row["id"]] = _launch_inputs(runtime, account, row, protocol, actual_url)
            pricing[row["id"]] = account
        except (RegistryError, RuntimeUnsupported, KeyError, ValueError, TypeError, StopIteration):
            # The plan counts invalid profiles; never expose validator messages.
            continue
    return LegacySnapshot(runtime_rows, accounts, rows, tuple(session_provider_ids), bindings, pricing, rules)


def plan(old: LegacySnapshot) -> ConversionPlan:
    """Compute the same static plan for a DB snapshot or normalized legacy import.

    The public report is an explicit RFC 9.1 allowlist. Partial conversions are
    useful only for counts: any invalid input blocks the entire application.
    """
    blocked = False
    try:
        converted = convert(old.runtimes, old.providers, old.profiles, rules=old.rules)
    except MigrationBlocked:
        blocked = True
        valid = []
        for row in old.profiles:
            try:
                convert(old.runtimes, old.providers, [row], rules=old.rules)
            except MigrationBlocked:
                continue
            valid.append(row)
        try:
            converted = convert(old.runtimes, old.providers, valid, rules=old.rules)
        except MigrationBlocked:
            converted = None
    r = converted.report if converted else {}
    mapped = len(converted.profiles) if converted else 0
    report = {
        "providers_before": len(old.providers), "providers_after": r.get("providers_after", 0),
        "endpoints_after": r.get("endpoints_after", 0), "models_after": r.get("models_after", 0),
        "profiles_before": len(old.profiles), "profiles_after": mapped, "profiles_mapped": mapped,
        "models_from_open_catalog": r.get("models_from_open_catalog", 0),
        "providers_merged": r.get("providers_merged", 0),
        "providers_kept_separate_by_reason": {reason.value: r.get(f"retained_{reason.value}", 0)
                                               for reason in RetentionReason},
        "legacy_default_endpoints": r.get("legacy_default_endpoints", 0),
        "unresolved_profiles": len(old.profiles) - mapped, "binding_mismatches": 0,
        "pricing_mismatches": 0, "session_references_remapped": 0,
    }
    if converted is not None:
        rows = {row.get("id"): row for row in old.profiles}
        for identity, profile in converted.profiles.items():
            model = converted.models[profile.model_id]
            account = converted.providers[model.provider_id]
            endpoint = converted.endpoints[(account.id, model.protocol)]
            target = LegacyProviderSpec(account.id, account.name, account.auth, frozenset({model.protocol}),
                                        account.oauth_runtime_type, endpoint.base_url,
                                        _catalog_models(m for m in converted.models.values()
                                                        if m.provider_id == account.id and m.protocol is model.protocol),
                                        account.env, account.api_key)
            row = {**rows[identity], "model": model.name, "effort": profile.effort,
                   "agent_teams": profile.agent_teams, "custom_prompt": profile.custom_prompt}
            actual_url = endpoint.base_url or converted.default_urls.get((account.id, model.protocol))
            try:
                resolved = ResolvedModel(model, account, endpoint)
                binding = _launch_inputs(profile.runtime, target, row, model.protocol, actual_url, resolved=resolved)
                same_binding = identity in old.bindings and old.bindings[identity] == binding
            except (RuntimeUnsupported, RegistryError, KeyError, ValueError, TypeError):
                same_binding = False
            report["binding_mismatches"] += int(not same_binding)
            previous = old.pricing.get(identity)
            same_price = (previous is not None and _same_prices(previous.catalog, target.catalog)
                          and model_price(previous, rows[identity]["model"]) == model_price(target, model.name)
                          and ADAPTERS[profile.runtime.type].cost_source(previous)
                          == ADAPTERS[profile.runtime.type].cost_source(target))
            report["pricing_mismatches"] += int(not same_price)
        report["session_references_remapped"] = sum(
            identity in converted.provider_map and converted.provider_map[identity] != identity
            for identity in old.session_provider_ids)
    return ConversionPlan(converted, report, blocked)


def _index(rows: Iterable[Any], *, runtimes: bool = False) -> dict[str, Any]:
    result = {}
    for row in rows:
        if not isinstance(row, RuntimeSpec if runtimes else Mapping):
            raise MigrationBlocked("invalid_legacy_value")
        identity = row.id if runtimes else row.get("id")
        if not isinstance(identity, str) or not identity:
            raise MigrationBlocked("invalid_id")
        if identity in result:
            raise MigrationBlocked("duplicate_id")
        result[identity] = row
    return result


def _protocols(row: Mapping[str, Any]) -> tuple[ApiProtocol, ...]:
    raw = row.get("protocols")
    if not isinstance(raw, (list, tuple, set, frozenset)) or not raw:
        raise MigrationBlocked("invalid_protocols")
    try:
        protocols = tuple(ApiProtocol(value) for value in raw)
    except (TypeError, ValueError):
        raise MigrationBlocked("unknown_protocol") from None
    if len(set(protocols)) != len(protocols):
        raise MigrationBlocked("invalid_protocols")
    return tuple(p for p in ApiProtocol if p in protocols)


def _vendor(row: Mapping[str, Any], protocols: tuple[ApiProtocol, ...]) -> VendorType:
    if row.get("vendor_type") is not None:
        return VendorType(row["vendor_type"])
    if row["auth"] == AuthKind.OAUTH:
        matches = [v for v in VendorType if v.oauth_runtime_type == row.get("oauth_runtime_type")
                   and v.auth_kind is AuthKind.OAUTH]
        if len(matches) != 1:
            raise MigrationBlocked("unknown_login")
        return matches[0]
    matches = [VendorType(preset.id) for preset in PRESETS if all(
        any(row.get("base_url", "") == api.base_url and protocol in api.protocols for api in preset.apis)
        for protocol in protocols)]
    return matches[0] if len(matches) == 1 else VendorType.CUSTOM


def _default_url(protocol: ApiProtocol, rules: LegacyRules, *,
                 runtime_types: set[str] | None = None) -> str:
    # An unused declared endpoint still needs a provable default. Check all
    # old API-key adapters that could select this protocol, not just profiles.
    # Preserve the established four-adapter check for old accounts. A newer
    # adapter has a different (DSH) or absent (Pi) default: only accounts it
    # actually uses need that additional boundary.
    scope = runtime_types if runtime_types is not None else {"claude-code", "codex", "omp"}
    runtimes = [name for name, priority in rules.protocol_priority.items()
                if name in scope and protocol in priority]
    urls = {rules.default_urls.get((name, protocol)) for name in runtimes}
    if not urls or None in urls or "" in urls:
        raise MigrationBlocked("unknown_default_endpoint")
    if len(urls) != 1:
        raise MigrationBlocked("default_endpoint_conflict")
    return next(iter(urls))


def convert(runtimes: Iterable[RuntimeSpec], providers: Iterable[Mapping[str, Any]],
            profiles: Iterable[Mapping[str, Any]], *, rules: LegacyRules = LEGACY_RULES) -> Conversion:
    """Convert normalized legacy rows, or raise MigrationBlocked atomically.

    The original ids identify runtime/account/profile rows. Model ids are
    UUIDv5 of the complete provider/exact-name/protocol natural key. Finite
    catalogs retain all entries on every declared protocol; open catalogs
    materialize only profiles' actual references. Inputs are never modified.
    """
    runtime_rows = _index(runtimes, runtimes=True)
    provider_rows, profile_rows = _index(providers), _index(profiles)
    for runtime in runtime_rows.values():
        if not isinstance(runtime.type, str) or runtime.type not in ADAPTERS \
                or runtime.type not in rules.protocol_priority:
            raise MigrationBlocked("unknown_runtime")
    account_runtimes: dict[str, set[str]] = {}
    for row in profile_rows.values():
        runtime = runtime_rows.get(row.get("runtime_id"))
        account_id = row.get("provider_id")
        if runtime is not None and isinstance(account_id, str):
            account_runtimes.setdefault(account_id, set()).add(runtime.type)
    result = Conversion()
    catalogs: dict[str, list[dict[str, Any]]] = {}
    protocols_by_provider = {}
    try:
        for identity, row in provider_rows.items():
            protocols = protocols_by_provider[identity] = _protocols(row)
            account = ProviderSpec.from_entry({
                "id": identity, "name": row["name"], "vendor_type": _vendor(row, protocols),
                "auth": row["auth"], "oauth_runtime_type": row.get("oauth_runtime_type"),
                "env": row.get("env", []), "max_concurrency": row.get("max_concurrency"),
                "api_key": _saved_key(row.get("api_key")), "builtin": row.get("builtin", False),
            })
            result.providers[identity] = account
            catalogs[identity] = _models(row.get("models", []))
            for protocol in protocols:
                key = (identity, protocol)
                url = row.get("base_url", "")
                runtime_default = not url and account.auth is AuthKind.API_KEY
                endpoint = Endpoint(identity, protocol, url, runtime_default)
                if endpoint.base_url != url:
                    raise MigrationBlocked("invalid_legacy_value")
                endpoint.check_provider(account)
                result.endpoints[key] = endpoint
                if runtime_default:
                    selected = account_runtimes.get(identity, set())
                    scope = selected if any(name in {"pi", "dsh"} and protocol in rules.protocol_priority[name]
                                            for name in selected) else None
                    actual = _default_url(protocol, rules, runtime_types=scope)
                    # Validate supplied rules' addresses with the same boundary.
                    Endpoint(identity, protocol, actual)
                    result.default_urls[key] = actual

        references = {}
        for identity, row in profile_rows.items():
            runtime = runtime_rows.get(row.get("runtime_id"))
            account = result.providers.get(row.get("provider_id"))
            if runtime is None:
                raise MigrationBlocked("bad_runtime_reference")
            if account is None:
                raise MigrationBlocked("bad_provider_reference")
            if account.auth is AuthKind.OAUTH and account.oauth_runtime_type != runtime.type:
                raise MigrationBlocked("incompatible_login")
            if runtime.type == "grok" and account.auth is not AuthKind.OAUTH:
                raise MigrationBlocked("incompatible_auth")
            protocol = next((p for p in rules.protocol_priority[runtime.type]
                             if p in protocols_by_provider[account.id]), None)
            if protocol is None:
                raise MigrationBlocked("incompatible_protocol")
            name = row["model"]
            catalog = catalogs[account.id]
            if catalog and not any(m["name"] == name for m in catalog):
                raise MigrationBlocked("model_outside_catalog")
            references[identity] = (account.id, name, protocol)

        natural_keys = {}

        def add(account_id: str, entry: Mapping[str, Any], protocol: ApiProtocol, order: int) -> None:
            key = (account_id, entry["name"], protocol)
            identity = _model_id(*key)
            model = ModelSpec(identity, account_id, entry["name"], protocol,
                              **{k: entry.get(k) for k in MODEL_FIELDS if k != "name"}, catalog_order=order,
                              builtin=result.providers[account_id].builtin)
            result.models[identity] = model
            natural_keys[key] = identity

        for account_id, catalog in catalogs.items():
            for protocol in protocols_by_provider[account_id]:
                for order, entry in enumerate(catalog):
                    add(account_id, entry, protocol, order)
        open_count = 0
        open_orders: dict[tuple[str, ApiProtocol], int] = {}
        for account_id, name, protocol in sorted(set(references.values())):
            if not catalogs[account_id]:
                key = (account_id, protocol)
                order = open_orders.get(key, 0)
                add(account_id, {"name": name}, protocol, order)
                open_orders[key] = order + 1
                open_count += 1
        for identity, row in profile_rows.items():
            model_id = natural_keys[references[identity]]
            profile = AgentProfile(identity, row["name"], runtime_rows[row["runtime_id"]], model_id,
                                   row.get("effort"), row.get("agent_teams", False), row.get("custom_prompt", ""))
            model = result.models[model_id]
            ResolvedModel(model, result.providers[model.provider_id],
                          result.endpoints[(model.provider_id, model.protocol)]).check_runtime(profile.runtime, profile.effort)
            result.profiles[identity] = profile
    except MigrationBlocked:
        raise
    except (RegistryError, ValueError, TypeError, KeyError):
        # Legacy validators can quote an invalid name; callers get only a code.
        raise MigrationBlocked("invalid_legacy_value") from None
    _merge_accounts(result, provider_rows, catalogs)
    result.report.update({"providers_before": len(provider_rows), "providers_after": len(result.providers),
                          "endpoints_after": len(result.endpoints), "models_after": len(result.models),
                          "profiles_before": len(profile_rows), "profiles_after": len(result.profiles),
                          "profiles_mapped": len(result.profiles), "models_from_open_catalog": open_count,
                          "legacy_default_endpoints": len(result.default_urls), "unresolved_profiles": 0})
    return result


def _saved_key(value: Any) -> str | None:
    # An omitted export secret and a masked API response are not credentials.
    if value is None or value == {"secret": "omitted"} or value == SECRET_MASK \
            or isinstance(value, str) and not value.strip():
        return None
    return value


def _model_id(provider_id: str, name: str, protocol: ApiProtocol) -> str:
    key = (provider_id, name, protocol)
    return f"model-{uuid5(MODEL_NAMESPACE, json.dumps(key, ensure_ascii=True, separators=(',', ':')))}"


def _created_at(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("created_at")
    if value is None:
        return None  # File entries without creation metadata tie by id.
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _catalog_models(models: Iterable[ModelSpec]) -> tuple[CatalogModel, ...]:
    return tuple(CatalogModel.from_entry({name: getattr(model, name) for name in MODEL_FIELDS})
                 for model in sorted(models, key=lambda m: (m.catalog_order, m.name)))


def _same_prices(before: tuple[CatalogModel, ...], after: tuple[CatalogModel, ...]) -> bool:
    # model_price is piecewise: exact names, then the FIRST normalized match,
    # then builtin prices. One non-exact representative per base covers every
    # remaining date/context/case variant, including runtime-reported helpers.
    names = {model.name for model in (*before, *after)}
    probes = set(names)
    for base in {base_model(name) for name in names}:
        index = 0
        while (probe := f"{base}-{index:08d}") in names:
            index += 1
        probes.add(probe)
    old = LegacyProviderSpec("", "", AuthKind.API_KEY, frozenset(), catalog=before)
    new = replace(old, catalog=after)
    return all(model_price(old, name) == model_price(new, name) for name in probes)


def _merge_accounts(result: Conversion, rows: Mapping[str, Mapping[str, Any]],
                    catalogs: Mapping[str, list[dict[str, Any]]]) -> None:
    """Prove each proposed union against ALL original members before mutating.

    This is only an in-memory conversion. Persistence callers must apply the
    returned reference maps, secret ownership and session references atomically.
    No credential fingerprints or secret-based grouping leave this function.
    """
    original_providers = dict(result.providers)
    original_endpoints = dict(result.endpoints)
    original_models = dict(result.models)
    original_profiles = dict(result.profiles)
    members: dict[str, list[str]] = {}
    dates = {identity: _created_at(row) for identity, row in rows.items()}

    def intrinsic(identity: str) -> set[RetentionReason]:
        account = result.providers[identity]
        reasons = set()
        if rows[identity].get("created_at") is not None and dates[identity] is None:
            reasons.add(RetentionReason.CREATION_ORDER_UNKNOWN)
        if account.builtin or account.auth is AuthKind.OAUTH:
            reasons.add(RetentionReason.PROTECTED_ACCOUNT)
        if account.vendor_type is VendorType.CUSTOM:
            reasons.add(RetentionReason.UNKNOWN_VENDOR)
        if account.auth is AuthKind.API_KEY and not account.api_key:
            reasons.add(RetentionReason.MISSING_KEY)
        if account.max_concurrency is not None:
            reasons.add(RetentionReason.FINITE_CONCURRENCY)
        return reasons

    def proposal(left: str, right: str) -> tuple[dict[tuple[str, ApiProtocol], ModelSpec],
                                                set[RetentionReason]]:
        a, b = result.providers[left], result.providers[right]
        reasons = intrinsic(left) | intrinsic(right)
        if reasons:
            return {}, reasons
        if a.vendor_type is not b.vendor_type:
            return {}, {RetentionReason.VENDOR_CONFLICT}
        # Compare the saved secrets themselves only in memory, never hashes.
        if rows[left].get("api_key") != rows[right].get("api_key"):
            return {}, {RetentionReason.DIFFERENT_KEY}
        if (dates[left] is None) != (dates[right] is None):
            return {}, {RetentionReason.CREATION_ORDER_UNKNOWN}
        if dict(a.env) != dict(b.env):
            return {}, {RetentionReason.ENV_CONFLICT}
        for protocol in ApiProtocol:
            first = result.endpoints.get((left, protocol))
            second = result.endpoints.get((right, protocol))
            if first and second and (first.base_url, first.use_runtime_default,
                                     result.default_urls.get((left, protocol))) != (
                    second.base_url, second.use_runtime_default, result.default_urls.get((right, protocol))):
                return {}, {RetentionReason.ENDPOINT_CONFLICT}
        combined = {(m.name, m.protocol): m for m in result.models.values() if m.provider_id == left}
        for model in sorted((m for m in result.models.values() if m.provider_id == right),
                            key=lambda m: (m.protocol, m.catalog_order, m.name)):
            key = (model.name, model.protocol)
            if existing := combined.get(key):
                if any(getattr(existing, name) != getattr(model, name) for name in MODEL_FIELDS):
                    return {}, {RetentionReason.MODEL_PRICING_CONFLICT}
            else:
                order = max((m.catalog_order for m in combined.values() if m.protocol is model.protocol),
                            default=-1) + 1
                combined[key] = replace(model, id=_model_id(left, model.name, model.protocol),
                                        provider_id=left, catalog_order=order)
        affected = [*members[left], *members[right]]
        for identity in affected:
            before = tuple(CatalogModel.from_entry(entry) for entry in catalogs[identity])
            for account_id, protocol in original_endpoints:
                if account_id == identity and not _same_prices(
                        before, _catalog_models(m for m in combined.values() if m.protocol is protocol)):
                    return {}, {RetentionReason.MODEL_PRICING_CONFLICT}
        for profile in original_profiles.values():
            model = original_models[profile.model_id]
            if model.provider_id not in affected:
                continue
            account = original_providers[model.provider_id]
            # Codex includes the account's display name in config.toml. A
            # changed name is not an allowed normalization of generated config.
            if profile.runtime.type == "codex" and account.name != a.name:
                return {}, {RetentionReason.LAUNCH_MISMATCH}
            endpoint = original_endpoints[(account.id, model.protocol)]
            old = LegacyProviderSpec(account.id, account.name, account.auth,
                                     frozenset(p for pid, p in original_endpoints if pid == account.id),
                                     account.oauth_runtime_type, endpoint.base_url,
                                     tuple(CatalogModel.from_entry(entry) for entry in catalogs[account.id]),
                                     account.env, account.api_key)
            # The target resolver binds the model's exact protocol, never the
            # union's protocol priority. The rest of the launch inputs stay on
            # the unchanged profile/runtime; all model settings matched above.
            new = replace(old, id=left, name=a.name, protocols=frozenset({model.protocol}),
                          catalog=_catalog_models(m for m in combined.values() if m.protocol is model.protocol),
                          env=a.env, api_key=a.api_key)
            try:
                adapter = ADAPTERS[profile.runtime.type]
                first = adapter.bind_provider(_binding_provider(old, model.name, model.protocol),
                                              model.name, profile.effort or "")
                second = adapter.bind_provider(_binding_provider(new, model.name, model.protocol),
                                               model.name, profile.effort or "")
            except (RuntimeUnsupported, KeyError):
                return {}, {RetentionReason.LAUNCH_MISMATCH}
            if first.args != second.args or dict(first.env) != dict(second.env) or set(first.unset) != set(second.unset):
                return {}, {RetentionReason.LAUNCH_MISMATCH}
        return combined, set()

    order = sorted(rows, key=lambda identity: (dates[identity] is None,
                                               dates[identity] or datetime.max.replace(tzinfo=timezone.utc), identity))
    result.provider_map = {identity: identity for identity in rows}
    for identity in order:
        members[identity] = [identity]
        for canonical in list(members):
            if canonical == identity:
                continue
            combined, reasons = proposal(canonical, identity)
            if reasons:
                continue
            result.models = {key: model for key, model in result.models.items()
                             if model.provider_id not in {canonical, identity}}
            result.models.update((model.id, model) for model in combined.values())
            for key, endpoint in list(result.endpoints.items()):
                if key[0] == identity:
                    result.endpoints.setdefault((canonical, key[1]), replace(endpoint, provider_id=canonical))
                    del result.endpoints[key]
                    if key in result.default_urls:
                        result.default_urls.setdefault((canonical, key[1]), result.default_urls.pop(key))
            members[canonical].extend(members.pop(identity))
            for old_id in members[canonical]:
                result.provider_map[old_id] = canonical
            del result.providers[identity]
            break

    # Local retained-account reasons contain ids and typed codes only. Public
    # reports expose counts, never account names, endpoints or comparison data.
    reasons_by_id = {identity: intrinsic(identity) for identity in members}
    remaining = list(members)
    for index, left in enumerate(remaining):
        for right in remaining[index + 1:]:
            _, reasons = proposal(left, right)
            reasons_by_id[left].update(reasons)
            reasons_by_id[right].update(reasons)
    result.retained = {identity: tuple(sorted(reasons, key=lambda reason: reason.value))
                       for identity, reasons in reasons_by_id.items() if reasons}
    targets = {(m.provider_id, m.name, m.protocol): m.id for m in result.models.values()}
    for model in original_models.values():
        canonical = result.provider_map[model.provider_id]
        result.model_map[(model.provider_id, model.name, model.protocol)] = (
            canonical, targets[(canonical, model.name, model.protocol)])
    for identity, profile in original_profiles.items():
        model = original_models[profile.model_id]
        result.profiles[identity] = replace(profile, model_id=result.model_map[
            (model.provider_id, model.name, model.protocol)][1])
    result.report["providers_merged"] = len(rows) - len(result.providers)
    for reason in RetentionReason:
        result.report[f"retained_{reason.value}"] = sum(reason in reasons for reasons in result.retained.values())
