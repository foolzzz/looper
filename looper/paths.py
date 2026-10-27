"""Where Looper keeps runtime state: ``--runtime-root``, else ``~/.looper``."""

from __future__ import annotations

from pathlib import Path

STATE_DIRNAME = ".looper"


def home_runtime_root(home: Path | None = None) -> Path:
    """Return the runtime root Looper uses under ``home`` when none is configured."""

    return (Path.home() if home is None else Path(home)) / STATE_DIRNAME
