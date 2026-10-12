"""Helpers for running the Claude Code CLI as a subprocess."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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
