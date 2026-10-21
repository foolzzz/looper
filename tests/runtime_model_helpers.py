"""Typed launch snapshots for runtime tests starting at the adapter binding boundary."""

from dataclasses import replace
from typing import Any

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import (
    MODEL_FIELDS, Endpoint, LegacyProviderSpec, ModelSpec, ProviderSpec, RegistryError,
    ResolvedAgentProfile, ResolvedModel, RuntimeSpec, builtin_resolved_profile,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType


def resolved_profile(id: str, name: str, runtime: RuntimeSpec, provider: LegacyProviderSpec,
                     model: str, effort: str | None = None, **options) -> ResolvedAgentProfile:
    """Resolve a test's explicit model, rather than passing a retired profile to Launch."""
    if provider.catalog and model not in provider.models:
        raise RegistryError("model reference is absent from this test account")
    protocol = next((value for value in (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT,
                                        ApiProtocol.ANTHROPIC_MESSAGES) if value in provider.protocols), None)
    if protocol is None:
        raise RegistryError("model needs an explicit protocol")
    vendor = (VendorType.CUSTOM if provider.auth is AuthKind.API_KEY else
              next((value for value in VendorType if value.oauth_runtime_type == provider.oauth_runtime_type),
                   VendorType.CUSTOM))
    account = ProviderSpec(provider.id, provider.name, vendor, provider.auth, provider.oauth_runtime_type,
                           env=provider.env, api_key=provider.api_key)
    entry = provider.catalog_model(model)
    settings = {key: getattr(entry, key) for key in MODEL_FIELDS[1:]} if entry else {}
    selected = ModelSpec("model-" + id, account.id, model, protocol, **settings)
    endpoint = Endpoint(account.id, protocol, provider.base_url,
                        use_runtime_default=provider.auth is AuthKind.API_KEY and not provider.base_url)
    return ResolvedAgentProfile(id, name, runtime, selected.id, effort or None,
                                resolved_model=ResolvedModel(selected, account, endpoint), **options)


def builtin_launch(**fields: Any) -> Launch:
    """A launch of the built-in profile (Claude Code's own login) with its resolved model binding."""
    return Launch(AgentSettings(profile=builtin_resolved_profile()), **fields)


def with_model_name(profile: ResolvedAgentProfile, name: str) -> ResolvedAgentProfile:
    """``profile`` running another exact model of the same account and protocol, as editing its model would."""
    resolved = profile.resolved_model
    model = replace(resolved.model, id=f"model-{name}", name=name)
    return replace(profile, model_id=model.id, resolved_model=ResolvedModel(model, resolved.provider,
                                                                             resolved.endpoint))
