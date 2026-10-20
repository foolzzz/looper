"""Pure v2 registry conversion shared by schema upgrade and legacy import.

Callers normalize file names to legacy row ids before calling ``convert``.
No database, runtime discovery, credentials lookup or vendor defaults are used.
Account merging is a separate step: this conversion retains every account id.
Profile metadata not represented by AgentProfile (builtin, timestamps) stays
on the caller's original row; only its provider/model reference is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid5

from .profile_registry import (
    MODEL_FIELDS, AgentProfile, Endpoint, ModelSpec, ProviderSpec, RegistryError, ResolvedModel,
    RuntimeSpec, _models,
)
from .provider_presets import PRESETS
from .runtimes import ApiProtocol, AuthKind, VendorType


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
    }),
    MappingProxyType({
        ("claude-code", ApiProtocol.ANTHROPIC_MESSAGES): "https://api.anthropic.com",
        ("codex", ApiProtocol.OPENAI_RESPONSES): "https://api.openai.com/v1",
        ("omp", ApiProtocol.OPENAI_RESPONSES): "https://api.openai.com/v1",
        ("omp", ApiProtocol.OPENAI_CHAT): "https://api.openai.com/v1",
        ("omp", ApiProtocol.ANTHROPIC_MESSAGES): "https://api.anthropic.com",
    }),
)


@dataclass
class Conversion:
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    endpoints: dict[tuple[str, ApiProtocol], Endpoint] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    profiles: dict[str, AgentProfile] = field(default_factory=dict)
    # Endpoint's v3 contract stores empty URL + flag. Retain the verified
    # actual addresses separately for callers' binding equivalence checks.
    default_urls: dict[tuple[str, ApiProtocol], str] = field(default_factory=dict)
    report: dict[str, int] = field(default_factory=dict)


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


def _default_url(protocol: ApiProtocol, rules: LegacyRules) -> str:
    # An unused declared endpoint still needs a provable default. Check all
    # old API-key adapters that could select this protocol, not just profiles.
    runtimes = [name for name, priority in rules.protocol_priority.items()
                if name != "grok" and protocol in priority]
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
    known_runtimes = {"claude-code", "codex", "omp", "grok"}
    for runtime in runtime_rows.values():
        if not isinstance(runtime.type, str) or runtime.type not in known_runtimes \
                or runtime.type not in rules.protocol_priority:
            raise MigrationBlocked("unknown_runtime")
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
                "api_key": row.get("api_key"), "builtin": row.get("builtin", False),
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
                    actual = _default_url(protocol, rules)
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
            identity = f"model-{uuid5(MODEL_NAMESPACE, json.dumps(key, ensure_ascii=True, separators=(',', ':')))}"
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
                          result.endpoints[(model.provider_id, model.protocol)]).check_runtime(profile.runtime)
            result.profiles[identity] = profile
    except MigrationBlocked:
        raise
    except (RegistryError, ValueError, TypeError, KeyError):
        # Legacy validators can quote an invalid name; callers get only a code.
        raise MigrationBlocked("invalid_legacy_value") from None
    result.report = {"providers_before": len(provider_rows), "providers_after": len(result.providers),
                     "endpoints_after": len(result.endpoints), "models_after": len(result.models),
                     "profiles_before": len(profile_rows), "profiles_after": len(result.profiles),
                     "profiles_mapped": len(result.profiles), "models_from_open_catalog": open_count,
                     "legacy_default_endpoints": len(result.default_urls), "unresolved_profiles": 0}
    return result
