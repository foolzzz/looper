"""Literal lookup of operator-selected environment variable names."""

from __future__ import annotations

import os


def get(name: str, default: str | None = None) -> str | None:
    """Return the named variable, including an empty value, or ``default``."""
    return os.environ.get(name, default)
