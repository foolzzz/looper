"""Typed launch snapshots for runtime tests starting at the adapter binding boundary."""

from looper.agents.profile_registry import (
    MODEL_FIELDS, Endpoint, LegacyProviderSpec, ModelSpec, ProviderSpec, RegistryError,
    ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
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
