"""Deliver intent notices only to the operator, with committed, at-most-once claims.

A session advisory lock survives the claim's commit and covers the network call. Another server cannot mistake an
active send for a crash; after the session disappears, its remaining ``sending`` row becomes ``uncertain``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..db import connection, intents as db_intents, proactive_deliveries as db_deliveries, schedules as db_schedules
from ..db import subscriptions as db_subs
from . import chat_settings, intent_cards, leads_model, proactive_delivery as delivery, subscriptions, turn_metrics
from .channels.base import PostOutcome
from .message import builtin_channel_id, channel_thread_id, parse_channel_id
from .operator_identity import operator_sender_id
from .subscription_runs import push_target

logger = logging.getLogger(__name__)
TICK_SECONDS = 10.0
EXPIRY_SECONDS = 300.0
FINAL_RETRY_SECONDS = 30
FINAL_RETRY_MAX_SECONDS = 300
PASS_LIMIT = 50
_LOCK_NAMESPACE = 741923
_active: IntentPushService | None = None


def _final_retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(FINAL_RETRY_SECONDS * 2**max(attempts-1, 0), FINAL_RETRY_MAX_SECONDS))


def _source_line(view: Mapping[str, Any], timezone: str) -> str:
    if not view["sources"]:
        return "来源："
    source = view["sources"][0]
    label = "私聊" if source["chat_kind"] == "p2p" else f"《{source['chat']}》"
    sent = datetime.fromisoformat(source["sent_at"]).astimezone(ZoneInfo(timezone)).strftime("%m-%d %H:%M")
    link = f"（{source['link']}）" if source["link"] else ""
    count = f"等 {view['sources_total']} 条" if view["sources_total"] >= 2 else ""
    return f"来源：{label} · {source['sender']} · {sent}{link}{count}"


def candidates_line(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "项目：这个会话没有绑定项目，请到意图页选择。"
    return "项目：这个会话没有绑定项目。候选：" + "  ".join(
        f"{index}. {project['name']}" for index, project in enumerate(candidates, 1))


def push_text(view: Mapping[str, Any], *, priority: str, timezone: str) -> str:
    iid = view["id"]
    prefix = f"🚨 {iid} · 紧急 · " if priority == "urgent" else f"💡 {iid} · "
    lines = [f"{prefix}{view['type']['name']}（置信度：{leads_model.CONFIDENCE_ZH[view['confidence']]}）",
             view["title"], view["summary"], _source_line(view, timezone)]
    if view["project"]:
        workflow = view.get("effective_workflow")
        lines.append(f"项目：{view['project']['name']}（模板：{workflow['name'] if workflow else '项目默认'}）")
        reply = f"回复「建」或「建 {iid}」建任务并开始；「驳回 {iid}」驳回。24 小时没处理会过期。"
    else:
        candidates = view.get("candidates", [])
        lines.append(candidates_line(candidates))
        reply = (f"回复「建 {iid} <编号>」用对应的项目建任务并开始；「驳回 {iid}」驳回。24 小时没处理会过期。"
                 if candidates else f"「驳回 {iid}」驳回；建任务请到意图页。")
    if view["flagged"]:
        lines.append("⚠️ 来源消息里有要求助手或 agent 做事的话，建之前先看一下原消息。")
    return subscriptions.strip_mentions("\n".join([*lines, reply]))


def progress_text(view: Mapping[str, Any], target: Mapping[str, Any], *, timezone: str) -> str:
    iid = target["id"]
    first = (f"🔄 {iid}（已建 {target['task']['id']}）新进展" if target["state"] == "tasked" and target["task"]
             else f"🔄 {iid} 新进展")
    lines = [first, view["summary"], _source_line(view, timezone)]
    if target["state"] == "pending":
        lines.append(f"回复「建 {iid}」建任务并开始；「驳回 {iid}」驳回。")
    return subscriptions.strip_mentions("\n".join(lines))


def final_text(view: Mapping[str, Any], *, timezone: str) -> str:
    iid, name = view["id"], view["type"]["name"]
    first = {"tasked": f"✅ {iid} 已建 {(view['task'] or {}).get('id', '')} · {name}",
             "rejected": f"❌ {iid} 已驳回 · {name}",
             "expired": f"⌛ {iid} 已过期（没有处理） · {name}"}[view["state"]]
    return subscriptions.strip_mentions("\n".join([first, view["title"], view["summary"], _source_line(view, timezone)]))


async def final_edit(intent_id: int) -> None:
    """Best effort after a committed operator transition; the poller also repairs missed final notices."""
    if _active is not None:
        try:
            await _active.final_edit(intent_id)
        except Exception:
            logger.warning("intent %s final notice deferred", intent_id, exc_info=True)


async def target_for(schedule: Mapping[str, Any] | str | None) -> tuple[str, str] | None:
    """A currently confirmed operator DM, or none. Rechecked for each delivery and edit."""
    from .schedules import notify_targets

    if isinstance(schedule, str):
        schedule = await db_schedules.get_schedule(schedule)
    if schedule is None or not await leads_model.intents_enabled():
        return None
    sub = schedule.get("subscription") or {}
    channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
    if (sub.get("output_kind") != "intents" or not channel or not chat or schedule.get("notify") == "never"
            or not sub.get("target_confirmed_at")
            or sub.get("target_confirmed_version") != await subscriptions.config_version(schedule)):
        return None
    # An explicit channel id may identify a configured bot; channel types identify their built-in bot.
    channel_id = channel if parse_channel_id(channel) is not None else builtin_channel_id(channel)
    if chat != await operator_sender_id(channel_id) or push_target(channel, chat).group:
        return None
    if (channel, chat) not in {(target["channel"], target["chat"]) for target in await notify_targets()}:
        return None
    return channel_id, chat


class IntentPushService:
    def __init__(self, registry: Any, *, drain: Any = None, clock: Callable[[], datetime] | None = None,
                 interval: float = TICK_SECONDS) -> None:
        self._registry = registry
        self._drain = drain
        self._clock = clock or (lambda: datetime.now(dt_timezone.utc))
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._last_expiry: datetime | None = None
        self._last_intent_id = 0

    async def start(self) -> None:
        global _active
        _active = self
        self._task = asyncio.create_task(self._loop(), name="intent-push")

    async def stop(self) -> None:
        global _active
        if _active is self:
            _active = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
                now = self._clock()
                if self._last_expiry is None or (now-self._last_expiry).total_seconds() >= EXPIRY_SECONDS:
                    await self.expire()
                    self._last_expiry = now
            except Exception:
                logger.warning("intent push loop failed; trying again later", exc_info=True)
            await asyncio.sleep(self._interval)

    async def run_once(self) -> None:
        if (self._drain is not None and self._drain.draining) or not await leads_model.intents_enabled():
            return
        rows = await connection.get_pool().fetch(
            "SELECT id FROM intents WHERE id > $3 AND ((state IN ('pending','merged') AND created_at >= $1) "
            "OR (state IN ('tasked','rejected','expired') AND EXISTS "
            "(SELECT 1 FROM intent_posts p LEFT JOIN intent_final_notices n ON n.push_id=p.id "
            "WHERE p.intent_id=intents.id AND p.kind='push' AND p.state='sent' AND "
            "((n.push_id IS NULL AND (p.edited_at IS NULL OR p.edited_at < intents.handled_at OR EXISTS "
            "(SELECT 1 FROM intent_posts f WHERE f.intent_id=intents.id AND f.kind='final' "
            "AND f.updated_at >= intents.handled_at AND f.state IN ('pending','failed','uncertain')))) "
            "OR n.handled_at <> intents.handled_at OR (n.state='pending' AND n.next_attempt_at <= $2)))) "
            ") ORDER BY id LIMIT $4", self._clock()-timedelta(hours=24), self._clock(), self._last_intent_id, PASS_LIMIT)
        for row in rows:
            await self._locked(row["id"])
        # Pending intents stay selectable after a send; continue past them before scanning from the oldest again.
        self._last_intent_id = rows[-1]["id"] if len(rows) == PASS_LIMIT else 0

    async def final_edit(self, intent_id: int) -> None:
        if await leads_model.intents_enabled():
            await self._locked(intent_id)

    async def _locked(self, intent_id: int) -> None:
        async with connection.get_pool().acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", _LOCK_NAMESPACE,
                                       intent_id % 2147483647):
                return
            try:
                await self._process(conn, intent_id)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1::int, $2::int)", _LOCK_NAMESPACE,
                                   intent_id % 2147483647)

    async def _process(self, conn: Any, intent_id: int) -> None:
        from . import intents

        row = await conn.fetchrow("SELECT * FROM intents WHERE id=$1", intent_id)
        if row is None:
            return
        held = (row["via"] == "on_request"
                or row["via"] == "followed_group" and not chat_settings.delivery_enabled())
        schedule = await db_schedules.get_schedule(row["schedule_id"], pool=conn) if row["schedule_id"] else None
        target = await target_for(schedule)
        if target is None:
            await self._defer_final_notice(conn, intent_id)
            return
        channel, chat = target
        now = self._clock()
        # A never-delivered suggestion still has a lifetime. Successful pushes/progress extend it below.
        if row["state"] == "pending" and row["expires_at"] is None:
            await conn.execute("UPDATE intents SET expires_at=created_at+interval '24 hours' "
                               "WHERE id=$1 AND state='pending' AND expires_at IS NULL", intent_id)
        # The session lock rules out a live writer. A leftover claim has an unknowable delivery result.
        await conn.execute("UPDATE intent_posts SET state='uncertain', error='the sender stopped before recording its answer', "
                           "updated_at=$2 WHERE intent_id=$1 AND state='sending'", intent_id, now)
        posts = await conn.fetch("SELECT * FROM intent_posts WHERE intent_id=$1 ORDER BY id", intent_id)
        view = await intents.intent_view(row, db=conn)
        timezone = schedule["timezone"]
        if not held and row["state"] == "pending" and not any(post["kind"] == "push" for post in posts):
            priority = await conn.fetchval("SELECT v.priority FROM intent_types t JOIN intent_type_versions v "
                                           "ON v.type_key=t.key AND v.version=t.version WHERE t.key=$1", row["type_key"])
            if priority != "quiet" and row["created_at"] >= now-timedelta(hours=24):
                text = push_text(view, priority=priority, timezone=timezone)
                await self._insert(conn, row["id"], "push", channel, chat, text,
                                   [candidate["id"] for candidate in view["candidates"]])
        elif not held and row["state"] == "merged" and not any(post["kind"] == "progress" for post in posts):
            parent = await conn.fetchrow("SELECT * FROM intents WHERE id=$1", row["merged_into"])
            push = await conn.fetchrow("SELECT * FROM intent_posts WHERE intent_id=$1 AND kind='push' "
                                      "AND ((state='sent' AND external_id IS NOT NULL) OR (state='pending' AND $2)) "
                                      "ORDER BY id DESC LIMIT 1", row["merged_into"], chat_settings.delivery_enabled())
            if (parent and parent["state"] in ("pending", "tasked") and push
                    and (push["channel_id"], push["chat"]) == target and row["created_at"] >= now-timedelta(hours=24)):
                text = progress_text(view, await intents.intent_view(parent, db=conn), timezone=timezone)
                await self._insert(conn, intent_id, "progress", channel, chat, text)
        elif row["state"] in ("tasked", "rejected", "expired"):
            push = next((post for post in posts if post["kind"] == "push" and post["state"] == "sent"
                         and post["external_id"] and (post["channel_id"], post["chat"]) == target), None)
            if push is not None:
                await self._finalize(conn, row, push, posts, schedule, final_text(view, timezone=timezone))
        for post in await conn.fetch("SELECT * FROM intent_posts WHERE intent_id=$1 AND state IN ('pending','failed') "
                                     "AND kind <> 'final' AND attempts < $2 ORDER BY id", intent_id, db_subs.DELIVERY_ATTEMPTS):
            if held and post["kind"] in ("push", "progress"):
                continue
            if (post["channel_id"], post["chat"]) == target:
                await self._deliver(conn, row, post, schedule)

    async def _insert(self, conn: Any, intent_id: int, kind: str, channel: str, chat: str, text: str,
                      candidates: list[str] | None = None) -> int:
        # Each insert/claim is autocommitted before network I/O, while the session lock serializes instances.
        return await conn.fetchval("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,candidates) "
                                   "VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
                                   intent_id, kind, channel, chat, text, candidates or [])

    async def _defer_final_notice(self, conn: Any, intent_id: int) -> None:
        now = self._clock()
        notice = await conn.fetchrow("SELECT n.* FROM intent_final_notices n JOIN intent_posts p ON p.id=n.push_id "
                                     "JOIN intents i ON i.id=p.intent_id WHERE i.id=$1 AND n.handled_at=i.handled_at "
                                     "AND n.state='pending' AND n.next_attempt_at <= $2", intent_id, now)
        if notice is not None:
            # An invalid target spends no delivery attempt, but must not stay due on every tick.
            await conn.execute("UPDATE intent_final_notices SET next_attempt_at=$2,updated_at=$3 WHERE push_id=$1",
                               notice["push_id"], now+_final_retry_delay(notice["attempts"]), now)

    async def _finalize(self, conn: Any, intent: Mapping[str, Any], push: Mapping[str, Any],
                        posts: list[Any], schedule: Any, text: str) -> None:
        now = self._clock()
        notice = await conn.fetchrow("SELECT * FROM intent_final_notices WHERE push_id=$1", push["id"])
        if notice is None or notice["handled_at"] != intent["handled_at"]:
            # Released versions set edited_at even on failure. Adopt their unfinished fallback once,
            # including exhausted replies; the new notice owns its bounded retry budget thereafter.
            legacy_edited = (notice is None and push["edited_at"] is not None
                             and push["edited_at"] >= intent["handled_at"])
            legacy = next((p for p in reversed(posts) if p["kind"] == "final"
                           and p["updated_at"] >= intent["handled_at"]), None) if legacy_edited else None
            if legacy_edited and (legacy is None or legacy["state"] == "sent"):
                return
            notice = await conn.fetchrow(
                "INSERT INTO intent_final_notices(push_id,handled_at,final_post_id,next_attempt_at,updated_at) "
                "VALUES($1,$2,$3,$4,$4) ON CONFLICT(push_id) DO UPDATE SET handled_at=$2,final_post_id=$3,"
                "state='pending',attempts=0,next_attempt_at=$4,failure_reason=NULL,updated_at=$4 RETURNING *",
                push["id"], intent["handled_at"], legacy["id"] if legacy else None, now)
        if notice["state"] != "pending" or notice["next_attempt_at"] > now:
            return
        channel, chat = push["channel_id"], push["chat"]
        schedule = await db_schedules.get_schedule(schedule["id"], pool=conn)
        if await target_for(schedule) != (channel, chat):
            await self._defer_final_notice(conn, intent["id"])
            return
        final = await conn.fetchrow("SELECT * FROM intent_posts WHERE id=$1", notice["final_post_id"]) \
            if notice["final_post_id"] else None
        success = final is not None and final["state"] == "sent"
        attempts = notice["attempts"]
        if not success and attempts < db_subs.DELIVERY_ATTEMPTS:
            attempts += 1
            # Persist the attempt and its backoff before I/O; the session lock excludes other senders.
            await conn.execute("UPDATE intent_final_notices SET attempts=$2,next_attempt_at=$3,updated_at=$4 "
                               "WHERE push_id=$1", push["id"], attempts, now+_final_retry_delay(attempts), now)
            try:
                card = await intent_cards.final_card(intent["id"], channel, chat, push["external_id"], text, conn=conn)
                receipt = (await self._registry.edit_card(channel, push_target(channel, chat), push["external_id"], card)
                           if card is not None else
                           await self._registry.edit(channel, push_target(channel, chat), push["external_id"], text))
                success = receipt.success
            except Exception:
                success = False
            if not success:
                if final is None:
                    async with conn.transaction():
                        post_id = await self._insert(conn, intent["id"], "final", channel, chat, text.splitlines()[0])
                        await conn.execute("UPDATE intent_final_notices SET final_post_id=$2 WHERE push_id=$1",
                                           push["id"], post_id)
                    final = await conn.fetchrow("SELECT * FROM intent_posts WHERE id=$1", post_id)
                # Unknown deliveries never resend. Retrying the idempotent edit can still repair the notice.
                if final["state"] in ("pending", "failed"):
                    await self._deliver(conn, intent, final, schedule)
                    success = await conn.fetchval("SELECT state='sent' FROM intent_posts WHERE id=$1", final["id"])
        state = "sent" if success else "failed" if attempts >= db_subs.DELIVERY_ATTEMPTS else "pending"
        reason = db_intents.FinalNoticeFailure.RETRY_EXHAUSTED if state == "failed" else None
        async with conn.transaction():
            await conn.execute("UPDATE intent_final_notices SET state=$2,failure_reason=$3,updated_at=$4 WHERE push_id=$1",
                               push["id"], state, reason, now)
            if success:
                await conn.execute("UPDATE intent_posts SET text=$2,edited_at=$3,updated_at=$3 WHERE id=$1",
                                   push["id"], text, max(now, intent["handled_at"]))
        if reason:
            logger.warning("intent %s final notice: %s", intent["id"], reason)

    async def _deliver(self, conn: Any, intent: Mapping[str, Any], post: Mapping[str, Any], schedule: Any) -> None:
        # Re-read the schedule: confirmation, identity and allowlist may have changed since another post.
        schedule = await db_schedules.get_schedule(schedule["id"], pool=conn)
        if await target_for(schedule) != (post["channel_id"], post["chat"]):
            return
        if post["kind"] == "push":
            current = await conn.fetchval("SELECT state FROM intents WHERE id=$1", intent["id"])
            if current != "pending":
                return
        reply = None
        if post["kind"] in ("progress", "final"):
            parent_id = intent["merged_into"] if post["kind"] == "progress" else intent["id"]
            if post["kind"] == "progress":
                state = await conn.fetchval("SELECT state FROM intents WHERE id=$1", parent_id)
                if state not in ("pending", "tasked"):
                    return
            reply = await conn.fetchval("SELECT external_id FROM intent_posts WHERE intent_id=$1 AND kind='push' "
                                        "AND state='sent' ORDER BY id DESC LIMIT 1", parent_id)
        now = self._clock()
        decided = None
        delivery_key = f"intent-{intent['id']}-{post['id']}"
        # Final edits and their fallback replies report an already handled intent, not a new suggestion.
        if post["kind"] in ("push", "progress") and chat_settings.delivery_enabled():
            try:
                # The unsuffixed bot-owner conversation, as for the native incoming DM.
                thread = channel_thread_id(post["channel_id"], post["chat"], "manager", "manager")
                spoke = await conn.fetchval("SELECT max(created_at) FROM chat_messages "
                                            "WHERE session_id=$1 AND role='user'", thread)
                # An intent marked urgent is neither a blocking gate nor a user-set reminder.
                decided = await delivery.decide(
                    delivery.Delivery(delivery.DeliveryKind.INTENT, delivery.Urgency.NORMAL, delivery.ChatKind.DIRECT),
                    dedupe_key=delivery_key, channel_id=post["channel_id"], chat=post["chat"],
                    last_user_message_at=spoke, source_ref=str(intent["id"]), schedule_id=schedule["id"], now=now)
            except Exception:
                logger.warning("intent post %s delivery decision failed; waiting to retry", post["id"], exc_info=True)
                return
            if decided.outcome is not delivery.Outcome.SEND:
                return
        # Record new evidence for the briefing even while its parent is deferred; sending needs its receipt.
        if post["kind"] in ("progress", "final") and not reply:
            return
        claimed = await conn.fetchval("UPDATE intent_posts SET state='sending', attempts=attempts+1, updated_at=$2 "
                                      "WHERE id=$1 AND state IN ('pending','failed') "
                                      "AND (kind='final' OR attempts<$3) RETURNING id",
                                      post["id"], now, db_subs.DELIVERY_ATTEMPTS)
        if claimed is None:
            return
        external = error = None
        card = None
        try:
            card = (await intent_cards.make(intent, post["channel_id"], post["chat"], post["text"], conn=conn)
                    if post["kind"] == "push" else None)
            target = push_target(post["channel_id"], post["chat"])
            kwargs = {"reply_to": reply, "idempotency_key": delivery_key,
                      **turn_metrics.proactive_kwargs(turn_metrics.ProactiveSource.INTENT, str(intent["id"]))}
            receipt = (await self._registry.post_card(post["channel_id"], target, card, **kwargs) if card is not None
                       else await self._registry.post(post["channel_id"], target, post["text"], **kwargs))
            state = {PostOutcome.SENT: "sent", PostOutcome.REJECTED: "failed", PostOutcome.UNKNOWN: "uncertain"}[receipt.result]
            external = receipt.external_ids[0] if receipt.external_ids else None
            error = receipt.error
        except Exception as exc:
            state, error = "uncertain", type(exc).__name__
        await conn.execute("UPDATE intent_posts SET state=$2, external_id=$3, error=$4, updated_at=$5 WHERE id=$1",
                           post["id"], state, external, error, now)
        if state == "failed" and card is not None:
            from ..db import card_actions as db_cards
            await db_cards.lapse([card.token], pool=conn)
        if decided is not None:
            try:
                # Retries refresh the receipt on the decided row, as the channel metrics do.
                await db_deliveries.insert({
                    "dedupe_key": delivery_key, "source": delivery.DeliveryKind.INTENT.value,
                    "source_ref": str(intent["id"]), "channel_id": post["channel_id"], "chat": post["chat"],
                    "chat_kind": delivery.ChatKind.DIRECT.value, "external_id": external,
                    "outcome": {"sent": "sent", "failed": "rejected", "uncertain": "unknown"}[state]})
            except Exception:
                logger.warning("intent post %s delivery receipt not recorded", post["id"], exc_info=True)
        if state == "sent":
            parent_id = intent["merged_into"] if post["kind"] == "progress" else intent["id"]
            if post["kind"] in ("push", "progress"):
                await conn.execute("UPDATE intents SET expires_at=$2, updated_at=$3 WHERE id=$1 AND state='pending'",
                                   parent_id, now+timedelta(hours=24), now)
        logger.info("intent %s %s post %s: %s", intent["id"], post["kind"], post["id"], state)

    async def expire(self) -> None:
        from ..db.intents import transition, IntentRefused

        if not await leads_model.intents_enabled() or (self._drain is not None and self._drain.draining):
            return
        now = self._clock()
        rows = await connection.get_pool().fetch("SELECT id FROM intents WHERE state='pending' "
                                                 "AND COALESCE(expires_at,created_at+interval '24 hours') <= $1 "
                                                 "ORDER BY id LIMIT $2", now, PASS_LIMIT)
        for row in rows:
            try:
                async with connection.transaction() as conn:
                    current = await conn.fetchrow("SELECT * FROM intents WHERE id=$1 FOR UPDATE", row["id"])
                    if (current and current["state"] == "pending"
                            and (current["expires_at"] or current["created_at"]+timedelta(hours=24)) <= now):
                        await transition(conn, row["id"], "expired", via="system", now=now)
            except IntentRefused:
                continue
            await self.final_edit(row["id"])
