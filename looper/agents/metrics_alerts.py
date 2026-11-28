"""Opt-in task-quality alerts: daily comparison, hourly missing writes, no model calls.

The local day's check is claimed before computing: failures are logged and the next day tries again. A session
lock covers checking and sending across servers. Like advisor alerts, the gate dashboard target is the recipient.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from ..db import connection, metrics as db_metrics, proactive_deliveries, system_settings
from . import chat_settings, metrics, proactive_delivery, turn_metrics
from .proactive_delivery import ChatKind, Delivery, DeliveryKind, Outcome, Urgency
from .supervisor import cancel_tasks

logger = logging.getLogger(__name__)
ENABLED_KEY = "metrics.alerts.enabled"
CHECKED_KEY = "metrics_alerts_checked"
MISSING_KEY = "metrics_alerts_missing"
STATE_PREFIX = "metrics_alert:"
_LOCK = (741927, 1)
MIN_SAMPLE = 20
DROP_TIERS = (.10, .20, .30)
COST_TIERS = (.50, 1., 2.)
MISSED_TIERS = (2., 4., 8.)
CONVERGENCE_DAYS = 7
_enabled = False
_stored: dict[str, str] = {}


class AlertRule(StrEnum):
    SUCCESS_DROP = "success_drop"
    FIRST_PASS_DROP = "first_pass_drop"
    STEP_COST_RISE = "step_cost_rise"
    MISSED_INCREASE = "missed_increase"
    MISSING_ATTEMPTS = "missing_attempts"


class RuleState(StrEnum):
    INACTIVE = "inactive"
    FIRING = "firing"


@dataclass(frozen=True)
class RuleValues:
    current: float | int | None
    comparison: float | int | None
    n: int = 0
    comparison_n: int = 0
    interval: tuple[float, float] | None = None
    basis: str | None = None
    tier: int = 0


@dataclass(frozen=True)
class RuleResult:
    fired: bool
    tier: int
    judged: bool
    values: tuple[RuleValues, ...]
    # A partially known cost result can fire, but cannot prove recovery in the unknown billing basis.
    complete: bool = True


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _drop(current: Mapping, comparison: Mapping) -> RuleResult:
    interval = current.get("wilson")
    values = RuleValues(current.get("value"), comparison.get("value"), current.get("n", 0),
                        comparison.get("n", 0), tuple(interval) if interval else None)
    judged = (values.n >= MIN_SAMPLE and values.comparison_n >= MIN_SAMPLE
              and _number(values.current) and _number(values.comparison) and interval is not None
              and len(interval) == 2 and all(_number(v) for v in interval))
    drop = values.comparison - values.current if judged else 0
    tier = sum(drop + 1e-12 >= threshold for threshold in DROP_TIERS) if judged else 0
    fired = bool(judged and tier and interval[1] < values.comparison)
    return RuleResult(fired, tier if fired else 0, bool(judged), (values,))


def success_drop(current: Mapping, comparison: Mapping) -> RuleResult:
    return _drop(current, comparison)


def first_pass_drop(current: Mapping, comparison: Mapping) -> RuleResult:
    return _drop(current, comparison)


def step_cost_rise(current: Mapping, comparison: Mapping) -> RuleResult:
    values, tiers = [], []
    for basis in ("provider_priced", "equivalent"):
        a, b = current.get(basis, {}), comparison.get(basis, {})
        value = RuleValues(a.get("median"), b.get("median"), a.get("n", 0), b.get("n", 0), basis=basis)
        if (value.n < MIN_SAMPLE or value.comparison_n < MIN_SAMPLE
                or not _number(value.current) or not _number(value.comparison)):
            continue
        increase = (value.current / value.comparison - 1 if value.comparison else
                    math.inf if value.current > 0 else 0)
        # The first threshold is strictly above +50%; higher tiers start at +100% and +200%.
        tier = sum(increase >= threshold for threshold in COST_TIERS) if increase > COST_TIERS[0] else 0
        values.append(RuleValues(value.current, value.comparison, value.n, value.comparison_n, basis=basis, tier=tier))
        tiers.append(tier)
    tier = max(tiers, default=0)
    return RuleResult(tier > 0, tier, bool(values), tuple(values), complete=len(values) == 2)


def missed_increase(current: Mapping, comparison: Mapping) -> RuleResult:
    values = RuleValues(current.get("count"), comparison.get("count"), current.get("n", 0), comparison.get("n", 0))
    judged = (values.n >= MIN_SAMPLE and values.comparison_n >= MIN_SAMPLE
              and _number(values.current) and _number(values.comparison))
    weekly = values.comparison / 4 if judged else 0
    multiple = values.current / weekly if weekly else math.inf if judged and values.current else 0
    tier = sum(multiple >= threshold for threshold in MISSED_TIERS) if judged and values.current >= 3 and multiple > 2 else 0
    return RuleResult(tier > 0, tier, bool(judged), (values,))


def missing_attempts(current: int | None, comparison: int | None) -> RuleResult:
    judged = isinstance(current, int)
    fired = judged and current > 0
    return RuleResult(fired, int(fired), judged, (RuleValues(current, comparison),))


_LABELS = {AlertRule.SUCCESS_DROP: "任务成功率下降", AlertRule.FIRST_PASS_DROP: "一次通过率下降",
           AlertRule.STEP_COST_RISE: "每个被接受步骤的花费上升", AlertRule.MISSED_INCREASE: "漏检增加",
           AlertRule.MISSING_ATTEMPTS: "尝试缺失"}


def _state(rule: AlertRule) -> dict[str, Any] | None:
    raw = _stored.get(STATE_PREFIX + rule.value)
    return json.loads(raw) if raw else None


def view() -> dict[str, Any]:
    rules = []
    for rule in AlertRule:
        state = _state(rule)
        rules.append({"rule": rule.value, "state": RuleState.FIRING.value if state else RuleState.INACTIVE.value,
                      "sent_on": state["sent_on"] if state else None, "tier": state["tier"] if state else 0})
    return {"enabled": _enabled, "last_checked": _stored.get(CHECKED_KEY), "rules": rules}


async def load() -> None:
    global _enabled, _stored
    _stored = await system_settings.get_settings([ENABLED_KEY, CHECKED_KEY, MISSING_KEY,
                                                  *(STATE_PREFIX + r.value for r in AlertRule)])
    _enabled = _stored.get(ENABLED_KEY) == "true"


async def update(value: bool | None) -> dict[str, Any]:
    global _enabled
    if value is not None and type(value) is not bool:
        raise ValueError("enabled must be true, false or null")
    await system_settings.put_settings({ENABLED_KEY: None if value is None else str(value).lower()})
    _enabled = bool(value)
    return view()


async def _save(values: Mapping[str, str | None]) -> None:
    await system_settings.put_settings(values)
    for key, value in values.items():
        if value is None:
            _stored.pop(key, None)
        else:
            _stored[key] = value


def _log(rule: AlertRule, result: RuleResult) -> None:
    logger.info("task quality alert check", extra={"rule": rule.value, "current": [v.current for v in result.values],
        "comparison": [v.comparison for v in result.values], "n": [v.n for v in result.values],
        "comparison_n": [v.comparison_n for v in result.values], "basis": [v.basis for v in result.values], "tier": result.tier,
        "verdict": "insufficient" if not result.judged else "fired" if result.fired else "clear"})


def _line(rule: AlertRule, result: RuleResult, recovered: bool = False) -> str:
    parts = []
    for v in result.values:
        if rule in (AlertRule.SUCCESS_DROP, AlertRule.FIRST_PASS_DROP):
            value = f"{v.current:.1%} 对 {v.comparison:.1%}，95% Wilson 区间 {v.interval[0]:.1%}–{v.interval[1]:.1%}"
        elif rule is AlertRule.STEP_COST_RISE:
            basis = "供应商价格" if v.basis == "provider_priced" else "订阅等价花费"
            value = f"{basis}中位数 {v.current:.2f} 对 {v.comparison:.2f} 美元"
        elif rule is AlertRule.MISSING_ATTEMPTS:
            value = f"记录覆盖期内 {v.current}，上次检查 {v.comparison}"
        else:
            value = f"{v.current} 对 {v.comparison}（前 28 天周均 {v.comparison / 4:g}）"
        parts.append(f"{value}，n={v.n} 对 n={v.comparison_n}")
    return f"- {_LABELS[rule]}{'：已恢复' if recovered else ''}：" + "；".join(parts)


class MetricsAlertService:
    def __init__(self, registry: Any, clock: Callable[[], datetime] | None = None, interval: float = 3600) -> None:
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await load()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="metrics-alerts")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await cancel_tasks([self._task], raise_errors=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), self._interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        if not _enabled or self._stopping.is_set():
            return
        try:
            async with connection.get_pool().acquire() as conn:
                if not await conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", *_LOCK):
                    return
                try:
                    await self._tick()
                finally:
                    await asyncio.shield(conn.execute("SELECT pg_advisory_unlock($1::int, $2::int)", *_LOCK))
        except Exception:
            logger.warning("metrics alert check failed; daily rules try again tomorrow", exc_info=True)

    async def _tick(self) -> None:
        global _stored
        now = self._clock()
        local = now.astimezone(proactive_delivery.load_policy().timezone)
        day = local.date()
        _stored = await system_settings.get_settings([CHECKED_KEY, MISSING_KEY,
                                                      *(STATE_PREFIX + r.value for r in AlertRule)])
        # Missing writes are checked hourly, including before the first daily comparison.
        count = None
        try:
            count = await self._missing(day, now)
        except Exception:
            logger.warning("missing attempt check failed", exc_info=True)
        if local.hour < 9 or _stored.get(CHECKED_KEY) == day.isoformat() or not _enabled:
            return
        await _save({CHECKED_KEY: day.isoformat()})
        report = await metrics.compute(7, metrics.Filters(), now=now, alerts=True)
        figures = report["alert_figures"]
        inputs = {AlertRule.SUCCESS_DROP: (success_drop, report["tasks"]["success_rate"]),
                  AlertRule.FIRST_PASS_DROP: (first_pass_drop, report["steps"]["first_pass_rate"]),
                  AlertRule.STEP_COST_RISE: (step_cost_rise, figures["cost"]),
                  AlertRule.MISSED_INCREASE: (missed_increase, figures["misses"])}
        if count == 0:
            inputs[AlertRule.MISSING_ATTEMPTS] = (missing_attempts, {"count": 0})
        lines, changes = [], {}
        for rule, (judge, current) in inputs.items():
            result = (missing_attempts(0, 0) if rule is AlertRule.MISSING_ATTEMPTS else
                      judge(current, current.get("previous", {})))
            _log(rule, result)
            state = _state(rule)
            if not result.judged:
                continue
            if result.fired and (state is None or day - date.fromisoformat(state["sent_on"]) >= timedelta(days=7)
                                 or result.tier > state["tier"]):
                lines.append(_line(rule, result))
                state_value = {"sent_on": day.isoformat(), "tier": result.tier}
                if rule is AlertRule.STEP_COST_RISE:
                    state_value["bases"] = [v.basis for v in result.values if v.tier]
                changes[STATE_PREFIX + rule.value] = json.dumps(state_value)
            elif (not result.fired and state is not None and (result.complete or (
                    rule is AlertRule.STEP_COST_RISE and "bases" in state
                    and set(state["bases"]) <= {v.basis for v in result.values}))):
                lines.append(_line(rule, result, recovered=True))
                changes[STATE_PREFIX + rule.value] = None
        if lines:
            a, b = report["window"], report["comparison_window"]
            text = (f"任务质量告警：最近 7 天（{a['since']} 至 {a['until']}）对前 28 天"
                    f"（{b['since']} 至 {b['until']}）\n" + "\n".join(lines))
            if await self._send(text, f"metrics_alert:{day.isoformat()}", now):
                await _save(changes)

    async def _missing(self, day: date, now: datetime) -> int | None:
        count = await db_metrics.missing_attempts()
        previous = json.loads(_stored.get(MISSING_KEY, "{}"))
        result = missing_attempts(count, previous.get("count"))
        _log(AlertRule.MISSING_ATTEMPTS, result)
        seen = previous.get("counts", []) if previous.get("date") == day.isoformat() else []
        if not result.fired or count in seen or not _enabled:
            return count
        # Claim before sending: a crash must not produce a second copy on restart.
        await _save({MISSING_KEY: json.dumps({"date": day.isoformat(), "count": count, "counts": [*seen, count]})})
        text = f"任务质量告警：尝试缺失 {count}（上次检查 {previous.get('count', '未知')}）。记录覆盖期内的 Worker 尝试写入路径可能损坏。"
        if await self._send(text, f"metrics_alert:{day.isoformat()}:missing:{count}", now):
            await _save({STATE_PREFIX + AlertRule.MISSING_ATTEMPTS: json.dumps({"sent_on": day.isoformat(), "tier": 1})})
        return count

    async def _send(self, text: str, key: str, now: datetime) -> bool:
        from .gate_service import dashboard_target

        chat = await dashboard_target()
        if chat is None or not _enabled or self._stopping.is_set():
            logger.warning("metrics alert has no active target: %s", key)
            return False
        decided = chat_settings.delivery_enabled()
        if decided:
            decision = await proactive_delivery.decide(
                Delivery(DeliveryKind.METRICS_ALERT, Urgency.NORMAL,
                         ChatKind.GROUP if chat.in_group else ChatKind.DIRECT),
                dedupe_key=key, channel_id=chat.channel_id, chat=chat.target.id, last_user_message_at=None, now=now)
            if decision.outcome is not Outcome.SEND:
                return decision.outcome is Outcome.DIGEST
        if not _enabled or self._stopping.is_set():
            return False
        with turn_metrics.proactive_send(turn_metrics.ProactiveSource.METRICS_ALERT, dedupe_key=key):
            sent, error = await self._registry.notify(chat.channel_id, chat.target.id, text, group=chat.in_group)
        if decided:
            await proactive_deliveries.record_receipt(key, "sent" if sent else "rejected")
        if not sent:
            logger.warning("metrics alert could not be sent: %s (%s)", key, error)
        return sent
