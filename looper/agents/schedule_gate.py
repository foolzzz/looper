"""A schedule's gate command: run before each run decides whether the run is worth a task (scheduled-tasks RFC §6).

The command runs with ``/bin/sh -c`` in the run's work directory, in a session
of its own (``proc_tree.spawn``, so a server that crashes meanwhile has it swept
at its next start), with the server's environment and no stdin. It has
``timeout_seconds``; past it, or when the gate is cancelled, its whole process
tree is ended the way an agent's is (``proc_tree.end``: SIGTERM, then SIGKILL
after ``proc_tree.TERM_GRACE_SEC``). What it leaves running in its own group
after it exits is killed. Its stdout is kept up to ``STDOUT_MAX_CHARS``
and the tail of its stderr up to ``STDERR_TAIL_CHARS``; the rest is read and
dropped, so a chatty gate cannot fill the server's memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass

from . import proc_tree

STDOUT_MAX_CHARS = 8000
STDERR_TAIL_CHARS = 2000
# How long the pipes may stay open after the command exited (something it started in the background holds them).
_DRAIN_SECONDS = 2.0
_CHUNK = 65536
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class GateResult:
    exit_code: int | None  # None: it did not exit by itself (timed out) or never started
    stdout: str
    stderr_tail: str
    timed_out: bool = False
    stdout_clipped: bool = False
    error: str | None = None  # it could not be started

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    def failure(self, timeout_seconds: float) -> str:
        """Why the gate failed, with the tail of its stderr."""
        if self.error:
            reason = self.error
        elif self.timed_out:
            reason = f"the gate did not finish within {timeout_seconds:g} s and was stopped"
        else:
            reason = f"the gate exited with status {self.exit_code}"
        tail = self.stderr_tail.strip()
        return f"{reason}; stderr: {tail}" if tail else reason


class _Capture:
    """What a pipe carried: its first (or last, with ``tail``) ``keep`` bytes, and whether more came."""

    def __init__(self, keep: int, *, tail: bool) -> None:
        self.data = bytearray()
        self.more = False
        self._keep = keep
        self._tail = tail

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(_CHUNK):
            self.data += chunk
            if len(self.data) > self._keep:
                self.more = True
                self.data = self.data[-self._keep:] if self._tail else self.data[:self._keep]

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _exited(process: asyncio.subprocess.Process, seconds: float) -> bool:
    """Whether the gate's own process exits within ``seconds``. ``process.wait()`` would also wait for its pipes,
    which something it started in the background can hold open."""
    deadline = asyncio.get_running_loop().time() + seconds
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
    return process.returncode is not None


async def _end_tree(process: asyncio.subprocess.Process, label: str) -> None:
    """End the gate's process tree; a process that left it may hold its pipes, which ``end`` would wait for."""
    try:
        await asyncio.wait_for(proc_tree.end(process, label=label),
                               timeout=proc_tree.TERM_GRACE_SEC + _DRAIN_SECONDS)
    except asyncio.TimeoutError:
        pass  # every signal was sent; what holds the pipes left the tree


async def run_gate(command: str, work_dir: str, timeout_seconds: float, *, label: str = "gate") -> GateResult:
    """Run the gate and say what it printed and how it ended. Cancelling it ends the gate's process tree."""
    try:
        process = await proc_tree.spawn(
            "/bin/sh", "-c", command, cwd=work_dir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return GateResult(None, "", "", error=f"the gate could not start in {work_dir}: {exc}")
    assert process.stdout is not None and process.stderr is not None
    # Characters are at most 4 bytes; the text is clipped to characters below.
    out, err = _Capture(STDOUT_MAX_CHARS * 4, tail=False), _Capture(STDERR_TAIL_CHARS * 4, tail=True)
    stdout, stderr = asyncio.ensure_future(out.read(process.stdout)), asyncio.ensure_future(err.read(process.stderr))
    timed_out = False
    try:
        if await _exited(process, timeout_seconds):
            _signal_group(process.pid, signal.SIGKILL)  # what it left running in its group
        else:
            timed_out = True
            await _end_tree(process, label)
        try:
            await asyncio.wait_for(asyncio.gather(stdout, stderr), timeout=_DRAIN_SECONDS)
        except asyncio.TimeoutError:
            pass  # a process that left the group holds the pipes; what was read is kept
    except asyncio.CancelledError:
        await asyncio.shield(_end_tree(process, label))
        raise
    finally:
        for reader in (stdout, stderr):
            if not reader.done():
                reader.cancel()
    text = out.text()
    return GateResult(
        exit_code=None if timed_out else process.returncode,
        stdout=text[:STDOUT_MAX_CHARS], stderr_tail=err.text()[-STDERR_TAIL_CHARS:],
        timed_out=timed_out, stdout_clipped=out.more or len(text) > STDOUT_MAX_CHARS,
    )
