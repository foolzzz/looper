"""One run of a prompt in a process of its own, bounded: a consult's (``consult``) and the processor's
(``processor``).

- **One deadline** covers writing the prompt, reading the reply and waiting for the process to exit.
- **The spend cap** stops the run once its recorder's usage passes the run's cap at its provider's prices
  (``ClaudeRunRecorder.cap_stop``), besides a runtime's own cap in its launch flags; the owner checks the recorded
  cost against its cap afterwards.
- **The process tree** is read every ``TREE_WATCH_SEC`` while the run goes on and again before the result is
  recorded, and ended on every exit, success included; the run's own process group is signalled. A child that calls
  ``setsid`` between two reads and whose parent then exits is not found (a known limit).
- **stderr** is drained, a short tail kept for the record's error, but for a ``quiet`` run (one given other people's
  text, which what it writes may repeat): its record's error is only what happened, and its output only a successful
  result's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, NamedTuple

from . import failures, proc_tree

if TYPE_CHECKING:
    from .agent_settings import Launch
    from .claude_cli import ClaudeRunRecorder

TREE_WATCH_SEC = 0.2  # how often a running process's tree is read
# What is kept of the result of a turn a prompt hook blocked, whose text echoes the prompt: its typed fields only.
BLOCKED_RESULT_FIELDS = ("type", "subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns", "session_id",
                         "total_cost_usd", "usage", "modelUsage", "stop_reason", "uuid")
STDERR_TAIL_CHARS = 2000


class StartFailed(Exception):
    """The run's process could not be started (its record is closed with why)."""


def unsuccessful(result: Mapping[str, Any], *, errors: bool = True) -> str | None:
    """Why a result must not be taken as the run's answer, e.g. a runtime that kept a valid final from a failed
    exit; with ``errors`` False, without the errors' own text."""
    if result.get("is_error"):
        if not errors:
            return "its run ended in error"
        return "its run ended in error: " + "; ".join(str(e) for e in result.get("errors") or []) [:300]
    if result.get("subtype") not in (None, "success"):
        return f"its run ended as {result.get('subtype')}, not success"
    return None


class Finished(NamedTuple):
    result: dict[str, Any] | None  # the run's last result event; None when it gave none
    started: bool  # whether it began a session (Claude Code names it in its init event)
    timed_out: bool  # whether it ran over the deadline
    # Why a Claude Code prompt hook refused the turn (``claude_cli.prompt_block``), when one did: no model turn ran,
    # and its result, whose text echoes the prompt, is recorded and given with its typed fields only.
    blocked: str | None = None


async def run_once(recorder: ClaudeRunRecorder, command: list[str], *, cwd: str, prompt: str, launch: Launch,
                   timeout: float, what: str, label: str, quiet: bool = False,
                   on_process: Callable[[asyncio.subprocess.Process], Awaitable[None]] | None = None) -> Finished:
    """Run ``command`` in ``cwd`` with ``prompt`` as its one user turn, recording it with ``recorder`` (already
    started), which is closed however the run ends; ``what`` names the run in its record's errors (``the consult``),
    ``label`` in the log; ``quiet`` keeps what the process wrote out of the record (the module's notes). Raises
    ``StartFailed`` when the process does not start."""
    runtime = launch.runtime
    try:
        process = await proc_tree.spawn(*command, cwd=cwd, env=launch.environment(),
                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        await recorder.finish(output_text=None, result_event=None, error=f"cannot start {what}: {exc}",
                              failure=failures.start_failure(exc))
        raise StartFailed(str(exc)) from None
    recorder.track_pid(lambda: process.pid)
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    result: dict[str, Any] | None = None
    started = False
    known: proc_tree.Tree = {}
    stderr_tail = ""
    blocked: str | None = None

    async def stderr() -> None:
        nonlocal stderr_tail
        while chunk := await process.stderr.read(8192):
            stderr_tail = (stderr_tail + chunk.decode(errors="replace"))[-STDERR_TAIL_CHARS:]

    draining = asyncio.create_task(stderr())

    async def watch_tree() -> None:
        # A parent that exits re-parents its children and nothing then links them to it, so the tree is kept
        # current while it runs.
        while process.returncode is None:
            known.update(await asyncio.to_thread(proc_tree.tree_of, process))
            await asyncio.sleep(TREE_WATCH_SEC)

    watching = asyncio.create_task(watch_tree())

    async def read() -> None:
        nonlocal result, started, known, blocked
        from .claude_cli import prompt_block

        async for event in runtime.parse_process(process, model=launch.model, launch=launch):
            if (refused := prompt_block(event, cwd)) is not None:
                blocked = refused.reason
            if blocked is not None and event.get("type") == "result":
                event = {key: event[key] for key in BLOCKED_RESULT_FIELDS if key in event}
            if event.get("type") == "result":
                # Read before the record is awaited, which a stop may cut short.
                known.update(await asyncio.to_thread(proc_tree.tree_of, process))
            await recorder.record(event)
            if (capped := recorder.cap_stop) is not None:
                result = capped.result_event
                await proc_tree.end(process, label=label)
                return
            if event.get("type") == "system" and event.get("subtype") == "init":
                started = True
            if event.get("type") == "result":
                result = event
                known.update(await proc_tree.begin_end(process))
                if not process.stdin.is_closing():
                    process.stdin.close()

    async def run() -> None:
        if on_process is not None:
            await on_process(process)
        try:
            await runtime.write_turn(process.stdin, prompt)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        await read()
        await process.wait()

    error: str | None = None
    failure: failures.Failure | None = None
    try:
        await asyncio.wait_for(run(), timeout=timeout)
        if blocked is not None:
            error = f"a Claude Code hook blocked the prompt of {what}: {blocked}"
        elif result is None or result.get("is_error"):
            error = f"{what} gave no result"
    except asyncio.TimeoutError:
        error = f"{what} ran over {timeout:g} s"
        failure = failures.TIMED_OUT
        return Finished(None, started, True)
    except BaseException:
        error = f"{what} was stopped"
        raise
    finally:
        try:
            await proc_tree.end(process, label=label, known=known)
        finally:
            for background in (draining, watching):
                if not background.done():
                    background.cancel()
            await asyncio.gather(draining, watching, return_exceptions=True)
            output = str((result or {}).get("result") or "") or None
            if quiet:
                output = output if result is not None and unsuccessful(result) is None else None
            elif error and stderr_tail:
                error += "\n" + stderr_tail
            await recorder.finish(output_text=output, result_event=result, error=error, failure=failure)
    return Finished(result, started, False, blocked)
