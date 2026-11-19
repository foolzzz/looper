"""One run of a subscription: its window, its processor run and its push (message-stream RFC, section 7).

The scheduler fires a subscription like any schedule and calls ``plan`` inside the firing's transaction, which fixes
the run's window (the events after the cursor up to the source's ``committed_seq``) or, when nothing in it is
selected, ends the firing there as ``skipped_gate`` with the cursor moved past it. A run with a window is ``running``
and ``execute`` (outside the transaction) does the rest:

1. read the window again, render it (``subscriptions.processor_input``) and give it to ``processor.run``;
2. a reply that is the quiet word ends the run ``completed`` (outcome ``quiet``); one that is not goes to the
   schedule's push target once the operator has confirmed it, and stays on the dashboard (``dashboard_only``) before;
3. the output is saved (``delivering``) *before* it is pushed, and the run ends, its notification recorded and its
   cursor moved, in one transaction after the push. A push is one message per part, each with the run's id as its
   idempotency key, so a push made again after a crash (``settle``) sends nothing new while the channel still knows the
   key (Lark: an hour).

A run that fails (the processor, or the push) ends ``failed`` or ``timed_out`` and leaves the cursor, so the next
run's window includes it, and what is older than ``max_age_minutes`` by then is dropped and counted.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from ..db import connection as db_connection, schedules as db_schedules, subscriptions as db_subs
from . import processor, subscriptions
from .message import builtin_channel_id
from .task_notice import NotifyTarget, TargetKind

logger = logging.getLogger(__name__)

# (channel id, target, text, idempotency key) -> whether it was sent, and why not.
Poster = Callable[..., Awaitable[Any]]
OwnApps = Callable[[], Awaitable[Collection[str]]]


def instance_token() -> str:
    """Names this server process on the runs it starts, so a later one can tell a run it left behind."""
    return f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


def push_target(channel: str, chat: str) -> NotifyTarget:
    """Who a push goes to: a person by open id, or a chat (a Lark group ``oc_...``, a Telegram chat)."""
    return NotifyTarget(TargetKind.USER if chat.startswith("ou_") else TargetKind.CHAT, chat,
                        group=chat.startswith(("oc_", "-")))


class SubscriptionRuns:
    """The runs of subscriptions of one server: planning inside the scheduler's transaction, executing and settling."""

    def __init__(self, *, poster: Poster | None, own_apps: OwnApps, runtime_root: Path | None,
                 clock: Callable[[], datetime], claude_bin: str = "claude") -> None:
        self._poster = poster
        self._clock = clock
        self._own_apps = own_apps
        self._runtime_root = runtime_root
        self._claude_bin = claude_bin
        self.instance = instance_token()
        self.active: set[str] = set()  # runs this process is executing

    # --- Firing ---

    async def plan(self, conn: asyncpg.Connection, schedule: Mapping[str, Any], *,
                   now: datetime) -> dict[str, Any] | None:
        """The run's window: None when nothing in it is selected, then the cursor moves past it; else the window the
        run is recorded with (``subscription_runs`` is written here, in the firing's transaction)."""
        sub = schedule["subscription"]
        to_seq = await db_subs.committed_seq(conn, sub["source_id"])
        from_seq = sub["cursor_seq"]
        if to_seq is None or to_seq <= from_seq:
            return None
        picked = await db_subs.window(conn, sub, from_seq=from_seq, to_seq=to_seq, now=now,
                                      own_apps=await self._own_apps())
        if not picked["rows"]:
            await db_subs.advance_cursor(conn, schedule["id"], from_seq=from_seq, to_seq=to_seq, at=now)
            return None
        return {"from_seq": from_seq, "to_seq": to_seq, **picked}

    async def record(self, conn: asyncpg.Connection, run_id: str, window: Mapping[str, Any]) -> None:
        await db_subs.insert_run(conn, run_id, from_seq=window["from_seq"], to_seq=window["to_seq"],
                                 in_window=window["in_window"], selected=len(window["rows"]),
                                 too_old=window["too_old"], over_cap=window["over_cap"])

    # --- Executing ---

    async def execute(self, run_id: str) -> None:
        """Run the processor over the run's window and push or keep its output (the module's notes). Never raises: a
        failure ends the run."""
        self.active.add(run_id)
        try:
            await self._execute(run_id)
        except Exception as exc:
            logger.exception("subscription run %s failed", run_id)
            await self._end(run_id, status="failed", outcome="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self.active.discard(run_id)

    async def _execute(self, run_id: str) -> None:
        run = await db_schedules.get_run(run_id)
        schedule = await db_schedules.get_schedule(run["schedule_id"]) if run else None
        stream = await db_subs.get_run(run_id)
        if run is None or schedule is None or stream is None or run["status"] != db_schedules.RUNNING:
            return
        sub = schedule["subscription"]
        pool = db_connection.get_pool()
        picked = await db_subs.window(pool, sub, from_seq=stream["from_seq"], to_seq=stream["to_seq"],
                                      now=run["fired_at"], own_apps=await self._own_apps())
        if not picked["rows"]:  # every message of it was deleted since the run was planned
            await self._end(run_id, status="completed", outcome="quiet", summary="the window's messages were deleted",
                            advance=True)
            return
        text = subscriptions.processor_input(picked["rows"], text_max_chars=sub["text_max_chars"],
                                             zone_name=schedule["timezone"])
        if self._runtime_root is None:
            await self._end(run_id, status="failed", outcome="failed", error="this server cannot run a processor")
            return
        timeout = (schedule["run_timeout_seconds"] if schedule["run_timeout_mode"] == "seconds"
                   else processor.PROCESSOR_TIMEOUT_SEC)
        reference = {"kind": "subscription_window", "schedule_id": schedule["id"], "run_id": run_id,
                     "from_seq": stream["from_seq"], "to_seq": stream["to_seq"], "events": len(picked["rows"])}
        result = await processor.run(
            owner=schedule["id"], instructions=schedule["requirement"], input_text=text, input_ref=reference,
            quiet_word=sub["quiet_word"], budget_usd=schedule["run_budget_usd"], timeout_s=float(timeout),
            runtime_root=self._runtime_root, claude_bin=self._claude_bin, project_id=schedule["project_id"],
            schedule_id=schedule["id"],
            metadata={"subscription": {**reference, "render_sha256": subscriptions.digest(text)}})
        await db_subs.update_run(pool, run_id, record_id=result.record_id, events_selected=len(picked["rows"]))
        if result.failure is not None:
            status = "timed_out" if result.failure is processor.ProcessorFailure.TIMEOUT else "failed"
            await self._end(run_id, status=status, outcome="failed", cost=result.cost_usd,
                            error=f"the processor gave no reply ({result.failure.value}): {result.detail}")
            return
        reply = result.reply or ""
        if subscriptions.is_quiet(reply, sub["quiet_word"]):
            await self._end(run_id, status="completed", outcome="quiet", cost=result.cost_usd, advance=True,
                            summary="没有内容（处理器回复了静默词）")
            return
        message = subscriptions.push_text(reply, sub["output_header"])
        if not message:
            await self._end(run_id, status="failed", outcome="output_invalid", cost=result.cost_usd,
                            error="the reply was empty once its @ marks were removed")
            return
        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        if not channel or not chat or schedule["notify"] == "never" or sub["target_confirmed_at"] is None:
            await self._end(run_id, status="completed", outcome="dashboard_only", cost=result.cost_usd, advance=True,
                            summary=message, output=message)
            return
        await db_schedules.update_run(pool, run_id, cost_usd=result.cost_usd)
        await db_subs.update_run(pool, run_id, outcome="delivering", output_text=message,
                                 output_sha256=subscriptions.digest(message))
        await self._deliver(run_id, schedule, message, cost=result.cost_usd)

    async def _deliver(self, run_id: str, schedule: Mapping[str, Any], message: str, *,
                       cost: float | None) -> None:
        channel, chat = schedule["notify_channel"], schedule["notify_chat"]
        if self._poster is None:
            await self._end(run_id, status="failed", outcome="failed", cost=cost, output=message,
                            error="this server cannot send messages")
            return
        parts = subscriptions.push_parts(message)
        target = push_target(channel, chat)
        for part, key in zip(parts, subscriptions.idempotency_keys(run_id, len(parts))):
            try:
                receipt = await self._poster(builtin_channel_id(channel), target, part, idempotency_key=key)
            except Exception as exc:  # a channel's failure is the run's, never the scheduler's
                receipt, error = None, f"{type(exc).__name__}: {exc}"
            else:
                error = receipt.error
            if receipt is None or not receipt.success:
                await self._end(run_id, status="failed", outcome="failed", cost=cost, output=message,
                                error=f"the push to {channel} {chat} failed: {error or 'not sent'}")
                return
        await self._end(run_id, status="completed", outcome="delivered", cost=cost, advance=True, output=message,
                        summary=message, pushed=(channel, chat))

    # --- Ending ---

    async def _end(self, run_id: str, *, status: str, outcome: str, cost: float | None = None,
                   error: str | None = None, summary: str | None = None, advance: bool = False,
                   output: str | None = None, pushed: tuple[str, str] | None = None) -> None:
        """End the run in one transaction: its status, its outcome and output, the push's notification, and, when it
        succeeded, the cursor."""
        now = self._clock()
        async with db_connection.transaction() as conn:
            run = await db_schedules.get_run(run_id, pool=conn)
            stream = await db_subs.get_run(run_id, db=conn)
            if run is None or stream is None or run["status"] != db_schedules.RUNNING:
                return
            fields: dict[str, Any] = {"outcome": outcome}
            if output is not None:
                fields.update(output_text=output, output_sha256=subscriptions.digest(output))
            if pushed is not None:
                fields["notification_id"] = await db_schedules.insert_notification(
                    conn, schedule_id=run["schedule_id"], run_id=run_id, kind="run", channel=pushed[0], chat=pushed[1],
                    text=output or "", status="sent", error=None)
            finished = await db_schedules.finish_run(
                conn, run_id, status=status, at=now, summary=summary, error=error,
                cost_usd=cost if cost is not None else run.get("cost_usd"))
            if not finished:
                return
            await db_subs.update_run(conn, run_id, **fields)
            if advance:
                await db_subs.advance_cursor(conn, run["schedule_id"], from_seq=stream["from_seq"],
                                             to_seq=stream["to_seq"], at=now)
        logger.info("subscription run %s ended %s (%s)", run_id, status, outcome)

    # --- After a restart ---

    async def settle(self, run: Mapping[str, Any], now: datetime) -> bool:
        """A subscription run that is ``running`` and not being executed by this process: a restart left it. One whose
        push may have been made (output saved, ``delivering``) pushes again with the same keys; any other ends
        ``interrupted``, leaving the cursor. False for a run another live server is executing."""
        if run["id"] in self.active or not self._orphaned(run, now):
            return False
        stream = await db_subs.get_run(run["id"])
        if stream is not None and stream["outcome"] == "delivering" and stream["output_text"]:
            schedule = await db_schedules.get_schedule(run["schedule_id"])
            if schedule is not None and schedule.get("notify_channel"):
                self.active.add(run["id"])
                try:
                    await self._deliver(run["id"], schedule, stream["output_text"], cost=run.get("cost_usd"))
                finally:
                    self.active.discard(run["id"])
                return True
        await self._end(run["id"], status="interrupted", outcome="failed",
                        error="the server stopped while the run was in progress")
        return True

    def _orphaned(self, run: Mapping[str, Any], now: datetime) -> bool:
        """Whether the process that started the run is gone: it is not this one, and its pid is not alive on this
        machine (or the run is older than anything it could still be doing)."""
        if run.get("runner_started") == self.instance:
            return False
        pid = run.get("runner_pid")
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass
        fired = run.get("fired_at")
        return fired is None or (now - fired).total_seconds() > processor.PROCESSOR_TIMEOUT_SEC + 300

