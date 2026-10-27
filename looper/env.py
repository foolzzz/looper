"""Environment lookup with compatibility for variables captured by existing units."""

from __future__ import annotations

import os


def get(name: str, default: str | None = None) -> str | None:
    """Read the named variable first (even if empty), then its pre-rename ``LOOPX_`` name."""
    if name in os.environ:
        return os.environ[name]
    if name.startswith("LOOPER_"):
        return os.environ.get("LOOPX_" + name[len("LOOPER_"):], default)
    return default
