"""One run of a subscription: its window, its processor run and its push (message-stream RFC, section 7).

The scheduler fires a subscription like any schedule and calls ``plan`` inside the firing's transaction, which fixes
the run's window (the events after the cursor up to the source's ``committed_seq``) or, when nothing in it is
selected, ends the firing there as ``skipped_gate`` with the cursor moved past it. A run with a window is ``running``
and ``execute`` (outside the transaction) does the rest:

1. read the window again, render it (``subscriptions.processor_input``) and give it to ``processor.run``; a window a
   prompt hook blocks ends the run ``completed`` (outcome ``skipped``, ``blocked_by_hook``), pushes nothing and moves
   the cursor past it, since it is blocked every time it is sent;
2. a reply that is the quiet word ends the run ``completed`` (outcome ``quiet``); one that is not goes to the
   target the run started with (``target_channel``/``target_chat``, pinned only when the operator had confirmed it for
   the configuration then in force), and stays on the dashboard (``dashboard_only``) when there was none;
3. its messages are rows of ``subscription_deliveries``, each claimed (``sending``, committed) before the channel is
   asked and never sent again after that unless the channel answered that it did not send it: one left ``sending`` by
   a process that died, or whose ask raised, is ``uncertain``. Before every message ``_refusal`` re-checks the pin; the
   run ends, with its notification and its cursor, in one transaction.

A run whose messages all failed ends ``failed`` and leaves the cursor; one that got a message out (sent or uncertain)
moves it, so no later run starts a second push for the same window. An extraction (``output_kind`` ``leads`` or
``intents``) is planned and executed by ``extraction`` instead, and pushes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from ..db import (connection as db_connection, proactive_deliveries as db_deliveries,
                  schedules as db_schedules, subscriptions as db_subs)
from . import (chat_settings, cron, extraction, model_call, proactive_delivery as delivery, processor,
               subscription_rules, subscriptions, turn_metrics)
from .agent_settings import AgentType
from .channels.base import PostOutcome
from .message import MessageEnvelope, builtin_channel_id, channel_thread_id, is_lark, parse_channel_id
from .task_notice import NotifyTarget, TargetKind

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 2.0  # between the attempts to send a message the channel refused


class _NoPoster(Exception):
    pass

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


async def _matcher(db: Any, sub: Mapping[str, Any]) -> subscription_rules.Matcher | None:
    """The subscription's rules for one window, or None when it selects with its own filter."""
    if not sub.get("rules"):
        return None
    return subscription_rules.Matcher(sub["rules"], me=await db_subs.source_user(db, sub["source_id"]))


class SubscriptionRuns:
    """The runs of subscriptions of one server: planning inside the scheduler's transaction, executing and settling."""

    def __init__(self, *, poster: Poster | None, own_apps: OwnApps, runtime_root: Path | None,
                 clock: Callable[[], datetime], claude_bin: str = "claude",
                 day_start: Callable[[datetime], datetime] | None = None) -> None:
        self._poster = poster
        self._clock = clock
        # Where the day a daily budget counts starts (``Scheduler.day_start``); None: midnight in this machine's zone.
        self._day_start = day_start
        self._own_apps = own_apps
        self._runtime_root = runtime_root
        self._claude_bin = claude_bin
        self.instance = instance_token()
        self.active: set[str] = set()  # runs this process is executing
        self._recent_lark_users: dict[tuple[str, str], datetime] = {}

    def observe_user(self, envelope: MessageEnvelope) -> None:
        """Lark DM targets are user ids, while their saved histories use native chat ids."""
        if (not chat_settings.delivery_enabled() or envelope.source.agent_type != "channel"
                or envelope.target.agent_type != "manager"):
            return
        channel = parse_channel_id(envelope.source.agent_id)
        context = envelope.payload.get("channel_context") or {}
        if (channel is None or not is_lark(channel.channel_type) or not isinstance(context, dict)
                or context.get("chat_type") != "p2p"
                or not context.get("sender_id")):
            return
        now = self._clock()
        self._recent_lark_users = {key: at for key, at in self._recent_lark_users.items()
                                   if now - at <= delivery.DEFAULT_RECENT_REPLY}
        self._recent_lark_users[(envelope.source.agent_id, context["sender_id"])] = datetime.fromisoformat(
            envelope.timestamp)

    def day_start(self, now: datetime) -> datetime:
        """The start of ``now``'s day, as the daily budget counts it."""
        if self._day_start is not None:
            return self._day_start(now)
        zone = cron.zone(cron.local_zone_name())
        return datetime.combine(now.astimezone(zone).date(), datetime.min.time(), tzinfo=zone)

    # --- Firing ---

    async def plan(self, conn: asyncpg.Connection, schedule: Mapping[str, Any], *,
                   now: datetime, scheduled_for: datetime) -> dict[str, Any] | None:
        """The run's window: None when nothing in it is selected, then the cursor moves past it; else the window the
        run is recorded with (``subscription_runs`` is written here, in the firing's transaction), with the target the
        run may push to: the schedule's, when the operator has confirmed it for the configuration it has now. An
        extraction's (``extraction.plan``) may also be a skip (``skip_reason``): turned off or not confirmed, the
        cursor stays."""
        sub = schedule["subscription"]
        if subscriptions.is_extraction(sub):
            return await extraction.plan(conn, schedule, now=now, own_apps=await self._own_apps())
        to_seq = await db_subs.committed_seq(conn, sub["source_id"])
        mode = sub.get("window", "cursor")
        from_seq = sub["cursor_seq"] if mode == "cursor" else 0
        if to_seq is None or to_seq <= from_seq:
            return None
        picked = await db_subs.window(conn, sub, from_seq=from_seq, to_seq=to_seq, now=now,
                                      own_apps=await self._own_apps(), matcher=await _matcher(conn, sub))
        if not picked["rows"]:
            if mode == "cursor":
                await db_subs.advance_cursor(conn, schedule["id"], from_seq=from_seq, to_seq=to_seq, at=now)
            return None
        daily_date = (scheduled_for.astimezone(cron.zone(schedule["timezone"])).date()
                      if sub.get("delivery_policy") == "daily" else None)
        if daily_date is not None and not await db_subs.claim_day(conn, schedule["id"], daily_date):
            return {"skip_reason": "the subscription already attempted this local day's digest or a later one"}
        version = await subscriptions.config_version(schedule)
        confirmed = (sub["target_confirmed_at"] is not None and sub.get("target_confirmed_version") == version
                     and schedule.get("notify_channel") and schedule["notify"] != "never")
        if confirmed:  # a target that private content may not go to is not pinned: the output stays on the dashboard
            try:
                subscriptions.check_private_target(sub, schedule["notify_channel"], schedule["notify_chat"],
                                                   await db_subs.source_user(conn, sub["source_id"]))
            except ValueError:
                confirmed = False
        return {"from_seq": from_seq, "to_seq": to_seq, "window_mode": mode, "daily_date": daily_date, **picked,
                "target": (schedule["notify_channel"], schedule["notify_chat"]) if confirmed else None,
                "config_version": version if confirmed else None, "config": db_subs.snapshot(sub),
                "config_epoch": sub["config_epoch"], "match_timeouts": int(picked["match_timed_out"])}

    async def record(self, conn: asyncpg.Connection, run_id: str, window: Mapping[str, Any]) -> None:
        await db_subs.insert_run(conn, run_id, from_seq=window["from_seq"], to_seq=window["to_seq"],
                                 in_window=window["in_window"], selected=len(window["rows"]),
                                 too_old=window["too_old"], over_cap=window["over_cap"], target=window["target"],
                                 config_version=window["config_version"], window_mode=window["window_mode"],
                                 daily_date=window["daily_date"], config=window["config"], config_epoch=window["config_epoch"],
                                 match_timeouts=window["match_timeouts"])
        if window.get("snapshot"):  # an extraction's pinned configuration and confirmation (``extraction.plan``)
            await db_schedules.update_run(conn, run_id, execution_snapshot=window["snapshot"])

    # --- Executing ---

    async def execute(self, run_id: str) -> bool:
        """Run the processor over the run's window and push or keep its output (the module's notes), or, for an
        extraction, store what it found (``extraction.execute``). Never raises: a failure ends the run. True when an
        extraction's window left events for another run at once (``more``)."""
        self.active.add(run_id)
        try:
            return await self._execute(run_id)
        except Exception as exc:
            logger.error("subscription run %s failed: %s", run_id, type(exc).__name__)
            stream = await db_subs.get_run(run_id)
            if stream is not None and stream["outcome"] == "delivering":
                # Its push is under way: what was sent is not sent again, and the window is not read twice.
                await db_subs.settle_sending(run_id)
                await self._finish_push(run_id, None)
            else:
                await self._end(run_id, status="failed", outcome="failed", error=f"{type(exc).__name__}: {exc}")
            return False
        finally:
            self.active.discard(run_id)

    async def _execute(self, run_id: str) -> bool:
        run = await db_schedules.get_run(run_id)
        schedule = await db_schedules.get_schedule(run["schedule_id"]) if run else None
        stream = await db_subs.get_run(run_id)
        if run is None or schedule is None or stream is None or run["status"] != db_schedules.RUNNING:
            return False
        if subscriptions.is_extraction(schedule["subscription"]):
            pool = db_connection.get_pool()
            snapshot = run.get("execution_snapshot") or {}
            planned = extraction.pinned(schedule, snapshot) if snapshot.get("subscription") else schedule
            picked = await db_subs.extraction_window(
                pool, planned["subscription"], output_kind=subscriptions.output_kind(planned["subscription"]),
                from_seq=stream["from_seq"], to_seq=stream["to_seq"], now=run["fired_at"],
                own_apps=await self._own_apps())
            snapshot = {**snapshot, "config_epoch": stream["config_epoch"]} if snapshot else None
            return await extraction.execute(self, run_id, planned, stream, {**picked, "snapshot": snapshot})
        await self._push_run(run_id, run, schedule, stream)
        return False

    async def _push_run(self, run_id: str, run: Mapping[str, Any], schedule: Mapping[str, Any],
                        stream: Mapping[str, Any]) -> None:
        # The run selects, renders and writes with the configuration it started with (``subscription_runs.config``): a
        # subscription changed meanwhile, and changed back, does not make it read what it was not meant to.
        sub = {**schedule["subscription"], **(stream["config"] or {}), "window": stream["window_mode"]}
        pool = db_connection.get_pool()
        picked = await db_subs.window(pool, sub, from_seq=stream["from_seq"], to_seq=stream["to_seq"],
                                      now=run["fired_at"], own_apps=await self._own_apps(),
                                      matcher=await _matcher(pool, sub))
        if not picked["rows"]:  # every message of it was deleted since the run was planned
            await self._end(run_id, status="completed", outcome="quiet", summary="the window's messages were deleted",
                            advance=True)
            return
        text = subscriptions.processor_input(picked["rows"], text_max_chars=sub["text_max_chars"],
                                             zone_name=schedule["timezone"], render=sub["render"], rules=sub["rules"])
        if self._runtime_root is None:
            await self._end(run_id, status="failed", outcome="failed", error="this server cannot run a processor")
            return
        timeout = (schedule["run_timeout_seconds"] if schedule["run_timeout_mode"] == "seconds"
                   else processor.PROCESSOR_TIMEOUT_SEC)
        reference = {"kind": "subscription_window", "schedule_id": schedule["id"], "run_id": run_id,
                     "from_seq": stream["from_seq"], "to_seq": stream["to_seq"], "events": len(picked["rows"])}
        result = model_call.processor_result(await model_call.call(model_call.ProcessorCall(
            owner=schedule["id"], instructions=schedule["requirement"], input_text=text, input_ref=reference,
            quiet_word=sub["quiet_word"], budget_usd=schedule["run_budget_usd"], timeout_s=float(timeout),
            runtime_root=self._runtime_root, claude_bin=self._claude_bin, project_id=schedule["project_id"],
            schedule_id=schedule["id"],
            metadata={"subscription": {**reference, "render_sha256": subscriptions.digest(text)}},
            role=AgentType.PROCESSOR), parse=str))
        await db_subs.update_run(pool, run_id, record_id=result.record_id, events_selected=len(picked["rows"]))
        if result.failure is processor.ProcessorFailure.BLOCKED:
            # Blocked every time it is sent: kept, the window would be sent again, and blocked, with every run after.
            reason = extraction.SkipReason.BLOCKED_BY_HOOK
            await self._end(run_id, status="completed", outcome="skipped", cost=result.cost_usd, advance=True,
                            summary=extraction.SKIPPED_SUMMARY[reason], skip_reason=reason)
            return
        if result.failure is not None:
            status = "timed_out" if result.failure is processor.ProcessorFailure.TIMEOUT else "failed"
            await self._end(run_id, status=status, outcome="failed", cost=result.cost_usd,
                            error=f"the processor gave no reply ({result.failure.value}): {result.detail}")
            return
        reply = result.reply or ""
        if subscriptions.is_quiet(reply, sub["quiet_word"]):
            # What a rule says must never be quiet is listed by code, as the processor's own output would be pushed.
            reply = subscription_rules.fallback_text(picked["rows"], sub["rules"]) if sub["rules"] else ""
            if not reply:
                await self._end(run_id, status="completed", outcome="quiet", cost=result.cost_usd, advance=True,
                                summary="没有内容（处理器回复了静默词）")
                return
        header = sub["output_header"]
        if sub["rules"]:
            header = subscription_rules.format_header(header, subscription_rules.counts(picked["rows"], sub["rules"]),
                                                     len(picked["rows"]))
        if "{date}" in header:
            local_date = stream["daily_date"] or run["scheduled_for"].astimezone(cron.zone(schedule["timezone"])).date()
            header = header.replace("{date}", local_date.isoformat())
        message = subscriptions.push_text(reply, header)
        if not message:
            await self._end(run_id, status="failed", outcome="output_invalid", cost=result.cost_usd,
                            error="the reply was empty once its @ marks were removed")
            return
        if stream["config_version"] and result.profile is not None and (
                await subscriptions.config_version(schedule, profile=result.profile) != stream["config_version"]):
            # The processor launched with a Profile other than the one the operator confirmed (edited meanwhile).
            await self._end(run_id, status="completed", outcome="dashboard_only", cost=result.cost_usd, advance=True,
                            summary=f"{message}\n\n(kept here: the processor's Profile changed since the target was "
                                    "confirmed)", output=message)
            return
        if not stream["target_channel"] or not stream["config_version"]:  # not confirmed when it started
            await self._end(run_id, status="completed", outcome="dashboard_only", cost=result.cost_usd, advance=True,
                            summary=message, output=message)
            return
        await db_schedules.update_run(pool, run_id, cost_usd=result.cost_usd)
        async with db_connection.transaction() as conn:
            await db_subs.update_run(conn, run_id, outcome="delivering", output_text=message,
                                     output_sha256=subscriptions.digest(message))
            await db_subs.create_deliveries(conn, run_id, subscriptions.push_parts(message))
        await self._push(run_id)

    async def _refusal(self, run_id: str) -> str | None:
        """Why the run may not push now, read afresh before every message: its target is not the one it started with,
        it is not confirmed for the configuration the run started under, the schedule no longer notifies, or the
        target is no longer allowed (the sender left the allowlist, the group binding is gone). None: it may."""
        from .schedules import notify_targets

        stream = await db_subs.get_run(run_id)
        run = await db_schedules.get_run(run_id)
        schedule = await db_schedules.get_schedule(run["schedule_id"]) if run else None
        if schedule is None or stream is None:
            return "the schedule is gone"
        sub = schedule["subscription"]
        pinned = (stream["target_channel"], stream["target_chat"])
        if stream["config_epoch"] is not None and sub["config_epoch"] != stream["config_epoch"]:
            return "the subscription was changed since the run started (even if changed back)"
        try:  # what the run selected may not go to a target that is not the operator's own direct chat
            subscriptions.check_private_target({**sub, **(stream["config"] or {})}, *pinned,
                                               await db_subs.source_user(db_connection.get_pool(), sub["source_id"]))
        except ValueError:
            return "it can select private messages and its target is not your own direct chat"
        if (schedule.get("notify_channel"), schedule.get("notify_chat")) != pinned:
            return "its push target was changed"
        if schedule["notify"] == "never":
            return "the schedule no longer notifies"
        if sub["target_confirmed_at"] is None or sub.get("target_confirmed_version") != stream["config_version"]:
            return "its push target is no longer confirmed"
        if await subscriptions.config_version(schedule) != stream["config_version"]:
            return "its configuration changed since it started"
        if pinned not in {(t["channel"], t["chat"]) for t in await notify_targets()}:
            return "its push target is no longer allowed or bound"
        return None

    async def _delivery_decision(self, run_id: str, channel: str, chat: str) -> delivery.Decision:
        """One decision and one daily slot for the whole digest, shared by its segments and their retries."""
        from .schedules import notify_target_kind

        pool = db_connection.get_pool()
        channel_id = builtin_channel_id(channel)
        routing = await pool.fetchrow(
            "SELECT c.manager_id AS owner, coalesce(b.manager_id, c.manager_id) AS manager "
            "FROM channel_configs c LEFT JOIN manager_chat_bindings b "
            "ON b.channel_id = c.channel_id AND b.chat_id = $2 WHERE c.channel_id = $1", channel_id, chat)
        session = channel_thread_id(channel_id, chat, routing["manager"], routing["owner"])
        last_user = await pool.fetchval(
            "SELECT created_at FROM chat_messages WHERE session_id = $1 AND role = 'user' "
            "ORDER BY created_at DESC LIMIT 1", session)
        observed = self._recent_lark_users.get((channel_id, chat))
        if observed is not None and (last_user is None or observed > last_user):
            last_user = observed
        schedule_id = (await db_schedules.get_run(run_id))["schedule_id"]
        chat_kind = (delivery.ChatKind.GROUP if await notify_target_kind(channel, chat) == "group"
                     else delivery.ChatKind.DIRECT)
        return await delivery.decide(
            delivery.Delivery(delivery.DeliveryKind.SUBSCRIPTION, delivery.Urgency.DIGEST, chat_kind),
            dedupe_key=f"subscription_run:{run_id}", channel_id=channel_id, chat=chat,
            last_user_message_at=last_user, schedule_id=schedule_id, now=self._clock())

    async def _push(self, run_id: str) -> None:
        """Push the run's saved output a message at a time (module notes): each is claimed (``sending``, committed)
        before the channel is asked and is never sent again unless the channel answered that it did not send it; one
        that was being sent when a process died is ``uncertain`` and is left. Ends the run."""
        stream = await db_subs.get_run(run_id)
        await db_subs.settle_sending(run_id)
        channel, chat = stream["target_channel"], stream["target_chat"]
        refused: str | None = None
        decided: delivery.Decision | None = None
        for first in await db_subs.list_deliveries(run_id):
            segment = first["segment"]
            while True:
                row = next(d for d in await db_subs.list_deliveries(run_id) if d["segment"] == segment)
                if row["state"] not in ("pending", "failed") or row["attempts"] >= db_subs.DELIVERY_ATTEMPTS:
                    break
                if row["state"] == "failed":
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                if (refused := await self._refusal(run_id)) is not None:
                    break
                if decided is None and chat_settings.delivery_enabled():
                    decided = await self._delivery_decision(run_id, channel, chat)
                    if decided.outcome is not delivery.Outcome.SEND:
                        refused = f"delivery {decided.outcome.value}: {decided.reason.value}"
                        logger.info("subscription run %s: %s", run_id, refused)
                        break
                if not await db_subs.claim_segment(run_id, segment):
                    break
                try:
                    if self._poster is None:
                        raise _NoPoster
                    receipt = await self._poster(builtin_channel_id(channel), push_target(channel, chat), row["text"],
                                                 idempotency_key=subscriptions.idempotency_keys(
                                                     run_id, segment)[segment - 1],
                                                 **turn_metrics.proactive_kwargs(
                                                     turn_metrics.ProactiveSource.SUBSCRIPTION, run_id,
                                                     dedupe_key=f"subscription_run:{run_id}" if decided is not None else None))
                except _NoPoster:
                    await db_subs.mark_segment(run_id, segment, "failed", error="this server cannot send messages")
                except Exception as exc:  # whether the channel sent it is not known: it is not sent again
                    logger.warning("subscription run %s message %d: uncertain (%s)", run_id, segment, type(exc).__name__)
                    await db_subs.mark_segment(run_id, segment, "uncertain", error=f"{type(exc).__name__}: {exc}")
                else:
                    if receipt.result is PostOutcome.SENT:
                        await db_subs.mark_segment(run_id, segment, "sent", external_ids=receipt.external_ids)
                        if decided is not None:
                            try:
                                await db_deliveries.record_receipt(
                                    f"subscription_run:{run_id}", "sent",
                                    receipt.external_ids[0] if receipt.external_ids else None)
                            except Exception:
                                # The channel took it: a failed metrics receipt cannot undo or repeat that send.
                                logger.exception("subscription run %s: delivery receipt was not recorded", run_id)
                    elif receipt.result is PostOutcome.REJECTED:  # the platform said no: asking again is safe
                        await db_subs.mark_segment(run_id, segment, "failed", error=receipt.error or "not sent")
                    else:  # it may have been accepted (a lost answer): it is never sent again
                        logger.warning("subscription run %s message %d: uncertain (no answer)", run_id, segment)
                        await db_subs.mark_segment(run_id, segment, "uncertain", error=receipt.error or "no answer")
            if refused is not None:
                break
        await self._finish_push(run_id, refused)

    async def _finish_push(self, run_id: str, refused: str | None) -> None:
        rows = await db_subs.list_deliveries(run_id)
        stream = await db_subs.get_run(run_id)
        message = stream["output_text"]
        states = [d["state"] for d in rows]
        out = any(state in ("sent", "uncertain") for state in states)
        pushed = (stream["target_channel"], stream["target_chat"]) if "sent" in states else None
        if "uncertain" in states:
            logger.warning("subscription run %s: %d message(s) of its push are uncertain and will not be sent again",
                           run_id, states.count("uncertain"))
        if refused is not None:
            await self._end(run_id, status="completed", outcome="dashboard_only", advance=True, output=message,
                            summary=f"{message}\n\n(the rest stayed on the dashboard: {refused})", pushed=pushed)
        elif any(state in ("pending", "failed", "sending") for state in states):
            failed = next(d for d in rows if d["state"] in ("pending", "failed"))
            await self._end(run_id, status="failed", outcome="failed", advance=out, output=message, pushed=pushed,
                            error=f"the push to {stream['target_channel']} {stream['target_chat']} failed at message "
                                  f"{failed['segment']} of {len(rows)}: {failed['error'] or 'not sent'}")
        else:
            await self._end(run_id, status="completed", outcome="uncertain" if "uncertain" in states else "delivered",
                            advance=True, output=message, summary=message, pushed=pushed)

    # --- Ending ---

    async def _end(self, run_id: str, *, status: str, outcome: str, cost: float | None = None,
                   error: str | None = None, summary: str | None = None, advance: bool = False,
                   output: str | None = None, pushed: tuple[str, str] | None = None,
                   skip_reason: extraction.SkipReason | None = None) -> None:
        """End the run in one transaction: its status, its outcome and output (and why a ``skipped`` run skipped its
        window), the push's notification, and, when it succeeded, the cursor. A failed or interrupted attempt with
        nothing sent or uncertain releases its daily claim."""
        now = self._clock()
        async with db_connection.transaction() as conn:
            run = await db_schedules.get_run(run_id, pool=conn)
            stream = await db_subs.get_run(run_id, db=conn)
            if run is None or stream is None or run["status"] != db_schedules.RUNNING:
                return
            fields: dict[str, Any] = {"outcome": outcome}
            if skip_reason is not None:
                fields["skip_reason"] = skip_reason.value
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
            if not advance and stream["daily_date"] is not None:
                await conn.execute("DELETE FROM subscription_daily_claims WHERE schedule_id = $1 AND local_date = $2",
                                   run["schedule_id"], stream["daily_date"])
            if advance and stream["window_mode"] == "cursor":
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
            self.active.add(run["id"])
            try:
                await self._push(run["id"])
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

