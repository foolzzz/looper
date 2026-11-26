"""The Manager's resident state as one stable block: whole, unchanged, or the changes since the last whole one.

Every user turn of a Manager starts with the same sections (projects, task templates, workflows, ready tasks, open
gates, active goals). A resident session's history only grows, so each copy is cached once and read back on every
later turn. With ``manager.stable_context`` on they are one block, "Looper state (rev R)", sent whole only when the
session may not hold the last one; any other turn says it is unchanged, or lists the lines added and removed since
that whole block (never since the previous note). R names the block's content, so a session resumed after a server
restart never holds two different blocks of one rev.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Any

STABLE_CONTEXT_KEY = "manager.stable_context"
# A new whole block instead of a change note when more than this percentage of the last whole block's non-blank
# lines changed (each line added or removed counts once, so a replaced line twice), or the note would be longer than
# this percentage of the new whole block (characters of the joined lines: the note with its heading line, the block
# without its rev line). Exactly at either limit it is still a note.
CHANGED_LINES_MAX_PERCENT = 30
NOTE_CHARS_MAX_PERCENT = 40

# The end of the Manager's contract while the switch is on, after every other part of it.
STABLE_CONTEXT_CONTRACT = """\
## Looper state
The sections a user message starts with ("Projects" and the rest) come as one
block, "Looper state (rev R)", which Looper sends whole only now and then;
other messages start with "Looper state: unchanged since rev R" or
"Looper state: changes since rev R". The latest whole "Looper state" block
with the newest change note after it is the current state; earlier ones are
stale.
"""


async def enabled(db: Any = None) -> bool:
    """Whether ``manager.stable_context`` is on (``true``; no row is off)."""
    from ...db import system_settings

    return (await system_settings.get_settings([STABLE_CONTEXT_KEY], pool=db)).get(STABLE_CONTEXT_KEY) == "true"


class StateView(str, Enum):
    """What a user turn starts with."""

    WHOLE = "whole"
    UNCHANGED = "unchanged"
    CHANGES = "changes"


class WholeReason(str, Enum):
    """A pending reason to restore the session's whole state block."""

    OTHER_AUDIENCE = "another audience's block came after the last shown state"


@dataclass(frozen=True)
class StateShown:
    """The last whole block a session got: its rev, its lines and that session."""

    rev: str
    lines: tuple[str, ...]
    session: str | None


@dataclass(frozen=True)
class Rendered:
    """A turn's start (``lines``, ending with a blank line) and the whole block it leaves the session with."""

    view: StateView
    rev: str
    reason: str
    lines: list[str]
    shown: StateShown

    def annotation(self) -> dict[str, Any]:
        return {"looper_state": {"view": self.view.value, "rev": self.rev, "reason": self.reason}}


def rev_of(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:8]


def render(current: Sequence[str], shown: StateShown | None, *, session: str | None,
           whole_reason: str | None) -> Rendered:
    """What a turn starts with, given the sections as they read now (``current``, each ending with a blank line) and
    the last whole block the session holds (``shown``; None: none it can refer to). ``whole_reason`` says why the
    session may not hold ``shown`` (a new session or process, or a runtime that does not report compaction)."""
    lines = tuple(current)
    if whole_reason is None and shown is None:
        whole_reason = "no whole block yet"
    if whole_reason is None and shown is not None:
        if lines == shown.lines:
            return Rendered(StateView.UNCHANGED, shown.rev, "unchanged",
                            [f"Looper state: unchanged since rev {shown.rev}", ""], shown)
        note, changed = _changes(shown, lines)
        if 100 * changed > CHANGED_LINES_MAX_PERCENT * sum(1 for line in shown.lines if line.strip()):
            whole_reason = "many lines changed"
        elif 100 * len("\n".join(note)) > NOTE_CHARS_MAX_PERCENT * len("\n".join(lines)):
            whole_reason = "long change note"
        else:
            return Rendered(StateView.CHANGES, shown.rev, "changes", [*note, ""], shown)
    rev = rev_of(lines)
    return Rendered(StateView.WHOLE, rev, whole_reason or "", [f"Looper state (rev {rev}):", *lines],
                    StateShown(rev, lines, session))


def _changes(shown: StateShown, current: Sequence[str]) -> tuple[list[str], int]:
    """The note of what changed since ``shown``, as a diff: each changed line under the heading of its section
    (unprefixed by +/-, with a space, when the heading itself did not change); and how many lines changed."""
    old = shown.lines
    note = [f"Looper state: changes since rev {shown.rev} (+ added, - removed):"]
    changed, heading_shown = 0, None
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, current, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for lines, start, end, sign in ((old, i1, i2, "-"), (current, j1, j2, "+")):
            for index in range(start, end):
                line = lines[index]
                if not line.strip():
                    continue
                heading = _heading(lines, index)
                if heading is None:  # a heading itself (or a line outside any section)
                    heading_shown = line
                elif heading != heading_shown:
                    note.append(f" {heading}")
                    heading_shown = heading
                note.append(f"{sign}{line}")
                changed += 1
    return note, changed


def _heading(lines: Sequence[str], index: int) -> str | None:
    """The heading of ``lines[index]``'s section when that line is an item under it (indented); else None."""
    if not lines[index].startswith(" "):
        return None
    for back in range(index - 1, -1, -1):
        if not lines[back].strip():
            return None
        if not lines[back].startswith(" "):
            return lines[back]
    return None
