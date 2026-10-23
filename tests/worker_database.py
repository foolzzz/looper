"""One PostgreSQL database per xdist worker.

Every database test builds a schema of its own, and ``ensure_schema`` holds one lock for the whole database while it
does, so workers that share a database queue behind each other. Each worker therefore gets a database of its own,
derived from ``LOOPER_TEST_DATABASE_URL`` (``<name>_<worker>``): created when the worker starts, the variable
rewritten for everything it runs or starts, dropped when the session ends. Without xdist nothing changes.

A database carries the host and pid of the worker that made it as its comment. A crashed run leaves its databases
behind; the next run's workers drop the ones of the same name pattern whose owner is gone, and never one that is
not marked as a worker's database or whose owner still runs.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
from collections.abc import Coroutine
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import asyncpg

ENV = "LOOPER_TEST_DATABASE_URL"
_MARK = "looper test worker database"
_NAME_LIMIT = 63  # what PostgreSQL keeps of an identifier

_T = TypeVar("_T")
_own: tuple[str, str] | None = None  # (admin url, name) of the database this worker made


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` on a loop of its own, leaving the thread's current event loop as it was."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _owner() -> str:
    return f"{_MARK}: host={socket.gethostname()} pid={os.getpid()}"


def _is_gone(comment: str | None) -> bool:
    """Whether the worker named by a database's comment has ended: it is marked, from this host, and its pid is
    not running."""
    match = re.fullmatch(rf"{_MARK}: host=(.*) pid=(\d+)", comment or "")
    if match is None or match.group(1) != socket.gethostname():
        return False
    try:
        os.kill(int(match.group(2)), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _name(base: str, worker: str, suffix: str = "") -> str:
    tail = f"_{worker}{suffix}"
    return base.encode()[:_NAME_LIMIT - len(tail)].decode(errors="ignore") + tail


async def _create(url: str, base: str, worker: str) -> str:
    connection = await asyncpg.connect(url, timeout=10, command_timeout=30)
    try:
        # CREATE/DROP DATABASE cannot run in a transaction. A session lock serializes only this short setup,
        # including stale cleanup, so two starting workers cannot drop the same leftover database concurrently.
        await connection.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", base)
        for row in await connection.fetch(
                "SELECT datname, shobj_description(oid, 'pg_database') AS comment FROM pg_database"):
            match = re.fullmatch(r".+_(gw\d+)(_\d+)?", row["datname"])
            if (match and row["datname"] == _name(base, match[1], match[2] or "")
                    and _is_gone(row["comment"])):
                await connection.execute(f"DROP DATABASE IF EXISTS {_quote(row['datname'])} WITH (FORCE)")
        taken = {row["datname"] for row in await connection.fetch("SELECT datname FROM pg_database")}
        name = _name(base, worker)
        if name in taken:  # a run still going has this worker's name (the same base database, run twice at once)
            name = _name(base, worker, f"_{os.getpid()}")
        await connection.execute(f"CREATE DATABASE {_quote(name)}")
        try:
            owner = _owner().replace("'", "''")
            await connection.execute(f"COMMENT ON DATABASE {_quote(name)} IS '{owner}'")
        except BaseException:
            await connection.execute(f"DROP DATABASE {_quote(name)} WITH (FORCE)")
            raise
        return name
    finally:
        await connection.close()


async def _drop(url: str, name: str) -> None:
    connection = await asyncpg.connect(url, timeout=10, command_timeout=30)
    try:
        await connection.execute(f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)")
    finally:
        await connection.close()


def start(worker: str | None) -> None:
    """For an xdist worker (``worker`` is its id): make its database and point ``LOOPER_TEST_DATABASE_URL`` at it."""
    global _own
    url = os.environ.get(ENV)
    if not url or not worker or _own is not None:
        return
    parts = urlsplit(url)
    base = unquote(parts.path.lstrip("/"))
    if not base:
        raise ValueError(f"{ENV} must name a disposable database")
    name = _run(_create(url, base, worker))
    _own = (url, name)
    os.environ[ENV] = urlunsplit(parts._replace(path=f"/{quote(name, safe='')}"))


def finish() -> None:
    """Drop the database ``start`` made, and put ``LOOPER_TEST_DATABASE_URL`` back."""
    global _own
    if _own is None:
        return
    url, name = _own
    _own = None
    os.environ[ENV] = url
    _run(_drop(url, name))
