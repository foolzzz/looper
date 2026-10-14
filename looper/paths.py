"""Where Looper keeps its runtime state: ``LOOPER_RUNTIME_ROOT``, else ``~/.looper``.

Before the rename the default was ``~/.loopx``. The first ``looper serve`` or
``looper service install`` that uses ``~/.looper`` while only ``~/.loopx``
exists moves it there (``settle_runtime_root``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import env


RUNTIME_ROOT_ENV = "LOOPER_RUNTIME_ROOT"
STATE_DIRNAME = ".looper"
LEGACY_STATE_DIRNAME = ".loopx"  # the default before the rename; moved to STATE_DIRNAME


def home_runtime_root(home: Path | None = None) -> Path:
    """Return the runtime root Looper uses under ``home`` when none is configured."""

    return (Path.home() if home is None else Path(home)) / STATE_DIRNAME


def legacy_home_runtime_root(home: Path | None = None) -> Path:
    """Return the default runtime root from before the rename, ``~/.loopx``."""

    return (Path.home() if home is None else Path(home)) / LEGACY_STATE_DIRNAME


def configured_runtime_root() -> Path | None:
    """Return the runtime root named by ``LOOPER_RUNTIME_ROOT`` (``LOOPX_RUNTIME_ROOT`` for one release), made absolute.

    An unset or blank value configures nothing. A relative value is resolved
    against the current directory at the moment it is read, lexically, so the
    same value names the same directory for every consumer in the process.
    """

    value = (env.get(RUNTIME_ROOT_ENV) or "").strip()
    if not value:
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def default_runtime_root() -> Path:
    """Return the runtime root used when no explicit root is given.

    ``LOOPER_RUNTIME_ROOT`` when it is set, otherwise ``~/.looper``. It is read at
    call time, never cached at import, so HOME and the environment in effect
    when the root is needed decide it. ``looper serve --runtime-root`` takes
    precedence over this default.
    """

    return configured_runtime_root() or home_runtime_root()


@dataclass(frozen=True)
class SettledRoot:
    """The runtime root to use, and what ``settle_runtime_root`` did or found ("" when nothing to say)."""

    root: Path
    note: str = ""
    warning: bool = False


def settle_runtime_root(root: Path, home: Path | None = None) -> SettledRoot:
    """Move ``~/.loopx`` to ``~/.looper`` when ``root`` is ``~/.looper`` and only ``~/.loopx`` exists.

    The move is one ``rename`` within the home directory, so nothing is copied
    or deleted, and ``~/.loopx`` is left as a symlink to ``.looper`` for one
    release. When both exist, ``~/.looper`` is used and ``~/.loopx`` is left as
    it is, with a warning. When the rename fails, ``~/.loopx`` is used as
    before, with a warning. Any other ``root`` is returned as given.
    """

    new, old = home_runtime_root(home), legacy_home_runtime_root(home)
    if Path(os.path.abspath(root)) != new:
        return SettledRoot(Path(root))
    if os.path.lexists(new):
        if os.path.lexists(old) and os.path.realpath(old) != os.path.realpath(new):
            return SettledRoot(new, f"both {new} and {old} exist: using {new} and leaving {old} as it is; move "
                                    f"what you still need from {old}, then remove it", warning=True)
        return SettledRoot(new)
    if not os.path.isdir(old):
        return SettledRoot(new)
    try:
        os.rename(old, new)
    except OSError as exc:
        if os.path.isdir(new):  # another process moved it meanwhile
            return SettledRoot(new)
        return SettledRoot(old, f"could not move {old} to {new} ({exc}); using {old}", warning=True)
    try:
        os.symlink(STATE_DIRNAME, old)
    except OSError as exc:
        return SettledRoot(new, f"moved {old} to {new}, but could not leave {old} as a link to it ({exc})",
                           warning=True)
    return SettledRoot(new, f"moved the runtime root {old} to {new} (Looper's name for it) and left {old} as a "
                            "link to it until the next release")
