"""Shared resolved Pi launches at the runtime binding boundary."""
from typing import Any

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind
from tests.runtime_model_helpers import resolved_profile

KEY = "sk-test-pi-provider-only"


def provider(**fields: Any) -> ProviderSpec:
    return ProviderSpec(**{"id": "prov-test", "name": "arbitrary", "auth": AuthKind.API_KEY,
                           "protocols": frozenset({ApiProtocol.OPENAI_RESPONSES}),
                           "base_url": "https://endpoint.example.invalid/v1", "api_key": KEY, **fields})


def launch(provider: ProviderSpec, model: str = "selected", effort: str = "high") -> Launch:
    profile = resolved_profile("agent-pi", "pi/test", RuntimeSpec("rt-pi", "pi", "pi"), provider, model, effort)
    return Launch(AgentSettings(profile=profile))

