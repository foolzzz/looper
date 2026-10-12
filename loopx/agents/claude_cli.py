"""Helpers for running the Claude Code CLI as a subprocess and recording its runs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
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

    The record opens as ``running`` with the launch command and working
    directory, gets every stream-json event as it arrives (partial text
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
    ) -> None:
        self._agent_id = agent_id
        self._task_id = task_id or None
        self._command = command
        self._cwd = cwd
        self._input_text = input_text
        self._session_id = session_id
        self._step_id = step_id
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
                    "cwd": self._cwd,
                    # The task step a Worker or Reviewer ran, so its run stays tied to that step.
                    **({"step_id": self._step_id} if self._step_id else {}),
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
    ) -> None:
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
                cost_usd=result.get("total_cost_usd"),
                duration_ms=int((_clock() - self._started) * 1000),
                model=self._model,
                error=error or (None if succeeded else f"Claude run ended: {result.get('subtype') or 'no result'}"),
            )
        except Exception:
            logger.warning("could not close llm record for %s", self._agent_id, exc_info=True)
