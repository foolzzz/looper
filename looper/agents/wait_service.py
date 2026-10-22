"""Run due waits and their checks, recording each result before moving a waiting row.

``WaitService`` checks active tasks at ``WAIT_TICK_SEC`` with at most four
checks at once. Ready and timed-out rows wake their Planner only after the
conditional move succeeds; a timeout records its activity and notice first.

``run_github_pr`` runs a fixed ``gh pr view`` (no shell). ``run_command`` runs a
shell command only when the project allows it. Each returns ``(result, output,
error, extra)``. ``extra`` carries ``baseline`` (``{"head", "comments"}``, or
None), ``resolution`` (``Ready: …`` when the check is ready) and ``exit_code``
when the process exited. The loop decides whether a pull request's first view
only records that baseline.

Output is the last ``OUTPUT_MAX_BYTES`` of the combined stream, decoded with
invalid bytes replaced. The database caps it again when the row is written.
Command output is bounded while streaming; the structured GitHub view has a
1 MiB capture limit and returns an error if it exceeds that limit.
Only check identifiers, result and exit code are logged; output and command text stay out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import child_env
from ..db import activity as db_activity, projects as db_projects, tasks as db_tasks, waits as db_waits
from . import proc_tree, waits
from .bus import MessageBus
from .links import task_link
from .message import AgentAddress, MessageEnvelope, MessageType, channel_address, planner_address
from .project_execution import ProjectExecution
from .projects import RequestError
from .task_notice import Notice, NotifyTarget, link_line, notify_channel
from .tiers.journal import clip
from .waits import (
    CHECK_TIMEOUT_SEC, OUTPUT_MAX_BYTES, CheckResult, CommandCheck, GithubPrCheck, ReadyWhen,
)

logger = logging.getLogger(__name__)

_JSON_FIELDS = "headRefOid,state,comments,reviews,statusCheckRollup"
# A StatusContext still waiting reports PENDING or EXPECTED. Anything else has finished.
_UNFINISHED_STATES = frozenset({"PENDING", "EXPECTED"})
_POLL_SEC = 0.05
_DRAIN_SEC = 2.0
# Keep enough structured data to parse an ordinary PR view, but never buffer an unbounded response.
_GITHUB_VIEW_MAX_BYTES = 1024 * 1024

Outcome = tuple[CheckResult, str, str, dict[str, Any]]


def wait_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A wait on the task page, including its persisted check spec and latest result."""
    spec = row["spec"]
    view = {
        "step": row.get("step_key"), "step_id": row["step_id"], "state": row["state"],
        "outcome": row.get("outcome"), "topic": row["topic"], "check": spec.get("check"),
        "every_seconds": spec.get("every_seconds"),
        **{key: row.get(key) for key in ("opened_at", "due_at", "deadline_at", "next_check_at",
                                       "last_checked_at", "last_result", "notice", "closed_at")},
        "checks": row.get("checks") or 0, "last_output": row.get("last_output") or "",
        "last_error": row.get("last_error") or "", "resolution": row.get("resolution") or "",
        "depends_on": list(row.get("depends_on") or []),
    }
    # Keep the same timestamp format through HTTP and the Manager's tool serializer.
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in view.items()}


async def waits_view(task_id: str) -> list[dict[str, Any]]:
    """The task's waits in plan order."""
    return [wait_view(row) for row in await db_waits.list_for_task(task_id)]


async def resume(task_id: str, label: str, *, has_planner: bool, bus: MessageBus,
                 source: AgentAddress) -> dict[str, Any]:
    """Make a waiting step ready on the user's request; its Planner settles it next."""
    return await _user_move(task_id, label, cancel=False, has_planner=has_planner, bus=bus, source=source)


async def cancel(task_id: str, label: str, *, has_planner: bool, bus: MessageBus,
                 source: AgentAddress) -> dict[str, Any]:
    """Ask the Planner to decide what follows the user's cancellation of a waiting step."""
    return await _user_move(task_id, label, cancel=True, has_planner=has_planner, bus=bus, source=source)


async def _user_move(task_id: str, label: str, *, cancel: bool, has_planner: bool, bus: MessageBus,
                     source: AgentAddress) -> dict[str, Any]:
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError("404 Not Found", "task not found")
    current = await db_waits.get(task_id, label)
    if current is None:
        raise RequestError("404 Not Found", f"task {task_id} has no wait {label}")
    if task.get("status") not in db_tasks.ACTIVE_STATUSES:
        raise RequestError("409 Conflict", "the task has ended: its waits no longer take user actions")
    state = waits.WaitState(current["state"])
    if state is not waits.WaitState.WAITING:
        reason = f"wait {label} is {state.value}"
        if state in (waits.WaitState.TIMED_OUT, waits.WaitState.CANCEL_REQUESTED):
            reason += ": its Planner decides what happens next"
        raise RequestError("409 Conflict", reason)
    verb = "cancelled" if cancel else "resumed"
    moved = await db_waits.move(
        current["step_id"], to="cancel_requested" if cancel else "ready", expect=("waiting",),
        outcome=None if cancel else "resumed", resolution=f"{verb.capitalize()} by the user from the dashboard.")
    if moved is None:
        raise RequestError("409 Conflict", f"wait {label} changed before this action: refresh and try again")
    await db_activity.insert_activity(task_id=task_id, worker="planner", kind="phase",
                                      summary=f"Wait {label} {verb} by the user")
    if has_planner:
        await bus.publish(MessageEnvelope.create(
            source=source, target=planner_address(task_id), message_type=MessageType.WAIT_EVENT,
            payload={"task_id": task_id, "wait": label}))
    # move returns the wait table's columns; retain the step label and dependency labels read above.
    return {"task_id": task_id, "wait": wait_view({**current, **moved})}


async def settings_view() -> dict[str, Any]:
    return await waits.settings_view()


async def update_settings(body: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return await waits.update(body)
    except ValueError as exc:
        raise RequestError("400 Bad Request", str(exc)) from None


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
                   stop: Callable[[], Awaitable[None]], *,
                   max_bytes: int = OUTPUT_MAX_BYTES) -> tuple[bytes, bool, bool]:
    """A bounded tail of the combined stream, whether it timed out, and whether it overflowed."""
    assert process.stdout is not None
    data = bytearray()
    overflowed = False

    async def drain() -> None:
        nonlocal overflowed
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            data.extend(chunk)
            if len(data) > max_bytes:
                overflowed = True
                del data[:-max_bytes]

    reader = asyncio.ensure_future(drain())
    try:
        timed_out = not await _exited(process, timeout)
        if timed_out:
            await stop()
        try:
            await asyncio.wait_for(reader, _DRAIN_SEC)
        except TimeoutError:
            reader.cancel()
        if not reader.cancelled():
            try:
                await reader
            except Exception:
                # A killed process can reset the pipe. The bytes already read are the output.
                if not timed_out:
                    raise
        return bytes(data), timed_out, overflowed
    except asyncio.CancelledError:
        # Stopping the service must reap the in-flight check as well as its loop.
        try:
            await stop()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        raise


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


async def run_github_pr(check: GithubPrCheck, baseline: Mapping[str, Any] | None, *, gh: str,
                        work_dir: str | None = None) -> Outcome:
    """View ``check`` with ``gh`` and say whether it is ready. ``baseline`` is the previous ``{head, comments}``."""
    try:
        process = await asyncio.create_subprocess_exec(
            gh, "pr", "view", str(check.number), "--repo", check.repo, "--json", _JSON_FIELDS,
            cwd=work_dir, env=child_env.inherited(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError:
        return _done(CheckResult.ERROR, "", "the check could not start", _extra())
    data, timed_out, overflowed = await _capture(
        process, CHECK_TIMEOUT_SEC, lambda: _kill(process), max_bytes=_GITHUB_VIEW_MAX_BYTES)
    output = _tail(data)
    if timed_out:
        return _done(CheckResult.ERROR, output, _timeout_error(), _extra())
    code = process.returncode
    if code != 0:
        return _done(CheckResult.ERROR, output, f"gh exited {code}", _extra(exit_code=code))
    if overflowed:
        return _done(CheckResult.ERROR, output,
                     f"the pull request view exceeds the {_GITHUB_VIEW_MAX_BYTES}-byte limit",
                     _extra(exit_code=code))
    assert code is not None
    return _from_view(check, baseline, data, code)


async def _end_command(process: asyncio.subprocess.Process, known: proc_tree.Tree | None = None) -> None:
    """End the command and every process it started, the way a run of Claude is ended (its group signalled
    before anything is listed)."""
    await proc_tree.end(process, label="wait command", known=known)


async def run_command(check: CommandCheck, work_dir: str | os.PathLike[str], *, project_allows: bool) -> Outcome:
    """Run ``check`` in ``work_dir`` when the project allows commands. Exit 0 is ready; anything else is not."""
    if not project_allows:
        return _done(CheckResult.ERROR, "", "wait_commands is off", _extra())
    try:
        process = await proc_tree.spawn(
            "/bin/sh", "-c", check.command, cwd=work_dir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError:
        return _done(CheckResult.ERROR, "", "the check could not start", _extra())
    known: proc_tree.Tree = {}
    try:
        # Keep the record until cleanup, even if the shell exits before its background children.
        known = await proc_tree.begin_end(process)
        data, timed_out, _ = await _capture(process, CHECK_TIMEOUT_SEC, lambda: _end_command(process, known))
    finally:
        await _end_command(process, known)
    output = _tail(data)
    if timed_out:
        return _done(CheckResult.ERROR, output, _timeout_error(), _extra())
    code = process.returncode
    if code == 0:
        return _done(CheckResult.READY, output, "",
                     _extra(resolution="Ready: the command exited 0.", exit_code=0))
    return _done(CheckResult.NOT_READY, output, "", _extra(exit_code=code))


async def _commands_allowed(task_id: str) -> bool:
    """The task's current project must still allow commands when a check starts."""
    return ProjectExecution.from_row(await db_projects.get_task_project(task_id)).wait_commands_on


class WaitService:
    """Every ``interval`` seconds, check each due wait of an active task, at most
    ``MAX_CONCURRENT_CHECKS`` at once. Store checks before conditional moves;
    tell the user of timeouts, then wake a Planner that is running."""

    def __init__(self, *, bus: MessageBus, has_planner: Callable[[str], bool], dashboard_url: str = "",
                 interval: float = waits.WAIT_TICK_SEC, gh: str = "gh") -> None:
        self._bus = bus
        self._has_planner = has_planner
        self._dashboard_url = dashboard_url
        self._interval = interval
        self._gh = gh
        self._semaphore = asyncio.Semaphore(waits.MAX_CONCURRENT_CHECKS)
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("the wait loop failed; trying again later")
            await asyncio.sleep(self._interval)

    async def run_once(self, *, clock: Callable[[], datetime] | None = None) -> int:
        """Check due waits; how many rows moved. ``clock`` supplies UTC now when given."""
        clock = clock or (lambda: datetime.now(timezone.utc))
        rows = await db_waits.due(clock(), db_tasks.ACTIVE_STATUSES)
        return sum(await asyncio.gather(*(self._run_row(row, clock) for row in rows)))

    async def _run_row(self, row: Mapping[str, Any], clock: Callable[[], datetime]) -> int:
        async with self._semaphore:
            try:
                # The queue may outlive a user action, a Planner transition or the task itself.
                current = await db_waits.get(row["task_id"], row["step_key"])
                task = await db_tasks.get_task(row["task_id"])
                if (current is None or current["step_id"] != row["step_id"]
                        or current["state"] != waits.WaitState.WAITING.value
                        or task is None or task["status"] not in db_tasks.ACTIVE_STATUSES):
                    return 0
                return await self._run_wait({**current, "work_dir": task["work_dir"]}, clock)
            except Exception:
                # Exceptions may contain command output or notice text. Only the row's id belongs in the log.
                logger.warning("wait step %s failed; trying again later", row["step_id"])
                return 0

    async def _run_wait(self, row: Mapping[str, Any], clock: Callable[[], datetime]) -> int:
        now = clock()
        step_id, task_id = row["step_id"], row["task_id"]
        spec = waits.WaitSpec.from_row(row["topic"], row["spec"])
        check = spec.check
        moved = None
        if check is not None and row["next_check_at"] is not None and row["next_check_at"] <= now:
            if isinstance(check, GithubPrCheck):
                result, output, error, extra = await run_github_pr(
                    check, row["baseline"], gh=self._gh, work_dir=row["work_dir"])
            else:
                result, output, error, extra = await run_command(
                    check, row["work_dir"], project_allows=await _commands_allowed(task_id))
            now = clock()
            # Keep the first view as the baseline: a change before due_at must still count afterwards.
            baseline = None
            if isinstance(check, GithubPrCheck) and row["baseline"] is None and result is not CheckResult.ERROR:
                baseline = extra["baseline"]
                result = CheckResult.NOT_READY
                output = f"baseline: head {baseline['head'][:7]}, {baseline['comments']} comments"
            recorded = await db_waits.record_check(
                step_id, at=now, result=result.value, output=output, error=error,
                next_check_at=now + timedelta(seconds=waits.effective_every(spec, waits.limits())), baseline=baseline)
            logger.info("wait step %s task %s result %s exitcode %s", step_id, task_id, result.value,
                        extra.get("exit_code"))
            if not recorded:
                return 0
            if result is CheckResult.ERROR and row["last_result"] != CheckResult.ERROR.value:
                await db_activity.insert_activity(task_id=task_id, worker="planner", kind="phase",
                                                  summary=f"Wait {row['step_key']} check failed: {error[:200]}")
            if result is CheckResult.READY and row["due_at"] <= now:
                moved = await db_waits.move(step_id, to="ready", expect=("waiting",), outcome="ready",
                                            resolution=extra["resolution"])
                if moved is None:
                    return 0
        elif check is None and row["due_at"] <= now:
            stamp = row["due_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            moved = await db_waits.move(step_id, to="ready", expect=("waiting",), outcome="ready",
                                        resolution=f"Ready: the time it waited for came ({stamp}).")
            if moved is None:
                return 0
        if moved is None and row["deadline_at"] <= now:
            seconds = max(0, int((now - row["opened_at"]).total_seconds()))
            duration = waits._duration(seconds)
            moved = await db_waits.move(step_id, to="timed_out", expect=("waiting",),
                                        resolution=f"Timed out after {duration}.")
            if moved is not None:
                await db_activity.insert_activity(
                    task_id=task_id, worker="planner", kind="phase",
                    summary=f"Wait {row['step_key']} timed out after {duration}: {row['topic']}; the Planner decides next")
                await self._notify_timeout(row, seconds)
        if moved is None:
            return 0
        if self._has_planner(task_id):
            try:
                await self._bus.publish(MessageEnvelope.create(
                    source=AgentAddress("wait_service", "wait-service"), target=planner_address(task_id),
                    message_type=MessageType.WAIT_EVENT, payload={"task_id": task_id, "wait": row["step_key"]}))
            except Exception:
                logger.warning("wait step %s could not wake its Planner", step_id)
        return 1

    async def _notify_timeout(self, row: Mapping[str, Any], seconds: int) -> None:
        task_id = row["task_id"]
        task = await db_tasks.get_task(task_id)
        if task is None or task["status"] not in db_tasks.ACTIVE_STATUSES:
            return
        channel, target = notify_channel(task.get("origin_channel_id")), NotifyTarget.from_columns(task)
        notice = "dashboard"
        if channel is not None and target is not None:
            name = db_tasks.task_name(title=task.get("title"), goal_id=task.get("goal_id"),
                                      requirement=task.get("requirement"), task_id=task_id)
            if seconds >= 86400:
                waited = f"{seconds // 86400} 天"
            elif seconds >= 3600:
                waited = f"{seconds // 3600} 小时"
            else:
                waited = f"{max(1, seconds // 60)} 分钟"
            text = "\n".join([f"⏳ 等待超时 · {clip(name, 40)}", row["topic"],
                               f"等了 {waited}，条件一直没有满足；规划者接下来决定怎么做。",
                               link_line(task_link(self._dashboard_url, task_id), channel)])
            try:
                await self._bus.publish(MessageEnvelope.create(
                    source=planner_address(task_id), target=channel_address(channel.value),
                    message_type=MessageType.CHAT_NOTICE, payload=Notice(channel, target, text).payload(),
                    metadata={"origin_channel_id": task.get("origin_channel_id")}))
                notice = "sent"
            except Exception:
                notice = "failed"
                logger.warning("wait step %s could not notify its chat", row["step_id"])
        await db_waits.set_notice(row["step_id"], notice)
