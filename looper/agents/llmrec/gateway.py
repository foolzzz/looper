"""Launch-side contract for models that require the local translation gateway."""
from __future__ import annotations

from . import LlmrecSkip, SkipReason


def translated(launch) -> bool:
    from ..runtimes import ModelPath, model_path

    resolved = getattr(launch.profile, "resolved_model", None)
    protocols = (resolved.model.protocol,) if resolved is not None else launch.profile.provider.protocols
    return len(protocols) == 1 and model_path(launch.runtime, next(iter(protocols))) is ModelPath.TRANSLATED


def dialect_for(launch) -> str:
    from .dialects import ChatDialect, default_dialect

    from ..runtimes import VendorType

    resolved = getattr(launch.profile, "resolved_model", None)
    model = resolved.model if resolved is not None else launch.profile.provider.catalog_model(launch.model)
    provider = resolved.provider if resolved is not None else launch.profile.provider
    return ChatDialect((model.chat_dialect if model is not None else None)
                       or default_dialect(getattr(provider, "vendor_type", VendorType.CUSTOM))).value


def gateway_refusal(route, model: str = '') -> str:
    prefix = (f"The model {model} speaks OpenAI Chat Completions; Claude Code reaches it only through "
              "Looper's local gateway, but ")
    if isinstance(route, LlmrecSkip):
        if route.reason is SkipReason.PROXY_OUTDATED:
            return prefix + ("the running gateway is an older version kept alive by running agents; "
                             "it is replaced once they finish.")
        if route.reason is SkipReason.CREDENTIALS_IN_URL:
            return prefix + "the upstream URL contains credentials. Move credentials to the provider API key."
        if route.reason is SkipReason.GATEWAY_UNAVAILABLE:
            return prefix + ("the gateway could not start" + (f": {route.detail}" if route.detail else "")
                             + ". Check `looper doctor` → Local gateway.")
    return prefix + "the gateway is not installed in this process."
