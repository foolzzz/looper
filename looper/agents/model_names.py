"""Shared model-name grammar and Claude Code's context-window convention."""

from __future__ import annotations

import re
from typing import NamedTuple


class ModelName(NamedTuple):
    base: str
    context_suffix: str | None


MODEL_MAX_CHARS = 200
# A base id, optionally followed by one legacy suffix (e.g. [1m]) or a
# comma-separated key=value list. Legacy tokens retain ':' and '/'; parameter
# keys and values use only ASCII letters, digits, '.', '_' and '-'. The final
# lookahead requires the true end of input, including for JSON Schema consumers
# where '$' alone can accept a trailing newline. Groups are base and suffix.
MODEL_NAME_PATTERN = (
    r"^([A-Za-z0-9][A-Za-z0-9._:/-]*)"
    r"(\[(?:[A-Za-z0-9._:/-]+|[A-Za-z0-9._-]+=[A-Za-z0-9._-]+"
    r"(?:,[A-Za-z0-9._-]+=[A-Za-z0-9._-]+)*)\])?(?![\s\S])"
)
MODEL_NAME_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MODEL_MAX_CHARS,
                     "pattern": MODEL_NAME_PATTERN}
MODEL_NAME = re.compile(MODEL_NAME_PATTERN)


def parse_model_name(name: str) -> ModelName:
    """Split a legacy context suffix (``[1m]``), preserving parameter lists as exact identities."""
    name = name.strip()
    match = MODEL_NAME.fullmatch(name)
    if match and (suffix := match[2]) and "=" not in suffix:
        return ModelName(match[1], suffix)
    return ModelName(name, None)
