"""A runtime's own login, shared with Looper's isolated homes by symbolic links (multi-runtime RFC P1).

A runtime that keeps its login in a file of its home (Codex's and Grok's
``auth.json``) runs with a home of Looper's own. Each such home holds a
symbolic link to the operator's login file, by its absolute path, never a copy
and never a hard link (``link``): a refresh the runtime writes through the
link reaches the operator's file, and every process reads the same tokens.

Should a save ever replace the link with a file of its own, that file is a copy
of the tokens and the operator's file may hold a refresh token that no longer
works. After every login run, and when ``looper serve`` starts, the homes are
inspected (``inspect_homes``): a copy, or any other entry in place of the link,
is removed (unlinked, never followed) and the link put back, and the problem is
recorded under the login's key (``record_problems``), which makes the login
unavailable to runs until the operator has checked it and cleared it
(``clear_problem``). The runtimes' own modules (``codex_login``,
``grok_login``) say which file, which key and what the operator should do.
"""

from __future__ import annotations

import enum
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LinkState(str, enum.Enum):
    """What a home's login entry is."""

    INTACT = "intact"  # a symbolic link to the operator's login file
    MISSING = "missing"
    COPY = "copy"  # a file of its own: a copy of the tokens, the login forked
    OTHER = "other"  # a link to something else, or not a file


class LoginLinkError(RuntimeError):
    """A home's login entry is not the link to the operator's login file."""


def link_state(home: Path, name: str, target: Path) -> LinkState:
    """What ``home``'s entry ``name`` is, read with ``lstat`` (a link is never followed)."""
    path = Path(home) / name
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return LinkState.MISSING
    if stat.S_ISLNK(mode):
        return LinkState.INTACT if os.readlink(path) == str(target) else LinkState.OTHER
    return LinkState.COPY if stat.S_ISREG(mode) else LinkState.OTHER


def link(home: Path, name: str, target: Path, *, login: str, fix: str = "") -> None:
    """Give ``home`` an entry ``name`` that is a symbolic link to ``target``; raises ``LoginLinkError`` when
    something else is there, which the next inspection removes (``inspect_homes``). ``login`` names the login
    ("the Codex login") and ``fix`` says what the operator should do."""
    for _ in range(2):
        state = link_state(home, name, target)
        if state is LinkState.INTACT:
            return
        if state is not LinkState.MISSING:
            raise LoginLinkError(f"{Path(home) / name} is not the link to {login} ({state.value})"
                                 + (f": {fix}" if fix else ""))
        try:
            os.symlink(target, Path(home) / name)
            return
        except FileExistsError:
            continue  # another run linked it meanwhile
    raise LoginLinkError(f"cannot link {login} into {home}")


def unlink(home: Path, name: str) -> None:
    """Remove ``home``'s link ``name``, if it has one (a session home whose agent moved to another provider)."""
    path = Path(home) / name
    if path.is_symlink():
        path.unlink()


def remove_entry(path: Path) -> None:
    """Remove ``path`` itself: a link or a file is unlinked, a directory removed; no link is followed."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LinkProblem:
    """A home whose login entry was not the link to the login when it was inspected. ``login`` names the login
    ("Codex login"); ``removed`` says what removes the link ("as `codex logout` there would")."""

    home: Path
    state: LinkState
    name: str
    login: str
    removed: str

    def text(self) -> str:
        if self.state is LinkState.COPY:
            return (f"{self.name} in {self.home} had become a copy of the {self.login} (the link was replaced), so "
                    "the login on this machine may hold a refresh token that no longer works; the copy was deleted")
        if self.state is LinkState.MISSING:
            return f"the {self.login} link in {self.home} was removed ({self.removed})"
        return f"{self.name} in {self.home} was not the link to the {self.login}; it was replaced by the link"


def inspect_homes(parents: Iterable[Path], links: Mapping[str, Path], *, is_login_home: Callable[[Path], bool],
                  login: str, removed: str) -> list[LinkProblem]:
    """Every home in ``parents`` whose entries ``links`` (name to target) are not the links to their targets: a
    copy or another entry in place of one, or a login home (``is_login_home``) whose link is gone. Each is put
    right (the entry removed, the link put back), so it is reported once. A home of another provider without the
    links is fine."""
    problems: list[LinkProblem] = []
    for parent in parents:
        for home in sorted(parent.iterdir()) if parent.is_dir() else []:
            if home.name.startswith(".") or home.is_symlink() or not home.is_dir():
                continue  # a home being built, or not one of Looper's
            for name, target in links.items():
                try:
                    state = link_state(home, name, target)
                    if state is LinkState.INTACT or (state is LinkState.MISSING and not is_login_home(home)):
                        continue
                    if state is not LinkState.MISSING:
                        remove_entry(home / name)
                    link(home, name, target, login=f"the {login}")
                except FileNotFoundError:
                    continue  # an idle home removed meanwhile
                problems.append(LinkProblem(home, state, name, login, removed))
    return problems


# --- The recorded problem: system_settings[key] = {"detail", "at"} ---


async def recorded_problem(key: str, *, pool: Any = None) -> dict[str, str] | None:
    """The problem recorded under ``key``, ``{"detail", "at"}``; None when there is none."""
    from ...db import system_settings

    raw = (await system_settings.get_settings([key], pool=pool)).get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    return value if isinstance(value, dict) else {"detail": "the recorded problem cannot be read", "at": ""}


async def record_problems(key: str, problems: list[LinkProblem]) -> str:
    """Record ``problems`` under ``key`` (the login is unavailable to runs from now on); their text."""
    from ...db import system_settings

    detail = "; ".join(problem.text() for problem in problems)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await system_settings.put_settings({key: json.dumps({"detail": detail, "at": at}, ensure_ascii=False)})
    return detail


async def clear_problem(key: str) -> None:
    from ...db import system_settings

    await system_settings.put_settings({key: None})
