"""Source collection alerts: durable episodes, bot delivery, and read-only projections."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from ..db import connection, message_parity, message_stream_alerts as store
from . import chat_settings, operator_identity, proactive_delivery, subscriptions, turn_metrics
from .channels.base import PostOutcome
from .message import builtin_channel_id, channel_thread_id, parse_channel_id
from .task_notice import NotifyTarget, TargetKind

logger = logging.getLogger(__name__)
_SENDER_LOCK = 759_195_001
_LABELS = {"parity_drift": "消息流对账有差异", "collector_failure": "消息流采集失败", "stale": "消息流一小时未成功采集",
           "auth": "消息流登录需处理", "auth_expiring": "消息流登录两天内到期", "partial": "消息流连续三轮未完成"}
_FIX = {"parity_drift": "查看同一窗口的覆盖和差异；修复后重新采集该窗口。",
        "collector_failure": "查看采集运行的错误类型并重试。", "stale": "检查采集运行、进程和登录状态。",
        "auth": "核对绑定身份并在本机登录对应 profile；Looper 不会代登录。",
        "auth_expiring": "在本机检查对应 profile 的登录续期。", "partial": "查看未完成的会话与话题并重试。"}
_ERRORS = {"auth_missing": "auth", "auth_expired": "auth", "identity": "identity", "protocol": "protocol",
           "pagination": "pagination", "rate_limit": "rate_limit", "transient": "transient", "permission": "permission",
           "not_member": "left", "deadline": "budget", "connection": "connection", "interrupted": "interrupted",
           "configuration": "policy"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _channel_id(value: str | None) -> str | None:
    if not value:
        return None
    if parse_channel_id(value) is not None:
        return value
    return builtin_channel_id(value)


def _facts(row: Mapping[str, Any]) -> dict[str, Any]:
    view = store.view(row)
    return {"source": "message_stream", "kind": row["kind"], "state": "active" if row["resolved_at"] is None else "recovered",
            "open_state": row["open_state"], "recovery_state": row["recovery_state"],
            "session": view["parity_session_id"], "run": view["metadata"].get("run_id"),
            "window": {key: view["metadata"].get(key) for key in ("window_from", "window_to")},
            **{key: view["metadata"].get(key) for key in ("bridge_only", "collect_only", "identity_conflicts")},
            "channel": None, "fix": _FIX[row["kind"]]}


def _attention_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"key": f"message-stream:{row['id']}", "title": _LABELS[row["kind"]], "context": row["source_id"],
            "since": row["opened_at"], "href": "#messages?collection=" + quote(row["source_id"], safe=""),
            "ref": None, "facts": _facts(row)}


async def attention_items(limit: int = 20, *, since: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
    async with connection.get_pool().acquire() as conn:
        if not await store.configured(conn):
            return [], 0
        rows, count = await store.list_active(conn, since=since, limit=limit)
    return [_attention_item(row) for row in rows], count


async def earliest_active(*, since: datetime | None = None) -> dict[str, Any] | None:
    items, _ = await attention_items(1, since=since)
    return items[0] if items else None


class SourceAlerts:
    def __init__(self, post: Any = None, *, clock: Callable[[], datetime] | None = None) -> None:
        self.post = post
        self.clock = clock or _now
        self._stopped = False
        self._flush_lock = asyncio.Lock()

    async def evaluate(self, source_id: str | None = None, *, now: datetime | None = None) -> None:
        if self._stopped:
            return
        now = now or self.clock()
        async with connection.get_pool().acquire() as conn:
            sources = await conn.fetch(
                "SELECT e.*, s.last_collect_ok_at, s.collect_enabled_at FROM source_collection_settings s "
                "JOIN event_sources e ON e.id = s.source_id WHERE s.alerts_enabled "
                "AND ($1::text IS NULL OR e.id = $1)", source_id)
            for source in sources:
                await self._evaluate(conn, source, now)

    async def _evaluate(self, conn: Any, source: Mapping[str, Any], now: datetime) -> None:
        sid = source["id"]
        runs = await conn.fetch(
            "SELECT id, status, error, error_code, started_at, finished_at FROM source_runs "
            "WHERE source_id = $1 AND mode = 'collect' AND status NOT IN ('running', 'skipped_busy') "
            "ORDER BY id DESC LIMIT 3", sid)
        latest = runs[0] if runs else None
        target = {"target_channel": _channel_id(source["alert_channel"]),
                  "target_chat": source["alert_chat"]}
        metadata = ({"run_id": str(latest["id"]), "status": latest["status"],
                     "error": _ERRORS.get(latest["error"], "unknown") if latest["error"] else None,
                     "error_code": latest["error_code"]} if latest else {})

        async def open_(kind: str, *, details: Mapping[str, Any] | None = None,
                        session: int | None = None) -> None:
            await store.open_episode(conn, sid, kind, now=now, metadata=details or metadata,
                                     parity_session_id=session, **target)

        # Rollback leaves unresolved episodes visible; bridge successes do not repair collection facts.
        if source["ingest_mode"] == "collect":
            anchor = source["last_collect_ok_at"] or source["collect_enabled_at"]
            if anchor is not None and now - anchor >= timedelta(hours=1):
                await open_("stale")
            elif source["last_collect_ok_at"] is not None:
                await store.resolve_episode(conn, sid, "stale", now=now)
            # A newly verified auth result supersedes the previous failed run before the next long pass finishes.
            latest_auth_failure = latest is not None and latest["status"] == "auth_failed" and (
                source["auth_checked_at"] is None or source["auth_checked_at"] <= (latest["finished_at"] or latest["started_at"]))
            auth_bad = source["auth_state"] in {"missing", "expired"} or latest_auth_failure
            if auth_bad:
                await open_("auth")
            elif source["auth_state"] in {"ready", "needs_refresh"}:
                await store.resolve_episode(conn, sid, "auth", now=now)
            expires = source["refresh_expires_at"]
            if expires is not None and now < expires <= now + timedelta(hours=48):
                await open_("auth_expiring")
            elif expires is not None and expires > now + timedelta(hours=48):
                await store.resolve_episode(conn, sid, "auth_expiring", now=now)
            if latest is not None:
                if latest["status"] in {"search_failed", "failed"} or (
                        latest["status"] == "interrupted" and latest["error"] == "connection"):
                    await open_("collector_failure")
                elif latest["status"] == "ok":
                    await store.resolve_episode(conn, sid, "collector_failure", now=now)
                    await store.resolve_episode(conn, sid, "partial", now=now)
                if len(runs) == 3 and all(run["status"] == "partial" for run in runs):
                    await open_("partial", details={**metadata, "partials": 3})
        # A drift is resolved only by its own frozen session, never by a later, disjoint matched window.
        sessions = await conn.fetch(
            "SELECT p.id, p.state, p.window_from, p.window_to FROM source_parity_sessions p "
            "WHERE p.source_id = $1 AND (p.state = 'drift' OR EXISTS (SELECT 1 FROM source_stream_alerts a "
            "WHERE a.source_id = $1 AND a.kind = 'parity_drift' AND a.resolved_at IS NULL "
            "AND a.parity_session_id = p.id)) ORDER BY p.id", sid)
        for session in sessions:
            if session["state"] == "drift":
                if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM source_stream_alerts WHERE source_id = $1 "
                                       "AND kind = 'parity_drift' AND resolved_at IS NULL)", sid):
                    continue
                totals, _, _, _ = await message_parity.report_rows(conn, session["id"], limit=1)
                await open_("parity_drift", session=session["id"], details={
                    "window_from": session["window_from"], "window_to": session["window_to"],
                    **{key: totals[key] for key in ("bridge_only", "collect_only", "identity_conflicts")}})
            elif session["state"] == "matched":
                await store.resolve_episode(conn, sid, "parity_drift", now=now, parity_session_id=session["id"])

    async def _target_valid(self, conn: Any, row: Mapping[str, Any]) -> bool:
        channel, chat = row["target_channel"], row["target_chat"]
        parsed = parse_channel_id(channel)
        if parsed is None or not chat or parsed.channel_type not in {"lark", "lark_bot", "telegram"}:
            return False
        source = await conn.fetchrow("SELECT e.alert_channel,e.alert_chat,s.alerts_enabled FROM event_sources e "
            "JOIN source_collection_settings s ON s.source_id=e.id WHERE e.id=$1", row["source_id"])
        if source is None or not source["alerts_enabled"] or not source["alert_channel"] or (
                _channel_id(source["alert_channel"]), source["alert_chat"]) != (channel, chat):
            return False
        operator = await operator_identity.operator_sender_id(channel)
        if operator != chat or operator is None:
            return False
        if parsed.channel_type in {"lark", "lark_bot"}:
            try:
                subscriptions.check_private_target({"chat_kinds": ["p2p"]}, parsed.channel_type, chat, operator)
            except ValueError:
                return False
        config = await conn.fetchrow("SELECT enabled FROM channel_configs WHERE channel_id = $1", channel)
        return config is not None and config["enabled"]

    async def flush(self, *, now: datetime | None = None) -> None:
        if self._stopped or self._flush_lock.locked():
            return
        now = now or self.clock()
        async with self._flush_lock, connection.get_pool().acquire() as conn:
            if not await store.configured(conn):
                return
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1::int, hashtext(current_schema()))", _SENDER_LOCK):
                return
            try:
                await store.orphan_notices(conn)
                for row in await store.pending_notices(conn, now=now):
                    for side in ("open", "recovery"):
                        if self._stopped or (side == "recovery" and row["resolved_at"] is None):
                            continue
                        claimed = await store.claim_notice(conn, row["id"], side, now=now)
                        if claimed is None:
                            continue
                        state, error = "dashboard", "target"
                        try:
                            if self.post is not None and await self._target_valid(conn, row):
                                state, error = await self._post(row, side, now)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # A policy/target read failed before _post reached the platform; a bounded retry is safe.
                            state, error = "rejected", "policy"
                        # Once post returns, retries touch only the receipt, never the platform.
                        for attempt in range(3):
                            try:
                                await store.settle_notice(conn, row["id"], side, claimed[f"{side}_claim"],
                                                          state, now=now, error=error)
                                break
                            except Exception:
                                if attempt == 2 or conn.is_closed():
                                    raise
                                await asyncio.sleep(0)
            finally:
                if not conn.is_closed():
                    await conn.execute("SELECT pg_advisory_unlock($1::int, hashtext(current_schema()))", _SENDER_LOCK)

    async def _post(self, row: Mapping[str, Any], side: str, now: datetime) -> tuple[str, str | None]:
        key = f"message-stream-alert:{row['id']}:{'open' if side == 'open' else 'recovered'}"
        if chat_settings.delivery_enabled():
            async with connection.get_pool().acquire() as conn:
                routing = await conn.fetchrow(
                    "SELECT c.manager_id AS owner, coalesce(b.manager_id, c.manager_id) AS manager "
                    "FROM channel_configs c LEFT JOIN manager_chat_bindings b "
                    "ON b.channel_id = c.channel_id AND b.chat_id = $2 WHERE c.channel_id = $1",
                    row["target_channel"], row["target_chat"])
                session = channel_thread_id(row["target_channel"], row["target_chat"], routing["manager"], routing["owner"])
                last_user = await conn.fetchval(
                    "SELECT created_at FROM chat_messages WHERE session_id = $1 AND role = 'user' "
                    "ORDER BY created_at DESC LIMIT 1", session)
            decision = await proactive_delivery.decide(
                proactive_delivery.Delivery(proactive_delivery.DeliveryKind.MESSAGE_STREAM_ALERT,
                                             proactive_delivery.Urgency.NORMAL, proactive_delivery.ChatKind.DIRECT),
                dedupe_key=key, channel_id=row["target_channel"], chat=row["target_chat"],
                last_user_message_at=last_user, source_ref=str(row["id"]), now=now)
            if decision.outcome is not proactive_delivery.Outcome.SEND:
                return "deferred", "policy"
        text = ("已恢复：" if side == "recovery" else "") + _LABELS[row["kind"]] + "。" + _FIX[row["kind"]]
        text += f" 来源：{row['source_id']}。"
        if row["parity_session_id"] is not None:
            text += f" 对账：{row['parity_session_id']}。"
            metadata = store.view(row)["metadata"]
            text += (f" 仅参考 {metadata.get('bridge_only', 0)}，仅采集 {metadata.get('collect_only', 0)}，"
                     f"身份冲突 {metadata.get('identity_conflicts', 0)}。")
        try:
            parsed = parse_channel_id(row["target_channel"])
            kind = TargetKind.CHAT if parsed.channel_type == "telegram" else TargetKind.USER
            with turn_metrics.proactive_send(turn_metrics.ProactiveSource.MESSAGE_STREAM_ALERT,
                                             source_ref=str(row["id"]), dedupe_key=key):
                receipt = await self.post(row["target_channel"], NotifyTarget(kind, row["target_chat"], group=False), text,
                                          idempotency_key=key)
            return receipt.result.value, None if receipt.result is PostOutcome.SENT else "delivery"
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown", "delivery"

    async def status_checks(self) -> list[dict[str, Any]]:
        items, _ = await attention_items()
        return [{"id": item["key"], "label": "消息流", "status": "fail", "detail": item["title"],
                 "fix": item["facts"]["fix"]} for item in items]

    async def attention_items(self, limit: int = 20, *, since: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
        return await attention_items(limit, since=since)

    async def stop(self) -> None:
        self._stopped = True
