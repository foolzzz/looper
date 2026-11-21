"""Shared formatting for sender-controlled text shown to a model."""

from __future__ import annotations

import unicodedata

__all__ = ["display_name"]


def display_name(raw: str, limit: int = 40) -> str:
    """A single, bounded label, never identity or authority.

    Use for model-visible sender names in turn context, group openers, cards
    and summaries. Callers must still label the result as sender-set text.
    """
    if limit < 1:
        raise ValueError("display name limit must be positive")
    clean = "".join(" " if char.isspace() else char for char in raw
                    if char not in "()[]\"':`" and (char.isspace() or not unicodedata.category(char).startswith("C")))
    clean = " ".join(clean.split()) or "unnamed sender"
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"
