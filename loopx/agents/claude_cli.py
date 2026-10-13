"""Helpers for running the Claude Code CLI as a subprocess and recording its runs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_READ_CHUNK = 64 * 1024


async def iter_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield the stream's lines, each with its newline, with no length limit.

    ``StreamReader.readline()`` fails on lines longer than the reader's limit,
    and Claude's stream-json lines (the tool and skill list, large tool
    results) have no fixed bound. A final line without a newline is yielded
    as is.
    """
    pending = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        search_from = len(pending)  # earlier bytes were already searched
        pending += chunk
        start = 0
        while (end := pending.find(b"\n", search_from)) != -1:
            yield bytes(pending[start:end + 1])
            start = search_from = end + 1
        del pending[:start]
    if pending:
        yield bytes(pending)


async def write_user_turn(stdin: asyncio.StreamWriter, text: str) -> None:
    """One user message in Claude's stream-json input format."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)
    stdin.write(line.encode("utf-8") + b"\n")
    await stdin.drain()


# How long a stopping resident Claude may take to exit after its stdin closes before it is killed.
STOP_GRACE_SEC = 5.0


@dataclass(frozen=True)
class ResidentTurn:
    """How one turn on a resident Claude ended.

    ``result_event`` is the turn's ``result``, or None when Claude exited
    before writing it; then ``exit_code`` is the reaped process's exit code.
    """

    result_event: dict[str, Any] | None
    exit_code: int | None = None


class ResidentClaude:
    """One long-lived ``claude --input-format stream-json`` process, fed one user turn at a time.

    The owner builds the command (flags, system prompt, ``--resume``) and
    serializes turns. Each turn's events are read up to its ``result``; the
    stdout line iterator stays open for the next turn. A process that exited,
    or whose ``signature`` (what it was started with) no longer matches, is
    stale: the owner stops it and starts a new one, which resumes the session.
    """

    def __init__(self, *, label: str) -> None:
        self._label = label  # names the owner in logs
        self._process: asyncio.subprocess.Process | None = None
        self._lines: AsyncIterator[bytes] | None = None
        self._command: list[str] = []
        self._signature: Any = None
        self._cost_usd = 0.0  # the process's running total at its last result

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def command(self) -> list[str]:
        """The running process's command; empty when none runs."""
        return self._command

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def stale(self, signature: Any) -> bool:
        """True when a process was started but has exited or was started with another ``signature``."""
        process = self._process
        return process is not None and (process.returncode is not None or self._signature != signature)

    async def start(self, command: list[str], *, cwd: str, signature: Any) -> None:
        """Start the process; raises ``OSError`` when it cannot run."""
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        self._process, self._command, self._signature = process, command, signature
        self._cost_usd = 0.0
        self._lines = iter_lines(process.stdout)
        logger.info("%s started Claude Code (pid=%s)", self._label, process.pid)

    async def run_turn(
        self,
        text: str,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> ResidentTurn:
        """Write ``text`` as one user turn and hand each event to ``on_event`` up to the turn's result.

        A Claude that exits before its result (its stdout ends, or it closed
        stdin) is reaped, so the next turn starts a new process. Any exception,
        cancellation included, kills and reaps the process before it
        propagates: a turn that was given up must not leave Claude running.
        """
        process, lines = self._process, self._lines
        assert process is not None and process.stdin is not None and lines is not None
        input_lost = False
        result_event: dict[str, Any] | None = None
        try:
            try:
                await write_user_turn(process.stdin, text)
            except (BrokenPipeError, ConnectionResetError):
                # Claude closed its stdin, so it is exiting; what it already wrote decides the turn.
                logger.warning("%s: Claude closed stdin before the turn was sent", self._label)
                input_lost = True
                if process.returncode is None:
                    process.kill()
            # This turn's events end at its result; the iterator stays open for the next turn.
            async for raw_line in lines:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                await on_event(event)
                if event.get("type") == "result":
                    result_event = event
                    break
        except BaseException:
            await self.stop(kill=True)
            raise
        if result_event is None or input_lost:
            await self.stop()
        return ResidentTurn(result_event, None if result_event is not None else process.returncode)

    def turn_cost(self, result_event: dict[str, Any] | None) -> float | None:
        """This turn's cost: Claude's ``total_cost_usd`` is its process's running total."""
        total = (result_event or {}).get("total_cost_usd")
        if not isinstance(total, (int, float)):
            return None
        cost, self._cost_usd = total - self._cost_usd, total
        return cost

    async def stop(self, *, kill: bool = False) -> None:
        """End the process, if any, and forget it.

        Its stdin is closed, which ends a healthy Claude; one still running
        after ``STOP_GRACE_SEC`` (at once with ``kill``) is killed. The
        process is always reaped.
        """
        process = self._process
        self._process, self._lines, self._command, self._signature = None, None, [], None
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            if process.returncode is None and not kill:
                try:
                    await asyncio.wait_for(process.wait(), STOP_GRACE_SEC)
                except TimeoutError:
                    pass
        finally:
            # Also when the wait is cancelled: the process is forgotten already.
            if process.returncode is None:
                process.kill()
            await process.wait()
        logger.info("%s stopped Claude Code (pid=%s, exit code %s)", self._label, process.pid, process.returncode)


# How often a running record's heartbeat (last_output_at) is written.
_HEARTBEAT_INTERVAL = 10.0


def _clock() -> float:
    return time.monotonic()

# Fields of Claude's init event that list every tool, skill or server; kept as
# counts so a transcript stays readable.
_INIT_LIST_LIMIT = 2_000


def _is_thinking_progress(event: dict[str, Any]) -> bool:
    """Claude emits one of these about every second while it thinks."""
    return event.get("type") == "system" and event.get("subtype") == "thinking_tokens"


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "system" or event.get("subtype") != "init":
        return event
    compact: dict[str, Any] = {}
    for key, value in event.items():
        if isinstance(value, list) and len(json.dumps(value)) > _INIT_LIST_LIMIT:
            compact[key] = f"<{len(value)} items>"
        else:
            compact[key] = value
    return compact


class ClaudeRunRecorder:
    """Record one Claude CLI run in ``llm_records``.

    The record opens as ``running`` with the launch command, the system
    prompt LoopX appended (also inside the command, kept on its own for
    display) and the working directory, gets every stream-json event as it
    arrives (partial text
    deltas excepted: the full message follows), and closes with the outcome,
    usage and cost. Recording is observability: a database error is logged
    and never fails the run.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        task_id: str | None,
        command: list[str],
        cwd: str,
        input_text: str,
        session_id: str | None = None,
        step_id: str | None = None,
        pid: int | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._task_id = task_id or None
        self._command = command
        self._cwd = cwd
        self._input_text = input_text
        self._session_id = session_id
        self._step_id = step_id
        self._pid = pid
        self._system_prompt = system_prompt
        self._record_id: int | None = None
        self._seq = 0
        # seq of the row holding the current thinking phase's progress, if it is the latest row
        self._thinking_seq: int | None = None
        self._model: str | None = None
        self._started = _clock()
        self._last_heartbeat = float("-inf")

    async def start(self) -> None:
        from ..db import llm_records

        try:
            self._record_id = await llm_records.start_record(
                agent_id=self._agent_id,
                task_id=self._task_id,
                session_id=self._session_id,
                input_text=self._input_text,
                metadata={
                    "command": self._command,
                    # What LoopX passed with --append-system-prompt; Claude Code's own prompt is not included.
                    **({"system_prompt": self._system_prompt} if self._system_prompt is not None else {}),
                    "cwd": self._cwd,
                    # The task step a Worker or Reviewer ran, so its run stays tied to that step.
                    **({"step_id": self._step_id} if self._step_id else {}),
                    # The resident process that served this turn, for a long-lived agent.
                    **({"pid": self._pid} if self._pid is not None else {}),
                },
            )
        except Exception:
            logger.warning("could not open llm record for %s", self._agent_id, exc_info=True)

    async def record(self, event: dict[str, Any]) -> None:
        await self._heartbeat()
        if event.get("type") == "stream_event":
            return
        if event.get("type") == "system" and event.get("subtype") == "init":
            self._model = event.get("model") or self._model
            self._session_id = event.get("session_id") or self._session_id
        if self._record_id is None:
            return
        from ..db import llm_records

        compact = _compact_event(event)
        thinking = _is_thinking_progress(event)
        try:
            if thinking and self._thinking_seq is not None and self._thinking_seq == self._seq:
                # One row per thinking phase: keep its latest token estimate.
                await llm_records.update_event(self._record_id, self._seq, compact)
                return
            self._seq += 1
            self._thinking_seq = self._seq if thinking else None
            await llm_records.append_events(self._record_id, [(self._seq, compact)])
        except Exception:
            logger.warning("could not record event for %s", self._agent_id, exc_info=True)

    async def _heartbeat(self) -> None:
        """Any output, partial deltas included, shows the run is not stuck."""
        now = _clock()
        if self._record_id is None or now - self._last_heartbeat < _HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        from ..db import llm_records

        try:
            await llm_records.touch_record(self._record_id)
        except Exception:
            logger.warning("could not record heartbeat for %s", self._agent_id, exc_info=True)

    async def finish(
        self,
        *,
        output_text: str | None,
        result_event: dict[str, Any] | None,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Close the record; ``cost_usd`` replaces the result's ``total_cost_usd``.

        Claude reports the running total of its process, so a run that is one
        turn of a longer-lived process passes its own share.
        """
        if self._record_id is None:
            return
        from ..db import llm_records

        result = result_event or {}
        succeeded = error is None and result.get("subtype") == "success" and not result.get("is_error")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        try:
            await llm_records.finish_record(
                self._record_id,
                status="completed" if succeeded else "failed",
                output_text=output_text,
                session_id=result.get("session_id") or self._session_id,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cost_usd=result.get("total_cost_usd") if cost_usd is None else cost_usd,
                duration_ms=int((_clock() - self._started) * 1000),
                model=self._model,
                error=error or (None if succeeded else f"Claude run ended: {result.get('subtype') or 'no result'}"),
            )
        except Exception:
            logger.warning("could not close llm record for %s", self._agent_id, exc_info=True)
