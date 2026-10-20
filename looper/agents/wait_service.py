"""Run one wait check. The loop that decides when to run one comes later.

``run_github_pr`` runs a fixed ``gh pr view`` (no shell). ``run_command`` runs a
shell command only when the project allows it. Each returns ``(result, output,
error, extra)``. ``extra`` carries ``baseline`` (``{"head", "comments"}``, or
None), ``resolution`` (``Ready: …`` when the check is ready) and ``exit_code``
when the process exited. The loop decides whether a pull request's first view
only records that baseline.

Output is the last ``OUTPUT_MAX_BYTES`` of the combined stream, decoded with
invalid bytes replaced. The database caps it again when the row is written.
Nothing here is logged: the output and the command stay out of the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .. import child_env
from . import proc_tree, waits
from .waits import (
    CHECK_TIMEOUT_SEC, OUTPUT_MAX_BYTES, CheckResult, CommandCheck, GithubPrCheck, ReadyWhen,
)

_JSON_FIELDS = "headRefOid,state,comments,reviews,statusCheckRollup"
# A StatusContext still waiting reports PENDING or EXPECTED. Anything else has finished.
_UNFINISHED_STATES = frozenset({"PENDING", "EXPECTED"})
_POLL_SEC = 0.05
_DRAIN_SEC = 2.0

Outcome = tuple[CheckResult, str, str, dict[str, Any]]


def _tail(data: bytes) -> str:
    """The newest ``OUTPUT_MAX_BYTES`` of ``data``, with a split character replaced."""
    return data[-OUTPUT_MAX_BYTES:].decode("utf-8", errors="replace")


def _cap(text: str) -> str:
    """The same byte tail as the output, so an error cannot carry more than the output may."""
    return _tail(text.encode("utf-8"))


def _timeout_error() -> str:
    """The product limit. A test may shorten the wait without changing this sentence."""
    return f"timed out after {waits.CHECK_TIMEOUT_SEC:g} s"


def _extra(*, baseline: dict[str, Any] | None = None, resolution: str = "",
           exit_code: int | None = None) -> dict[str, Any]:
    return {"baseline": baseline, "resolution": resolution, "exit_code": exit_code}


def _done(result: CheckResult, output: str, error: str, extra: dict[str, Any]) -> Outcome:
    return result, output, _cap(error), extra


async def _exited(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Whether ``process`` exits within ``timeout``. Polling leaves ``wait`` for the reap that follows a timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_POLL_SEC)
    return process.returncode is not None


async def _capture(process: asyncio.subprocess.Process, timeout: float,
                   stop: Callable[[], Awaitable[None]]) -> tuple[bytes, bool]:
    """The combined stream, and whether ``stop`` had to end ``process``."""
    assert process.stdout is not None
    data = bytearray()

    async def drain() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            data.extend(chunk)

    reader = asyncio.ensure_future(drain())
    timed_out = not await _exited(process, timeout)
    if timed_out:
        await stop()
    try:
        await asyncio.wait_for(reader, _DRAIN_SEC)
    except TimeoutError:
        reader.cancel()
    try:
        await reader
    except asyncio.CancelledError:
        pass
    except Exception:
        # A killed process can reset the pipe. The bytes already read are the output.
        if not timed_out:
            raise
    return bytes(data), timed_out


def _drop_pipes(process: asyncio.subprocess.Process) -> None:
    """Close our end of the pipes. ``wait`` stays open until every pipe disconnects."""
    for stream in (process.stdout, process.stderr, process.stdin):
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


async def _kill(process: asyncio.subprocess.Process) -> None:
    """Kill and reap ``process``. A child that inherited its output is killed too.

    ``Process.wait`` returns only after the pipes disconnect. ``gh`` is not a shell, but a helper it
    leaves behind would otherwise hold the check open until that helper exited on its own.
    """
    known = await proc_tree.begin_end(process)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    for pid in known:
        if pid != process.pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), _DRAIN_SEC)
    except TimeoutError:
        _drop_pipes(process)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), _DRAIN_SEC)


def _baseline_of(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    head = payload.get("headRefOid")
    comments, reviews = payload.get("comments"), payload.get("reviews")
    if not isinstance(head, str) or not isinstance(comments, list) or not isinstance(reviews, list):
        return None
    return {"head": head, "comments": len(comments) + len(reviews)}


def _previous_head(baseline: Mapping[str, Any] | None) -> str | None:
    if not isinstance(baseline, Mapping):
        return None
    head = baseline.get("head")
    return head if isinstance(head, str) else None


def _previous_comments(baseline: Mapping[str, Any] | None) -> int | None:
    if not isinstance(baseline, Mapping):
        return None
    count = baseline.get("comments")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _node_finished(node: Any) -> bool:
    """One rollup entry. A CheckRun is finished at COMPLETED; a StatusContext is finished once it is no longer waiting."""
    if not isinstance(node, Mapping):
        return False
    kind = node.get("__typename")
    if kind == "CheckRun" or (kind is None and "status" in node and "state" not in node):
        return node.get("status") == "COMPLETED"
    if kind == "StatusContext" or (kind is None and "state" in node):
        state = node.get("state")
        return isinstance(state, str) and state not in _UNFINISHED_STATES
    return False


def _checks_finished(rollup: Any) -> bool:
    """At least one check, and every CheckRun and StatusContext finished."""
    if not isinstance(rollup, list) or not rollup:
        return False
    return all(_node_finished(node) for node in rollup)


def _resolution(check: GithubPrCheck, payload: Mapping[str, Any], baseline: Mapping[str, Any] | None,
                extracted: Mapping[str, Any]) -> str:
    """Why the request is ready, or "" when it is not. Merged and closed win over the watched condition."""
    state = payload.get("state")
    where = f"{check.repo}#{check.number}"
    if state == "MERGED":
        return f"Ready: {where} was merged."
    if state == "CLOSED":
        return f"Ready: {where} was closed."
    if check.ready_when is ReadyWhen.HEAD_CHANGED:
        previous = _previous_head(baseline)
        if previous is not None and previous != extracted["head"]:
            return f"Ready: the head of {where} changed ({previous} → {extracted['head']})."
    elif check.ready_when is ReadyWhen.CHECKS_FINISHED:
        if _checks_finished(payload.get("statusCheckRollup")):
            return f"Ready: the checks of {where} finished."
    elif check.ready_when is ReadyWhen.NEW_COMMENTS:
        previous = _previous_comments(baseline)
        current = extracted["comments"]
        if previous is not None and current > previous:
            return f"Ready: {where} has new comments ({previous} → {current})."
    return ""


def _invalid(output: str, exit_code: int | None) -> Outcome:
    return _done(CheckResult.ERROR, output, "the pull request view is not valid JSON", _extra(exit_code=exit_code))


def _from_view(check: GithubPrCheck, baseline: Mapping[str, Any] | None, data: bytes, exit_code: int) -> Outcome:
    """Read the whole view. ``output`` is only its tail: the head and the comment count live at the start."""
    output = _tail(data)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return _invalid(output, exit_code)
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
        return _invalid(output, exit_code)
    extracted = _baseline_of(payload)
    if extracted is None:
        return _invalid(output, exit_code)
    resolution = _resolution(check, payload, baseline, extracted)
    result = CheckResult.READY if resolution else CheckResult.NOT_READY
    return _done(result, output, "", _extra(baseline=extracted, resolution=resolution, exit_code=exit_code))


async def run_github_pr(check: GithubPrCheck, baseline: Mapping[str, Any] | None, *, gh: str) -> Outcome:
    """View ``check`` with ``gh`` and say whether it is ready. ``baseline`` is the previous ``{head, comments}``."""
    try:
        process = await asyncio.create_subprocess_exec(
            gh, "pr", "view", str(check.number), "--repo", check.repo, "--json", _JSON_FIELDS,
            env=child_env.inherited(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError:
        return _done(CheckResult.ERROR, "", "the check could not start", _extra())
    data, timed_out = await _capture(process, CHECK_TIMEOUT_SEC, lambda: _kill(process))
    output = _tail(data)
    if timed_out:
        return _done(CheckResult.ERROR, output, _timeout_error(), _extra())
    code = process.returncode
    if code != 0:
        return _done(CheckResult.ERROR, output, f"gh exited {code}", _extra(exit_code=code))
    assert code is not None
    return _from_view(check, baseline, data, code)


async def _end_command(process: asyncio.subprocess.Process) -> None:
    """End the command and every process it started, the way a run of Claude is ended."""
    known = await proc_tree.begin_end(process)
    await proc_tree.end(process, label="wait command", known=known)


async def run_command(check: CommandCheck, work_dir: str | os.PathLike[str], *, project_allows: bool) -> Outcome:
    """Run ``check`` in ``work_dir`` when the project allows commands. Exit 0 is ready; anything else is not."""
    if not project_allows:
        return _done(CheckResult.ERROR, "", "wait_commands is off", _extra())
    try:
        process = await asyncio.create_subprocess_shell(
            check.command, cwd=work_dir, env=child_env.inherited(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    except OSError:
        return _done(CheckResult.ERROR, "", "the check could not start", _extra())
    data, timed_out = await _capture(process, CHECK_TIMEOUT_SEC, lambda: _end_command(process))
    output = _tail(data)
    if timed_out:
        return _done(CheckResult.ERROR, output, _timeout_error(), _extra())
    code = process.returncode
    if code == 0:
        return _done(CheckResult.READY, output, "",
                     _extra(resolution="Ready: the command exited 0.", exit_code=0))
    return _done(CheckResult.NOT_READY, output, "", _extra(exit_code=code))
