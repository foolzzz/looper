"""DeepSeek provider policy for DSH; the executable adapter is not registered yet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from . import ApiProtocol, AuthKind, ProviderBinding, RuntimeUnsupported

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec


PROVIDER_ID = "deepseek-official"
KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
MODEL_MAP = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-flash[1m]": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-pro[1m]": "deepseek-v4-pro",
}


@dataclass(frozen=True)
class DshProviderPolicy:
    """Validated overlay inputs; no credentials and no inference of a provider's vendor."""

    route: ClassVar[str] = PROVIDER_ID
    model: str
    effort: str
    base_url: str


def validate_provider(provider: ProviderSpec, model: str, effort: str | None = None) -> DshProviderPolicy:
    """Check configuration without reading a key; also used at the launch boundary."""
    from ..profile_registry import RegistryError, _base_url

    if provider.auth is not AuthKind.API_KEY:
        raise RuntimeUnsupported("DSH requires an API key provider")
    if ApiProtocol.ANTHROPIC_MESSAGES not in provider.protocols:
        raise RuntimeUnsupported("DSH requires an anthropic-messages provider")
    # Check the original catalog name before normalizing: an alias does not
    # grant membership to the corresponding raw model (or vice versa).
    if provider.models and model not in provider.models:
        raise RuntimeUnsupported(f"model {model!r} is not in provider {provider.name!r}'s catalog")
    if model not in MODEL_MAP:
        raise RuntimeUnsupported(f"DSH supports only {', '.join(MODEL_MAP)}; got {model!r}")
    if effort not in (None, "", "low", "high", "max"):
        raise RuntimeUnsupported("DSH effort must be low, high or max, or empty for high")
    try:
        _base_url(provider.base_url)
    except RegistryError as exc:
        raise RuntimeUnsupported(str(exc)) from None
    # DSH appends the Messages path itself. Preserve explicit gateway roots,
    # including /v1 and trailing slashes, rather than rewriting a Chat endpoint.
    return DshProviderPolicy(MODEL_MAP[model], effort or "high", provider.base_url or DEFAULT_BASE_URL)


def bind_provider(provider: ProviderSpec, model: str, effort: str | None = None) -> ProviderBinding:
    """Validate a launch and inject its explicit key, never an ambient login or key."""
    validate_provider(provider, model, effort)
    if not provider.api_key or not provider.api_key.strip():
        raise RuntimeUnsupported(f"provider {provider.name!r} has no API key: set one in Settings")
    return ProviderBinding(env=(*provider.env, (KEY_ENV, provider.api_key)))
