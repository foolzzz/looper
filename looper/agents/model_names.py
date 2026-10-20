"""Model names and Claude Code's trailing context-window convention."""

from __future__ import annotations

import re
from typing import NamedTuple


class ModelName(NamedTuple):
    base: str
    context_suffix: str | None


_CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")


def parse_model_name(name: str) -> ModelName:
    """Split a trailing bracketed context suffix (``[1m]``); leave the API name and any date intact."""
    name = name.strip()
    suffix = _CONTEXT_SUFFIX.search(name)
    return ModelName(name[:suffix.start()], suffix.group()) if suffix else ModelName(name, None)
