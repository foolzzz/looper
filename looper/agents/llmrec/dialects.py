"""Typed Chat Completions differences, selected by model rather than model-name guesses."""
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal, Mapping

from looper.agents.runtimes import VendorType


class ChatDialect(StrEnum):
    GENERIC = 'generic'
    OPENAI = 'openai'
    DEEPSEEK = 'deepseek'
    MOONSHOT = 'moonshot'
    ZHIPU = 'zhipu'
    QWEN = 'qwen'
    VLLM = 'vllm'
    MINIMAX = 'minimax'
    ARK = 'ark'
    OPENROUTER = 'openrouter'


class ReasoningReplay(StrEnum):
    ALL = 'all'
    TOOL_LOOP = 'tool_loop'
    NONE = 'none'


class MidSystem(StrEnum):
    SYSTEM = 'system'
    USER = 'user'


class CacheDialect(StrEnum):
    AUTOMATIC = 'automatic'


class UsageField(StrEnum):
    PROMPT_CACHE_HIT_TOKENS = 'prompt_cache_hit_tokens'
    CACHED_TOKENS = 'prompt_tokens_details.cached_tokens'


@dataclass(frozen=True)
class Dialect:
    name: ChatDialect
    max_tokens_field: Literal['max_tokens', 'max_completion_tokens'] = 'max_tokens'
    max_output_cap: int | None = None
    reasoning_field: Literal['reasoning_content', 'reasoning'] | None = 'reasoning_content'
    reasoning_replay: ReasoningReplay = ReasoningReplay.ALL
    thinking_on: Mapping[str, Any] | None = None
    thinking_off: Mapping[str, Any] | None = None
    effort_field: str | None = None
    usage_fields: tuple[UsageField, ...] = (UsageField.CACHED_TOKENS,)
    include_usage: bool = True
    prompt_cache_key: bool = False
    send_user: bool = False
    parallel_tool_calls: bool = True
    tool_choice: frozenset[str] = frozenset({'auto', 'none', 'required', 'named'})
    rejects: frozenset[str] = frozenset()
    temperature_range: tuple[float, float] | None = None
    max_stop: int | None = None
    images: bool = False
    mid_system: MidSystem = MidSystem.USER
    empty_assistant_content: Literal[''] | None = None
    context_overflow_codes: frozenset[str] = frozenset({'context_length_exceeded'})
    billing_codes: frozenset[str] = frozenset({'insufficient_quota', 'insufficient_balance'})
    cache: CacheDialect = CacheDialect.AUTOMATIC
    extra: Mapping[str, Any] = field(default_factory=dict)
    rejects_when_thinking: frozenset[str] = frozenset()
    reasoning_fill_empty: bool = False
    tool_message_name: bool = False
    base64_images_only: bool = False


_GENERIC = Dialect(ChatDialect.GENERIC)
DIALECTS: Mapping[ChatDialect, Dialect] = {
    # Generic is deliberately conservative; https://developers.openai.com/api/reference/resources/chat/ — 2026-11-05 查.
    ChatDialect.GENERIC: _GENERIC,
    # https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create — 2026-11-05 查.
    ChatDialect.OPENAI: Dialect(ChatDialect.OPENAI, max_tokens_field='max_completion_tokens', reasoning_field=None,
        reasoning_replay=ReasoningReplay.NONE, effort_field='reasoning_effort', prompt_cache_key=True,
        extra={'store': False}, rejects_when_thinking=frozenset({'temperature', 'top_p'}), max_stop=4, images=True),
    # https://api-docs.deepseek.com/api/create-chat-completion ; https://api-docs.deepseek.com/guides/thinking_mode — 2026-11-05 查.
    ChatDialect.DEEPSEEK: Dialect(ChatDialect.DEEPSEEK, max_output_cap=393216, reasoning_fill_empty=True,
        thinking_on={'thinking': {'type': 'enabled'}}, thinking_off={'thinking': {'type': 'disabled'}},
        effort_field='reasoning_effort', usage_fields=(UsageField.PROMPT_CACHE_HIT_TOKENS, UsageField.CACHED_TOKENS),
        rejects_when_thinking=frozenset({'temperature', 'top_p', 'presence_penalty', 'frequency_penalty'}), max_stop=16),
    # https://platform.kimi.ai/docs/api/chat ; https://platform.kimi.ai/docs/guide/context-caching.md — 2026-11-05 查.
    ChatDialect.MOONSHOT: Dialect(ChatDialect.MOONSHOT, max_tokens_field='max_completion_tokens',
        effort_field='reasoning_effort', prompt_cache_key=True, tool_message_name=True,
        rejects=frozenset({'temperature'}), max_stop=5, images=True, base64_images_only=True),
    # https://docs.z.ai/api-reference/llm/chat-completion ; https://docs.z.ai/guides/capabilities/thinking-mode — 2026-11-05 查.
    ChatDialect.ZHIPU: Dialect(ChatDialect.ZHIPU, max_output_cap=131072,
        thinking_on={'thinking': {'type': 'enabled', 'clear_thinking': False}},
        thinking_off={'thinking': {'type': 'disabled'}}, tool_choice=frozenset({'auto'}),
        max_stop=1, temperature_range=(0.01, 1)),
    # https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions — 2026-11-05 查.
    ChatDialect.QWEN: Dialect(ChatDialect.QWEN, max_tokens_field='max_completion_tokens',
        thinking_on={'enable_thinking': True}, thinking_off={'enable_thinking': False},
        tool_choice=frozenset({'auto', 'none', 'named'}), temperature_range=(0, 1.99)),
    # https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html — 2026-11-05 查; cached usage needs server opt-in.
    ChatDialect.VLLM: Dialect(ChatDialect.VLLM, reasoning_field='reasoning', send_user=False,
        thinking_on={'chat_template_kwargs': {'enable_thinking': True}},
        thinking_off={'chat_template_kwargs': {'enable_thinking': False}}),
    # Unverified: generic fallback; https://platform.minimax.io/docs/api-reference/text-openai-api — 2026-11-05 查.
    ChatDialect.MINIMAX: replace(_GENERIC, name=ChatDialect.MINIMAX),
    # Unverified: generic fallback; https://docs.volcengine.com/docs/82379/1494384 — 2026-11-05 查.
    ChatDialect.ARK: replace(_GENERIC, name=ChatDialect.ARK),
    # Unverified: opaque reasoning replay needs a future signature kind; https://openrouter.ai/docs/guides/best-practices/reasoning-tokens — 2026-11-05 查.
    ChatDialect.OPENROUTER: replace(_GENERIC, name=ChatDialect.OPENROUTER),
}


def default_dialect(vendor_type: VendorType) -> ChatDialect:
    if vendor_type is VendorType.DEEPSEEK:
        return ChatDialect.DEEPSEEK
    if vendor_type in (VendorType.KIMI_MOONSHOT, VendorType.KIMI_MOONSHOT_CN):
        return ChatDialect.MOONSHOT
    if vendor_type in (VendorType.GLM_BIGMODEL, VendorType.GLM_ZAI):
        return ChatDialect.ZHIPU
    return ChatDialect.GENERIC
