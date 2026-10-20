"""Portable launch capture for legacy and current checkouts; no production imports.

``capture(root, exercise, inspect_launch=..., readback=...)`` runs an async
driver through its real tiers/adapters. Reuse ``agent_launch_parity.capture``
or the runtime tests' existing fake CLIs; only executables below ``root`` are
observed (also behind an ``env NAME=value ...`` prefix). Other subprocesses
are left alone. The driver owns disposable persistence and synthetic auth.

``inspect_launch(argv, env, cwd)`` reads config bytes, prompt, permissions,
isolation, MCP and login-link targets *at launch*. ``readback(index)`` reads
session, totals, price and cap *after the exercise*. Both must return all
their fields, using explicit None for inapplicable facts. No runtime-specific
defaults or expected values are invented here. The returned list is JSON
data, suitable for freezing on an old checkout and comparing on a new one.
Drivers must seed deterministic IDs and control their inherited environment.

Only the supplied temporary root and explicitly requested session timestamps
can be normalized. Config text, URL spelling, model/provider names, ordering,
explicit nulls, environment removals, prices and caps remain significant.
Existing main fixtures are never read or refreshed by this module.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import math
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

LAUNCH_FIELDS = frozenset({"unset", "config", "prompt", "permissions", "isolation", "mcp", "login_target"})
STATE_FIELDS = frozenset({"session", "totals", "price", "cap"})
FIELDS = frozenset({"argv", "env", "unset", "first_input"}) | LAUNCH_FIELDS | STATE_FIELDS
TIME_PATHS = frozenset({("session", "created_at"), ("session", "updated_at")})


def _fields(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    if not isinstance(value, dict) or value.keys() != expected:
        actual = set(value) if isinstance(value, dict) else set()
        raise ValueError(f"capture fields: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _json_copy(value: Any) -> Any:
    """Reject implicit coercion (tuples, Paths, callables, non-string keys, NaN)."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json_copy(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _json_copy(item) for key, item in value.items()}
    raise ValueError(f"capture requires JSON data, got {type(value).__name__}")


def normalize(record: dict[str, Any], *, root: Path,
              time_paths: Iterable[tuple[str, ...]] = ()) -> dict[str, Any]:
    """Return a detached contract, normalizing only root paths and opted-in times."""
    _fields(record, FIELDS)
    result = _json_copy(record)
    for name in ("argv", "unset"):
        if not isinstance(result[name], list) or any(type(item) is not str for item in result[name]):
            raise ValueError(f"{name} must be a list of strings")
    if not result["argv"]:
        raise ValueError("argv must be non-empty")
    if not isinstance(result["env"], dict) or any(type(v) is not str for v in result["env"].values()):
        raise ValueError("env must map names to strings")
    if len(set(result["unset"])) != len(result["unset"]):
        raise ValueError("unset names must be unique")
    for name in ("prompt", "first_input"):
        if type(result[name]) is not str:
            raise ValueError(f"{name} must be a string")
    paths = tuple(time_paths)
    if any(type(path) is not tuple or path not in TIME_PATHS for path in paths) or len(set(paths)) != len(paths):
        raise ValueError(f"time_paths must be unique entries from {sorted(TIME_PATHS)}")
    for path in paths:
        parent = result
        for part in path[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise ValueError(f"missing timestamp at {path}")
            parent = parent[part]
        if not isinstance(parent, dict) or path[-1] not in parent:
            raise ValueError(f"missing timestamp at {path}")
        value = parent[path[-1]]
        try:
            if not isinstance(value, str) or "T" not in value or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError(f"invalid timestamp at {path}") from None
        parent[path[-1]] = "<time>"
    root_text = str(root)
    if not root.is_absolute() or root_text == "/" or ".." in root.parts:
        raise ValueError("root must be an absolute temporary directory, not /")
    # A root prefix in a longer filename is not a path under this root.
    pattern = re.compile(re.escape(root_text) + r"(?=/|$|[\s\"'\\,;:)\]}])")

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub("<root>", value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            replaced = {pattern.sub("<root>", key): replace(item) for key, item in value.items()}
            if len(replaced) != len(value):
                raise ValueError("root normalization would collide dictionary keys")
            return replaced
        return value

    return replace(result)


def assert_parity(expected: list[dict[str, Any]], actual: list[dict[str, Any]], *,
                  expected_root: Path, actual_root: Path,
                  time_paths: Iterable[tuple[str, ...]] = ()) -> None:
    """Compare every launch, with a field-level diff. Empty baselines are errors."""
    if not expected or not actual:
        raise ValueError("launch captures must be non-empty")
    if len(expected) != len(actual):
        raise AssertionError(f"launch count: expected {len(expected)}, actual {len(actual)}")
    paths = tuple(time_paths)
    for index, (before, after) in enumerate(zip(expected, actual)):
        left = json.dumps(normalize(before, root=expected_root, time_paths=paths),
                          ensure_ascii=False, sort_keys=True, indent=2)
        right = json.dumps(normalize(after, root=actual_root, time_paths=paths),
                           ensure_ascii=False, sort_keys=True, indent=2)
        if left != right:
            diff = "\n".join(difflib.unified_diff(left.splitlines(), right.splitlines(),
                                                fromfile="legacy", tofile="current", lineterm=""))
            raise AssertionError(f"launch {index} differs:\n{diff}")


class _Input:
    def __init__(self, stream: Any, record: dict[str, Any]) -> None:
        self._stream, self._record = stream, record
        self._seen = False

    def observe(self, data: bytes) -> None:
        if data and not self._seen:
            self._record["first_input"] = data.decode("utf-8")
            self._seen = True

    def write(self, data: bytes) -> None:
        self.observe(data)
        self._stream.write(data)

    def writelines(self, lines: Iterable[bytes]) -> None:
        self.write(b"".join(lines))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _Process:
    def __init__(self, process: Any, record: dict[str, Any]) -> None:
        self._process = process
        self.stdin = _Input(process.stdin, record) if process.stdin is not None else None

    async def communicate(self, input: bytes | None = None) -> Any:
        if input is not None and self.stdin is not None:
            self.stdin.observe(input)
        return await self._process.communicate(input)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


async def capture(
    root: Path, exercise: Callable[[], Awaitable[Any]], *,
    inspect_launch: Callable[[list[str], dict[str, str], str], dict[str, Any]],
    readback: Callable[[int], dict[str, Any]], timeout: float = 15,
) -> list[dict[str, Any]]:
    """Observe actual fake-CLI launches; callbacks never replace the launch itself.

    Run serially: interception is process-wide. ``env`` contains values changed
    or added relative to the parent; ``unset`` contains every removed name
    plus the binding's explicit removals supplied by ``inspect_launch``. A
    runtime can clear an inherited directory and then set its own isolated
    directory under the same name, so these two fields can overlap.
    ``first_input`` is the exact first nonempty stdin write (or communicate
    payload), including the runtime's envelope. No IDs or hashes are scrubbed.
    """
    root = root.absolute()
    records: list[dict[str, Any]] = []
    processes: list[Any] = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        argv = [os.fsdecode(arg) for arg in args]
        at = 1 if argv and argv[0] == "env" else 0
        while at and at < len(argv) and "=" in argv[at]:
            at += 1
        if at >= len(argv) or not Path(argv[at]).is_relative_to(root):
            return await original(*args, **kwargs)
        parent = dict(os.environ)
        env = dict(kwargs.get("env") if kwargs.get("env") is not None else parent)
        for assignment in argv[1:at]:
            key, _, value = assignment.partition("=")
            env[key] = value
        details = inspect_launch(argv, env, os.fsdecode(kwargs.get("cwd") or os.getcwd()))
        _fields(details, LAUNCH_FIELDS)
        details = _json_copy(details)
        unset = details.pop("unset")
        if not isinstance(unset, list) or any(type(key) is not str for key in unset):
            raise ValueError("unset must be a list of strings")
        record = {"argv": argv, "env": {k: v for k, v in env.items() if parent.get(k) != v},
                  "unset": sorted((parent.keys() - env.keys()) | set(unset)), "first_input": "", **details}
        process = await original(*args, **kwargs)
        processes.append(process)
        records.append(record)
        return _Process(process, record)

    try:
        with patch.object(asyncio, "create_subprocess_exec", spawn):
            await asyncio.wait_for(exercise(), timeout=timeout)
    finally:
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # It exited between the returncode check and kill.
                await process.wait()
    if not records:
        raise ValueError("launch captures must be non-empty")
    for index, record in enumerate(records):
        state = readback(index)
        _fields(state, STATE_FIELDS)
        record.update(_json_copy(state))
        normalize(record, root=root)  # validate without discarding the raw paths
        if not record["first_input"]:
            raise ValueError(f"launch {index} has no first input")
    return records
