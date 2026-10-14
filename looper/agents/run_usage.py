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
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
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
        )


def reported_totals(result_event: dict[str, Any] | None) -> Usage:
    """The session's running totals a result reports: its cost, and its tokens over every model.

    The tokens are None when the result has no ``modelUsage`` (an older Claude Code).
    """
    result = result_event or {}
    totals = Usage(cost_usd=_dollars(result.get("total_cost_usd")))
    models = result.get("modelUsage")
    if not isinstance(models, dict) or not models:
        return totals
    sums: dict[str, int] = {}
    for name, (model_key, _) in _TOKEN_FIELDS.items():
        counts = [_count(usage.get(model_key)) for usage in models.values() if isinstance(usage, dict)]
        if counts and all(count is not None for count in counts):
            sums[name] = sum(counts)  # type: ignore[arg-type]
    return replace(totals, **sums)


def turn_tokens(result_event: dict[str, Any] | None) -> Usage:
    """The tokens of the turn's own main-loop requests (``result.usage``): no compaction, no subagents."""
    usage = (result_event or {}).get("usage")
    if not isinstance(usage, dict):
        return Usage()
    return Usage(**{name: _count(usage.get(key)) for name, (_, key) in _TOKEN_FIELDS.items()})


def run_usage(result_event: dict[str, Any] | None, *, before: Usage | None = None) -> Usage:
    """A run's own usage: its result's totals less ``before``, the session's totals when it started.

    Tokens without a ``modelUsage`` to count them come from the turn's own ``usage``.
    """
    if result_event is None:
        return Usage()
    return reported_totals(result_event).since(before or Usage()).filled_from(turn_tokens(result_event))


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
