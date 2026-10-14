"""Prices: a run's tokens at its provider's prices, and the per-run spend cap Looper enforces itself (RFC section 8).

A model has four prices, in US dollars per million tokens: uncached input,
output, prompt read from the cache and prompt written to it. A provider's model
catalog (``ProviderSpec.catalog``) sets them; each one it leaves unknown comes
from the built-in table below, which ships with this version. A model that has
no price for a kind of token its run used is unpriced: the run records its
tokens only, and no spend cap can apply to it.

The built-in table (``BUILTIN_PRICES``) holds the vendors' official standard
pay-as-you-go list prices, read on ``PRICES_AS_OF`` from ``PRICE_SOURCES``.
Where a vendor charges nothing extra for writing to its cache, a written token
is billed as input, so the cache write price is the input price. Not modelled:
OpenAI's long-context rates (inputs over 272K tokens), DeepSeek's off-peak
discount, and the CNY prices of the China platforms (BigModel, Moonshot China),
whose models use these USD list prices unless a provider's catalog says
otherwise.

A model is looked up by the name its runtime reports (Claude Code's
``modelUsage`` keys), without a context-window suffix (``glm-5.3[1m]``) or a
date suffix (``claude-haiku-4-5-20251001``).

A runtime reports a run's tokens as internal ``usage`` events (``usage_event``):
the tokens of one model request (Claude Code) or of one turn (a runtime that
reports usage only when a turn completes), never running totals. ``RunSpend``
adds them up at the provider's prices and says when the run went over its cap
(``cap_exceeded``): a soft cap, since the request that crosses it is already
made and paid.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

from ..run_usage import Price, Usage
from . import CostSource, RunCost

if TYPE_CHECKING:
    from ..profile_registry import ProviderSpec

PRICE_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")


@dataclass(frozen=True)
class ModelPrice:
    """A model's four prices in USD per million tokens; None where unknown."""

    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cache_read_usd_per_mtok: float | None = None
    cache_write_usd_per_mtok: float | None = None

    @property
    def complete(self) -> bool:
        """Every price is known, so every run of the model can be priced."""
        return all(getattr(self, name) is not None for name in PRICE_FIELDS)

    def cost(self, usage: Usage) -> float | None:
        """What ``usage``'s tokens cost: None when it reports no tokens, or uses a kind whose price is unknown."""
        counts = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_creation_tokens)
        if all(count is None for count in counts):
            return None
        total = 0.0
        for count, name in zip(counts, PRICE_FIELDS):
            if not count:
                continue
            price = getattr(self, name)
            if price is None:
                return None
            total += count * price
        return round(total / 1_000_000, 10)

    def view(self) -> dict[str, float | None]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


PRICES_AS_OF = "2026-10-14"
PRICE_SOURCES = (
    "https://platform.claude.com/docs/en/about-claude/pricing",
    "https://developers.openai.com/api/docs/pricing",
    "https://docs.z.ai/guides/overview/pricing",
    "https://api-docs.deepseek.com/quick_start/pricing",
    "https://platform.kimi.ai/docs/pricing/chat.md",
)


def _p(input_usd: float, output_usd: float, cache_read_usd: float, cache_write_usd: float) -> ModelPrice:
    return ModelPrice(input_usd, output_usd, cache_read_usd, cache_write_usd)


# Model -> (input, output, cache read, cache write), USD per million tokens, as of PRICES_AS_OF.
BUILTIN_PRICES: Mapping[str, ModelPrice] = {
    # Anthropic: a 5-minute cache write is 1.25x input.
    "claude-fable-5-1": _p(10.00, 50.00, 0.25, 12.50),
    "claude-opus-5-5": _p(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": _p(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": _p(1.00, 5.00, 0.10, 1.25),
    # OpenAI, standard tier, up to 272K input tokens: GPT-5.6 and later bill a cache write at 1.25x input.
    "gpt-6.1-sol": _p(2.00, 10.00, 0.10, 2.50),
    "gpt-6-sol": _p(2.00, 10.00, 0.20, 2.50),
    "gpt-6-luna": _p(0.10, 0.50, 0.01, 0.125),
    "gpt-6-astra": _p(10.00, 50.00, 1.00, 12.50),
    "gpt-5.6-sol": _p(4.00, 20.00, 0.40, 5.00),
    "gpt-5.6-terra": _p(2.00, 12.00, 0.20, 2.50),
    "gpt-5.6-luna": _p(0.20, 1.20, 0.02, 0.25),
    "gpt-5.5": _p(5.00, 30.00, 0.50, 5.00),
    "gpt-5.4-mini": _p(0.75, 4.50, 0.075, 0.75),
    # Z.ai (GLM): cache storage is free for now; a written token is billed as input.
    "glm-5.3": _p(1.40, 4.40, 0.26, 1.40),
    "glm-5.3-flash": _p(0.15, 0.50, 0.03, 0.15),
    "glm-5.3-flashx": _p(0.37, 1.25, 0.075, 0.37),
    "glm-5.2": _p(1.40, 4.40, 0.26, 1.40),
    "glm-5.1": _p(1.40, 4.40, 0.26, 1.40),
    "glm-5": _p(1.00, 3.20, 0.20, 1.00),
    "glm-4.7": _p(0.60, 2.20, 0.11, 0.60),
    "glm-4.6": _p(0.60, 2.20, 0.11, 0.60),
    "glm-4.5-air": _p(0.20, 1.10, 0.03, 0.20),
    # DeepSeek, peak hours (off-peak is half): a cache miss is input, a cache hit the read price.
    "deepseek-flash": _p(0.30, 1.20, 0.006, 0.30),
    "deepseek-v4-pro": _p(1.32, 3.96, 0.044, 1.32),
    # Moonshot (Kimi), international platform: only kimi-k3 bills a cache write (5 minutes) beyond input.
    "kimi-k3": _p(3.00, 15.00, 0.30, 3.00),
    "kimi-k2.7-code": _p(0.95, 4.00, 0.19, 0.95),
    "kimi-k2.7-code-highspeed": _p(1.90, 8.00, 0.38, 1.90),
    "kimi-k2.6": _p(0.95, 4.00, 0.16, 0.95),
}

_SUFFIX = re.compile(r"\[[^\]]*\]$")
_DATE = re.compile(r"-\d{8}$")


def base_model(name: str) -> str:
    """``name`` without a context-window suffix (``[1m]``) or a date suffix (``-20251001``), lower case."""
    return _DATE.sub("", _SUFFIX.sub("", name.strip())).lower()


def model_price(provider: ProviderSpec, model: str) -> ModelPrice:
    """``model``'s prices with ``provider``: each from the provider's catalog when it gives one, else from the
    built-in table."""
    base = base_model(model)
    builtin = BUILTIN_PRICES.get(base, ModelPrice())
    entry = provider.catalog_model(model) or next((m for m in provider.catalog if base_model(m.name) == base), None)
    if entry is None:
        return builtin
    return ModelPrice(**{name: getattr(entry, name) if getattr(entry, name) is not None else getattr(builtin, name)
                         for name in PRICE_FIELDS})


def price_cost(usage: Usage, provider: ProviderSpec, model: str) -> RunCost:
    """``usage`` priced at ``provider``'s prices for ``model``: its tokens always, its ``cost_usd`` None when a
    price it needs is unknown."""
    return RunCost(replace(usage, cost_usd=model_price(provider, model).cost(usage)), CostSource.PROVIDER_PRICE)


def pricer(provider: ProviderSpec) -> Price:
    """What prices a model's tokens at ``provider``'s prices (for ``run_usage`` to price a per-model report)."""
    return lambda model, usage: model_price(provider, model).cost(usage)


def cap_exceeded(cost_so_far: float | None, cap: float | None) -> bool:
    """Whether a run that has spent ``cost_so_far`` went over its per-run ``cap``; never for an unpriced run or
    without a cap."""
    return cost_so_far is not None and cap is not None and cost_so_far > cap


# --- Internal usage events ---

USAGE_EVENT = "usage"


def usage_event(model: str | None, usage: Usage, **extra: Any) -> dict[str, Any]:
    """The internal event for the tokens of one model request or turn (not running totals)."""
    tokens = {name: value for name, value in usage.to_dict().items() if name != "cost_usd"}
    return {"type": USAGE_EVENT, "model": model, "usage": tokens, **extra}


def event_usage(event: Mapping[str, Any]) -> tuple[str | None, Usage] | None:
    """The model and tokens of an internal usage event; None for any other event."""
    if event.get("type") != USAGE_EVENT:
        return None
    model = event.get("model")
    return (model if isinstance(model, str) and model else None), replace(Usage.from_dict(event.get("usage")),
                                                                          cost_usd=None)


class RunSpend:
    """One run's spend at its provider's prices so far, from its usage events, against its per-run cap.

    ``usage`` sums the events' tokens and their cost; its ``cost_usd`` is None
    once one event cannot be priced, which takes the run out of the cap.
    """

    def __init__(self, provider: ProviderSpec, model: str, cap: float | None) -> None:
        self._provider = provider
        self._model = model
        self.cap = cap
        self.usage = Usage()
        self._unpriced = False

    def add(self, event: Mapping[str, Any]) -> bool:
        """Count ``event`` if it is a usage event; True when it is the one that took the run over its cap."""
        found = event_usage(event)
        if found is None:
            return False
        model, usage = found
        was = self.exceeded
        cost = model_price(self._provider, model or self._model).cost(usage)
        self._unpriced = self._unpriced or (cost is None and usage != Usage())
        spent = None if self._unpriced else round((self.usage.cost_usd or 0.0) + (cost or 0.0), 10)
        self.usage = replace(self.usage.plus(usage), cost_usd=spent)
        return not was and self.exceeded

    @property
    def exceeded(self) -> bool:
        return cap_exceeded(self.usage.cost_usd, self.cap)
