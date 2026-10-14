"""Where LoopX keeps its runtime state: ``LOOPX_RUNTIME_ROOT``, else ``~/.loopx``."""

from __future__ import annotations

import os
from pathlib import Path


RUNTIME_ROOT_ENV = "LOOPX_RUNTIME_ROOT"
LOOPX_STATE_DIRNAME = ".loopx"


def home_runtime_root(home: Path | None = None) -> Path:
    """Return the runtime root LoopX uses under ``home`` when none is configured."""

    return (Path.home() if home is None else Path(home)) / LOOPX_STATE_DIRNAME


def configured_runtime_root() -> Path | None:
    """Return the runtime root named by ``LOOPX_RUNTIME_ROOT``, made absolute.

    An unset or blank value configures nothing. A relative value is resolved
    against the current directory at the moment it is read, lexically, so the
    same value names the same directory for every consumer in the process.
    """

    value = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
    if not value:
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def default_runtime_root() -> Path:
    """Return the runtime root used when no explicit root is given.

    ``LOOPX_RUNTIME_ROOT`` when it is set, otherwise ``~/.loopx``. It is read at
    call time, never cached at import, so HOME and the environment in effect
    when the root is needed decide it. ``loopx serve --runtime-root`` takes
    precedence over this default.
    """

    return configured_runtime_root() or home_runtime_root()
