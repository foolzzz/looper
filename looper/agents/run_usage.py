"""What a Claude Code run spent and how large its context grew, read from its stream-json output.

Verified against Claude Code 2.1.285; the recorded examples are in
``tests/fixtures/claude_stream``.

- A result's ``total_cost_usd`` and ``modelUsage`` (per model:
  ``inputTokens``, ``outputTokens``, ``cacheReadInputTokens``,
  ``cacheCreationInputTokens``) are the running totals of the session in that
  process. They count every request, including the summary request of a
  context compaction and the requests of subagents. A process started with
  ``--resume`` carries on from the session's earlier totals, so its first
  result reports the earlier spending plus its own.
- A result's ``usage`` counts only the turn's own main-loop requests: no
  compaction request and no subagent. An assistant message's ``usage``
  describes the one request that wrote it, but its ``output_tokens`` is a
  snapshot taken while streaming.
- A request's prompt is its message's ``input_tokens`` +
  ``cache_read_input_tokens`` + ``cache_creation_input_tokens``. A subagent's
  messages carry the ``parent_tool_use_id`` of the tool call that started it.
- A compaction, automatic or ``/compact``, is a ``system`` event of subtype
  ``compact_boundary``. Its ``compact_metadata`` holds ``trigger`` ("auto" or
  "manual"), ``pre_tokens``, ``post_tokens``, ``duration_ms`` and more. A
  failed attempt is only a ``status`` event with ``compact_result: "failed"``.

So a run's own usage is the change in the reported totals, and its context is
the prompt of its last main-loop request.

``total_cost_usd`` is priced at Claude's prices whatever served the requests.
With a provider of its own prices (``runtimes.pricing``), the totals' cost is
each model's ``modelUsage`` tokens at that provider's prices instead
(``price``), so the change in the totals is still the run's own spend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Any

# The facts of a compaction kept with a run; the rest of compact_metadata stays in the transcript.
_COMPACTION_FIELDS = ("trigger", "pre_tokens", "post_tokens", "duration_ms")

# Usage field -> its name in modelUsage, in result.usage.
_TOKEN_FIELDS = {
    "input_tokens": ("inputTokens", "input_tokens"),
    "output_tokens": ("outputTokens", "output_tokens"),
    "cache_read_tokens": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "cache_creation_tokens": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
}


def _count(value: Any) -> int | None:
    """A token count, or None for anything that is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _dollars(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


class CostStatus(str, Enum):
    """Shared price/cost coverage state; unknown dollars are never a free run."""

    KNOWN = "known"
    UNKNOWN = "unknown"


def cost_status(cost: float | None) -> CostStatus:
    return CostStatus.UNKNOWN if cost is None else CostStatus.KNOWN


@dataclass(frozen=True)
class Usage:
    """Dollars and tokens: a run's own share, or a session's running totals.

    None means not reported. The four token kinds are the model's: uncached
    input, output, prompt read from the cache and prompt written to it.
    """

    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    # The part of ``cache_creation_tokens`` written for an hour rather than five minutes (a request's
    # ``cache_creation.ephemeral_1h_input_tokens``); None when the split is not reported.
    cache_creation_1h_tokens: int | None = None

    @property
    def cost_status(self) -> CostStatus:
        return cost_status(self.cost_usd)

    def since(self, earlier: Usage) -> Usage:
        """What was spent between ``earlier`` totals and these.

        A field below its earlier value counted from zero again (Claude did
        not carry the session's totals), so its value here is all new.
        """
        changes: dict[str, Any] = {}
        for item in fields(self):
            now, before = getattr(self, item.name), getattr(earlier, item.name)
            if now is None or not before or now < before:
                changes[item.name] = now
            elif item.name == "cost_usd":
                changes[item.name] = round(now - before, 10)  # without the float subtraction's noise
            else:
                changes[item.name] = now - before
        return Usage(**changes)

    def updated(self, reported: Usage) -> Usage:
        """These totals with every field ``reported`` has replaced."""
        return replace(self, **{f.name: v for f in fields(self) if (v := getattr(reported, f.name)) is not None})

    def plus(self, other: Usage) -> Usage:
        """Both usages added up; a field neither reports stays unreported."""
        sums: dict[str, Any] = {}
        for item in fields(self):
            mine, theirs = getattr(self, item.name), getattr(other, item.name)
            total = None if mine is None and theirs is None else (mine or 0) + (theirs or 0)
            sums[item.name] = round(total, 10) if item.name == "cost_usd" and total is not None else total
        return Usage(**sums)

    def filled_from(self, other: Usage) -> Usage:
        """This usage with its unreported fields taken from ``other``."""
        return replace(self, **{f.name: getattr(other, f.name) for f in fields(self) if getattr(self, f.name) is None})

    def to_dict(self) -> dict[str, Any]:
        """The reported fields, for a state file."""
        return {f.name: v for f in fields(self) if (v := getattr(self, f.name)) is not None}

    @classmethod
    def from_dict(cls, data: Any) -> Usage:
        """The usage a state file saved; anything unreadable is left out."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            cost_usd=_dollars(data.get("cost_usd")),
            **{name: _count(data.get(name)) for name in _TOKEN_FIELDS},
            cache_creation_1h_tokens=_count(data.get("cache_creation_1h_tokens")),
        )


# Prices one model's tokens (``runtimes.pricing.pricer``); None when it cannot.
Price = Callable[[str, Usage], "float | None"]


def _model_tokens(usage: dict[str, Any]) -> Usage:
    return Usage(**{name: _count(usage.get(model_key)) for name, (model_key, _) in _TOKEN_FIELDS.items()})


def reported_totals(result_event: dict[str, Any] | None, price: Price | None = None) -> Usage:
    """The session's running totals a result reports: its cost, and its tokens over every model.

    The tokens are None when the result has no ``modelUsage`` (an older Claude Code). With ``price``, the cost
    is each model's tokens at that price, None when one of them cannot be priced, instead of
    ``total_cost_usd``. An explicit ``usage_complete=False`` keeps cost unknown
    while retaining reported tokens, including a runtime's partial observations.
    """
    result = result_event or {}
    complete = result.get("usage_complete") is not False
    totals = Usage(cost_usd=_dollars(result.get("total_cost_usd")) if price is None and complete else None)
    models = result.get("modelUsage")
    if not isinstance(models, dict) or not models:
        return totals
    per_model = {str(name): _model_tokens(usage) for name, usage in models.items() if isinstance(usage, dict)}
    # modelUsage does not split a model's cache writes by how long they last; the turn's own usage does. When one
    # model wrote them all and the turn's writes are all of them (no request beside the turn's), that is their split.
    writers = [name for name, usage in per_model.items() if usage.cache_creation_tokens]
    turn = turn_tokens(result)
    if (len(writers) == 1 and turn.cache_creation_1h_tokens is not None
            and per_model[writers[0]].cache_creation_tokens == turn.cache_creation_tokens):
        per_model[writers[0]] = replace(per_model[writers[0]], cache_creation_1h_tokens=turn.cache_creation_1h_tokens)
    sums: dict[str, Any] = {}
    for name in (*_TOKEN_FIELDS, "cache_creation_1h_tokens"):
        counts = [getattr(usage, name) for usage in per_model.values()]
        if counts and all(count is not None for count in counts):
            sums[name] = sum(counts)
    if price is not None and per_model and complete:
        costs = [price(name, usage) for name, usage in per_model.items()]
        sums["cost_usd"] = None if any(c is None for c in costs) else round(sum(costs), 10)  # type: ignore[arg-type]
    return replace(totals, **sums)


def request_tokens(usage: Any) -> Usage:
    """The tokens of one model request from its API ``usage`` (``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``, and the one-hour part of the writes from
    ``cache_creation``)."""
    if not isinstance(usage, dict):
        return Usage()
    split = usage.get("cache_creation")
    one_hour = _count(split.get("ephemeral_1h_input_tokens", 0)) if isinstance(split, dict) else None
    return Usage(**{name: _count(usage.get(key)) for name, (_, key) in _TOKEN_FIELDS.items()},
                 cache_creation_1h_tokens=one_hour)


def cache_split_known(result_event: dict[str, Any] | None) -> bool:
    """Whether every cache write the result reports is known to be a five-minute or a one-hour one."""
    totals = reported_totals(result_event)
    if totals.cache_creation_tokens is None:  # no modelUsage: the turn's own usage is all it reports
        totals = turn_tokens(result_event)
    return not totals.cache_creation_tokens or totals.cache_creation_1h_tokens is not None


def turn_tokens(result_event: dict[str, Any] | None) -> Usage:
    """The tokens of the turn's own main-loop requests (``result.usage``): no compaction, no subagents."""
    return request_tokens((result_event or {}).get("usage"))


def run_usage(result_event: dict[str, Any] | None, *, before: Usage | None = None, price: Price | None = None,
              ) -> Usage:
    """A run's own usage: its result's totals (priced with ``price``, if given) less ``before``, the session's
    totals when it started.

    Tokens without a ``modelUsage`` to count them come from the turn's own ``usage``.
    """
    if result_event is None:
        return Usage()
    return reported_totals(result_event, price).since(before or Usage()).filled_from(turn_tokens(result_event))


def request_context(event: dict[str, Any]) -> int | None:
    """The prompt size of the main-loop model request behind an assistant message, else None.

    A subagent's messages (with a ``parent_tool_use_id``) are its own context, not the agent's.
    """
    if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
        return None
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    parts = [_count(usage.get(key)) for key in
             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if parts[0] is None:
        return None
    return sum(part or 0 for part in parts)


def compaction(event: dict[str, Any]) -> dict[str, Any] | None:
    """A compaction's trigger and its context before and after, from its ``compact_boundary`` event."""
    if event.get("type") != "system" or event.get("subtype") != "compact_boundary":
        return None
    metadata = event.get("compact_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {key: metadata[key] for key in _COMPACTION_FIELDS if metadata.get(key) is not None}
