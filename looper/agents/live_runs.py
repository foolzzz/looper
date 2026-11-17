"""The agent runs going on now and the tokens they use per minute: the dashboard's process list and its TPM.

Every agent run (a Manager's or a Planner's turn, a Worker's or a Reviewer's run, a consult) is recorded by a
``claude_cli.ClaudeRunRecorder``, and while it runs its token usage reaches only that recorder, as internal usage
events; ``llm_records`` gets a run's tokens when it ends. So a recorder registers its run here while it follows it
(``LiveRun``, ``track``, ``untrack``) and adds the tokens of each usage event to the run's ``TokenWindow`` when
the event comes.

TPM is what a window holds of the last ``WINDOW_SEC`` seconds, not a rate extrapolated from a shorter span, and
counts the four token kinds alike: uncached input, output, prompt read from the cache and prompt written to it.
A run whose runtime reports usage only when a turn ends (``RuntimeAdapter.streams_usage`` false) has no TPM of
its own while it runs; what it reports at the end still counts toward the total. A run that ended stays in the
total until its last tokens leave the window. All of it is in memory: a restart starts every window empty, and the
usage a resumed run replays is not counted again (``ClaudeRunRecorder.account``'s ``replayed``).
"""

from __future__ import annotations

import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .run_usage import Usage

WINDOW_SEC = 60.0

# TokenWindow's kinds -> their Usage field.
_KINDS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_tokens",
          "cache_creation": "cache_creation_tokens"}


@dataclass(eq=False)
class TokenWindow:
    """The tokens of the usage events of the last ``WINDOW_SEC`` seconds, each at the time it was added."""

    clock: Callable[[], float] = time.monotonic
    _events: deque[tuple[float, tuple[int, ...]]] = field(default_factory=deque)

    def add(self, usage: Usage) -> None:
        counts = tuple(getattr(usage, name) or 0 for name in _KINDS.values())
        if any(counts):
            self._events.append((self.clock(), counts))
        self._prune()  # a long run nobody looks at keeps only its last minute

    def _prune(self) -> None:
        cutoff = self.clock() - WINDOW_SEC
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()

    @property
    def empty(self) -> bool:
        self._prune()
        return not self._events

    def rate(self) -> dict[str, int]:
        """The window's tokens by kind and their ``total``."""
        self._prune()
        sums = dict.fromkeys(_KINDS, 0)
        for _, counts in self._events:
            for kind, count in zip(_KINDS, counts):
                sums[kind] += count
        return {"total": sum(sums.values()), **sums}


def total_rate(windows: Iterable[TokenWindow]) -> dict[str, int]:
    """The ``rate`` of several windows added up."""
    total = dict.fromkeys(("total", *_KINDS), 0)
    for window in windows:
        for kind, count in window.rate().items():
            total[kind] += count
    return total


@dataclass(eq=False)
class LiveRun:
    """One agent run while its recorder follows it.

    ``role`` is the run's ``AgentType`` value, or ``consult``; ``runtime`` names its runtime as the dashboard does
    (``RuntimeAdapter.label``); ``model`` is the one its runtime reported once it did, else the one asked for.
    ``pid_of`` reads the pid of the process serving it, when its owner knows one.
    """

    agent_id: str
    role: str
    task_id: str | None
    step_id: str | None
    runtime: str
    model: str | None
    streams_usage: bool
    started_at: datetime
    pid_of: Callable[[], int | None] | None = None
    window: TokenWindow = field(default_factory=TokenWindow)

    @property
    def pid(self) -> int | None:
        if self.pid_of is None:
            return None
        try:
            pid = self.pid_of()
        except Exception:  # the owner's process handle is gone or not started
            return None
        return pid if isinstance(pid, int) and not isinstance(pid, bool) else None

    def tpm(self) -> dict[str, int] | None:
        """The run's own TPM; None when its runtime reports usage only when a turn ends."""
        return self.window.rate() if self.streams_usage else None


# Weak: a recorder that is dropped without closing takes its run out of the list with it.
_live: weakref.WeakSet[LiveRun] = weakref.WeakSet()
# Runs that ended with tokens still in their window, for the total.
_ended: list[LiveRun] = []


def track(run: LiveRun) -> None:
    _live.add(run)


def untrack(run: LiveRun) -> None:
    """The run ended (or another server follows it now); its tokens stay in the total while the window holds them."""
    if run in _live:
        _live.discard(run)
        _drop_spent()  # without a reader too, ended runs do not pile up
        if not run.window.empty:
            _ended.append(run)


def _drop_spent() -> None:
    _ended[:] = [run for run in _ended if not run.window.empty]


def running() -> list[LiveRun]:
    """The runs recorders follow now, the longest-running first."""
    return sorted(_live, key=lambda run: run.started_at)


def counted() -> list[LiveRun]:
    """Every run whose tokens are in a window now: the running ones and those that ended within it."""
    _drop_spent()
    return [*running(), *_ended]
