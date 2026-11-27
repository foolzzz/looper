"""``looper-edit`` on a direct task Manager session's PATH (task-sessions RFC, section 25.9 H1).

A direct session's only built-in tool is Bash, so the model changes files with ``looper-edit`` (``looper.file_edit``)
through it. ``edit_bin`` keeps a private directory holding one symlink, ``looper-edit``, to the console script
installed next to the running interpreter, and ``task_manager_safety.environment(..., edit_dir=...)`` puts that
directory first on the CLI's PATH. The directory is in the operator's cache: not under Looper's runtime root (on the
sandbox's read deny list, and the sandbox cannot run a link it cannot read), and not under the task directory or
``$TMPDIR`` (both writable inside the sandbox, so the model could replace the link). A directory under any of
``forbidden`` is refused, as is a missing console script (an installation defect).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path


def _inside(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def edit_bin(environment: Mapping[str, str], *, forbidden: Sequence[str]) -> Path:
    """The private directory holding ``looper-edit``, created (0700) or repaired, from the CLI's ``environment``."""
    home = environment.get("HOME") or str(Path.home())
    directory = Path(environment.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")) / "looper" / "task-manager-bin"
    for root in forbidden:
        if root and _inside(str(directory), root):
            raise RuntimeError(f"the looper-edit directory {directory} is under {root}, which a direct session can "
                               "write or cannot read; set XDG_CACHE_HOME to a directory outside it")
    script = Path(sys.executable).parent / "looper-edit"
    if not os.access(script, os.X_OK):
        raise RuntimeError(f"looper-edit is not installed next to {sys.executable}; reinstall Looper")
    for root in forbidden:  # the link's target too: a script the sandbox can rewrite would run outside the sandbox
        if root and _inside(str(script), root):
            raise RuntimeError(f"the looper-edit console script {script} is under {root}, which a direct session can "
                               "write or cannot read; run Looper from an installation outside it")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    link = directory / "looper-edit"
    if not (link.is_symlink() and os.readlink(link) == str(script)):
        staged = directory / f".looper-edit-{uuid.uuid4().hex}"
        os.symlink(script, staged)
        os.replace(staged, link)  # atomic: a concurrent launch never sees the link missing
    return directory
