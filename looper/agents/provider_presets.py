"""Provider presets: what a new provider for a known vendor is filled with, so the user only adds the key.

A preset names a vendor's APIs, each a set of protocols at one base URL (an
Anthropic-format API for Claude Code; an OpenAI-format one for an OpenAI-type
runtime), its common models with a suggested background model and context
window, and the extra environment its Claude Code guide sets. An API may list
its own models; a provider made from that API uses them instead of the
preset's, which is how a vendor that serves each model family through one
endpoint (OpenCode) keeps a family off the endpoints that reject it. A
provider holds one base URL, so a provider made from a preset uses one of its
APIs. Prices are not part of a preset: the built-in table (``runtimes.pricing``)
prices these models, and the provider's catalog can override it.

The URLs, models and variables come from each vendor's official documentation
(``docs``), read on 2026-10-14. OpenCode and Sub2API were read on 2026-10-20.
``[1m]`` asks Claude Code for a model's 1M context window, as the vendors'
Claude Code guides write it; it is added to every model with a 1M window.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .runtimes import ApiProtocol

_ANTHROPIC = (ApiProtocol.ANTHROPIC_MESSAGES,)
_CHAT = (ApiProtocol.OPENAI_CHAT,)
_RESPONSES = (ApiProtocol.OPENAI_RESPONSES,)
_OPENAI = (ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT)


@dataclass(frozen=True)
class PresetApi:
    """One API of a vendor: the protocols it serves at ``base_url``.

    ``models``, when set, replaces the preset's models for a provider built
    from this API. Unset, that provider lists the preset's models.
    """

    protocols: tuple[ApiProtocol, ...]
    base_url: str
    models: tuple[PresetModel, ...] | None = None


@dataclass(frozen=True)
class PresetModel:
    name: str
    context_window: int | None = None
    background_model: str | None = None


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    name: str  # the provider name it suggests
    apis: tuple[PresetApi, ...]
    models: tuple[PresetModel, ...]
    env: tuple[tuple[str, str], ...]  # what the vendor's Claude Code guide sets beside the model mapping
    docs: tuple[str, ...]
    note: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "preset_id": self.id, "label": self.label, "name": self.name,
            "apis": [_api_view(api) for api in self.apis],
            "models": [_model_view(model) for model in self.models],
            "env": [{"key": key, "value": value} for key, value in self.env],
            "docs": list(self.docs), "note": self.note,
        }


def _model_view(model: PresetModel) -> dict[str, Any]:
    return {"name": model.name, "context_window": model.context_window, "background_model": model.background_model}


def _api_view(api: PresetApi) -> dict[str, Any]:
    """One API as the registry shows it. ``models`` is present only when this API lists its own."""
    shown: dict[str, Any] = {"protocols": [protocol.value for protocol in api.protocols], "base_url": api.base_url}
    if api.models is not None:
        shown["models"] = [_model_view(model) for model in api.models]
    return shown


def api_models(preset: Mapping[str, Any], api: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The models a provider built from ``api`` lists: that API's own, when set, else the preset's."""
    return list(api["models"] if "models" in api else preset["models"])


_GLM_MODELS = (
    PresetModel("glm-5.3[1m]", 1_000_000, "glm-5.3-flash[1m]"),
    PresetModel("glm-5.3-flash[1m]", 1_000_000, "glm-5.3-flash[1m]"),
    PresetModel("glm-5.2[1m]", 1_000_000, "glm-5.3-flash[1m]"),
    PresetModel("glm-4.7", 200_000, "glm-4.5-air"),
)
_GLM_ENV = (("API_TIMEOUT_MS", "3000000"), ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),
            ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"))
_KIMI_MODELS = (
    PresetModel("kimi-k3[1m]", 1_048_576, "kimi-k2.7-code"),
    PresetModel("kimi-k2.7-code", 262_144, "kimi-k2.7-code"),
    PresetModel("kimi-k2.6", 262_144, "kimi-k2.6"),
)
_KIMI_ENV = (("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),)
_KIMI_NOTE = ("kimi-k2.7-code always thinks; Moonshot's Claude Code guide uses it as the background model, and asks "
              "for thinking to be on")
# OpenCode serves each family through one endpoint (https://opencode.ai/v2/docs/console/models/):
# Responses the GPT models, chat completions DeepSeek, GLM and Kimi, Anthropic messages the Claude models.
_OPENCODE_RESPONSES = (PresetModel("gpt-6.1-sol", 1_050_000), PresetModel("gpt-5.6-sol", 1_050_000))
_OPENCODE_CHAT = (PresetModel("deepseek-v4-pro"), PresetModel("glm-5.3"), PresetModel("kimi-k3"))
_OPENCODE_ANTHROPIC = (PresetModel("claude-opus-5-5"), PresetModel("claude-sonnet-5-5"))
_OPENCODE_URL = "https://opencode.ai/inference/openai/v1"

PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "glm-bigmodel", "GLM (BigModel, China)", "glm",
        (PresetApi(_ANTHROPIC, "https://open.bigmodel.cn/api/anthropic"),
         PresetApi(_CHAT, "https://open.bigmodel.cn/api/paas/v4"),
         PresetApi(_RESPONSES, "https://open.bigmodel.cn/api/v1")),
        _GLM_MODELS, _GLM_ENV,
        ("https://docs.bigmodel.cn/cn/coding-plan/tool/claude", "https://docs.bigmodel.cn/cn/guide/start/quick-start",
         "https://docs.bigmodel.cn/cn/guide/start/model-overview"),
        "BigModel bills in CNY; Looper prices these models at Z.ai's USD list prices unless the catalog says "
        "otherwise",
    ),
    ProviderPreset(
        "glm-zai", "GLM (Z.ai, international)", "glm-zai",
        (PresetApi(_ANTHROPIC, "https://api.z.ai/api/anthropic"),
         PresetApi(_CHAT, "https://api.z.ai/api/paas/v4"),
         PresetApi(_RESPONSES, "https://api.z.ai/api/v1")),
        _GLM_MODELS, _GLM_ENV,
        ("https://docs.z.ai/devpack/tool/claude", "https://docs.z.ai/guides/overview/quick-start",
         "https://docs.z.ai/guides/llm/glm-5.3"),
    ),
    ProviderPreset(
        "deepseek", "DeepSeek", "deepseek",
        (PresetApi(_ANTHROPIC, "https://api.deepseek.com/anthropic"),
         PresetApi(_CHAT, "https://api.deepseek.com")),
        (PresetModel("deepseek-flash[1m]", 1_000_000, "deepseek-flash"),
         PresetModel("deepseek-v4-pro[1m]", 1_000_000, "deepseek-flash")),
        (("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "786432"),),
        ("https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/",
         "https://api-docs.deepseek.com/guides/anthropic_api", "https://api-docs.deepseek.com/"),
    ),
    ProviderPreset(
        "kimi-moonshot-cn", "Kimi (Moonshot, China)", "kimi",
        (PresetApi(_ANTHROPIC, "https://api.moonshot.cn/anthropic"), PresetApi(_OPENAI, "https://api.moonshot.cn/v1")),
        _KIMI_MODELS, _KIMI_ENV,
        ("https://platform.kimi.com/docs/guide/claude-code-kimi.md", "https://platform.kimi.com/docs/api/overview"),
        _KIMI_NOTE + "; Moonshot China bills in CNY, and Looper prices these models at the international USD list "
                     "prices unless the catalog says otherwise",
    ),
    ProviderPreset(
        "kimi-moonshot", "Kimi (Moonshot, international)", "kimi-intl",
        (PresetApi(_ANTHROPIC, "https://api.moonshot.ai/anthropic"), PresetApi(_OPENAI, "https://api.moonshot.ai/v1")),
        _KIMI_MODELS, _KIMI_ENV,
        ("https://platform.kimi.ai/docs/guide/claude-code-kimi.md", "https://platform.kimi.ai/docs/api/overview.md"),
        _KIMI_NOTE,
    ),
    ProviderPreset(
        "cpa", "CLIProxyAPI on this machine", "cpa",
        (PresetApi(_OPENAI, "http://127.0.0.1:8317/v1"), PresetApi(_ANTHROPIC, "http://127.0.0.1:8317")),
        (PresetModel("gpt-6.1-sol", 1_050_000, "gpt-6-luna"), PresetModel("gpt-5.6-sol", 1_050_000, "gpt-5.6-luna"),
         PresetModel("gpt-5.6-luna", 1_050_000, "gpt-5.6-luna")),
        (),
        (),
        "a local proxy for development: its key is one of the api-keys in its config file, and its models are "
        "those it serves",
    ),
    ProviderPreset(
        "opencode", "OpenCode (Zen)", "opencode",
        (PresetApi(_RESPONSES, _OPENCODE_URL, _OPENCODE_RESPONSES),
         PresetApi(_CHAT, _OPENCODE_URL, _OPENCODE_CHAT),
         PresetApi(_ANTHROPIC, "https://opencode.ai/inference/anthropic", _OPENCODE_ANTHROPIC)),
        (*_OPENCODE_RESPONSES, *_OPENCODE_ANTHROPIC, *_OPENCODE_CHAT),
        (),
        ("https://opencode.ai/v2/docs/console/models/",),
        "the address may be checked against https://opencode.ai/v2/docs/console/models/; the model list is "
        "https://opencode.ai/inference/v1/models. OpenCode prices some models as ranges, so they are not in the "
        "built-in table; a model whose name is in that table is priced at the vendor list price. Its key is an "
        "OpenCode API key (OPENCODE_API_KEY), sent as Authorization: Bearer",
    ),
    ProviderPreset(
        "sub2api", "Sub2API on this machine", "sub2api",
        (PresetApi(_OPENAI, "http://127.0.0.1:8080/v1"), PresetApi(_ANTHROPIC, "http://127.0.0.1:8080")),
        (PresetModel("gpt-6.1-sol", 1_050_000, "gpt-6-luna"), PresetModel("gpt-5.6-sol", 1_050_000, "gpt-5.6-luna"),
         PresetModel("gpt-5.6-luna", 1_050_000, "gpt-5.6-luna")),
        (),
        ("https://github.com/Wei-Shaw/sub2api",),
        "a self-hosted gateway: its key is an sk-... key the gateway issues, sent as Authorization: Bearer, and its "
        "models depend on the accounts behind it, so match this list to /v1/models",
    ),
)


def presets_view() -> list[dict[str, Any]]:
    return [preset.view() for preset in PRESETS]
