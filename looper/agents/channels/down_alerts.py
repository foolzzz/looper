"""One outage alert and recovery, through the dashboard and other healthy operator channels.

The control plane polls actual notification results and delivery receipts, never logs. Persist the alert
transition before attempting external sends: a restart cannot repeat an alert whose delivery was uncertain.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from ...db import channel_configs, schedules, system_settings
from . import down_alert_settings
from .bots import BOT_TYPES

logger = logging.getLogger(__name__)
STATE_KEY = "channels.down_state:"


@dataclass
class Outage:
    since: datetime | None = None
    alerted: bool = False
    success_at: datetime | None = None
    failed_since: datetime | None = None

    def dump(self) -> str:
        return json.dumps({k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(self).items()})

    @classmethod
    def read(cls, value: str | None) -> Outage:
        if value is None:
            return cls()
        data = json.loads(value)
        return cls(datetime.fromisoformat(data['since']) if data['since'] else None, data['alerted'],
                   datetime.fromisoformat(data['success_at']) if data['success_at'] else None,
                   datetime.fromisoformat(data['failed_since']) if data.get('failed_since') else None)


class Monitor:
    def __init__(self, registry, *, clock: Callable[[], datetime] | None = None) -> None:
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    async def tick(self) -> None:
        # A single owner also serializes a manual check with the periodic pass.
        async with self._lock:
            await self._tick()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.warning('channel outage check failed', exc_info=True)
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        configs = [r for r in await channel_configs.list_configs() if r['channel_type'] in BOT_TYPES]
        rows = [r for r in configs if r['enabled']]
        saved = await system_settings.get_settings([STATE_KEY + r['channel_id'] for r in configs])
        now = self.clock()
        # Retain a cutoff so historical notification failures cannot become a new enable cycle's outage.
        disabled = {STATE_KEY + r['channel_id']: Outage(success_at=now).dump() for r in configs
                    if not r['enabled'] and STATE_KEY + r['channel_id'] in saved}
        if disabled:
            await system_settings.put_settings(disabled)
        for row in configs:
            if not row['enabled']:
                channel = row['channel_id']
                self.registry.acknowledge_deliveries(channel, len(self.registry.delivery_events(channel)))
        states, down, changes, observed, history_failed = {}, {}, [], {}, {}
        for row in rows:
            channel = row['channel_id']
            state = Outage.read(saved.get(STATE_KEY + channel))
            states[channel] = state
            events = self.registry.delivery_events(channel)
            observed[channel] = len(events)
            outcomes = events + ([(state.failed_since, False)] if state.failed_since else [])
            history = await schedules.channel_delivery_streak(channel, success_after=state.success_at,
                                                               outcomes=outcomes)
            state.success_at, state.failed_since = history['success_at'], history['failed_since']
            history_failed[channel] = history['last_failed_at']
            adapter = self.registry.get(channel)
            runtime = self.registry.start_state(channel)
            running = (adapter is not None and adapter.healthcheck().get('healthy', False)
                       and (runtime is None or runtime['state'] == 'running'))
            # A success between two passes ends the old delivery streak even if another send already failed.
            if running and state.since is not None and state.success_at is not None and state.success_at >= state.since:
                if state.alerted:
                    changes.append((channel, 'recovered'))
                state.since, state.alerted = None, False
            if not running or state.failed_since is not None:
                down[channel] = 'not running' if not running else 'all deliveries failed'
                if state.since is None:
                    state.since = state.failed_since or now
            elif state.since is not None:
                if state.alerted:
                    changes.append((channel, 'recovered'))
                state.since, state.alerted = None, False

        checks, writes = [], {}
        for row in rows:
            channel = row['channel_id']
            state = states[channel]
            if state.since is not None and (state.alerted or now - state.since > timedelta(minutes=down_alert_settings.minutes())):
                elapsed = max(0, int((now - state.since).total_seconds() // 60))
                checks.append({'id': f'channel_down:{channel}', 'label': f'Channel {channel}', 'status': 'fail',
                               'detail': f'Down for {elapsed} minutes ({down[channel]})',
                               'fix': 'Check Settings → Channels and the channel transport.'})
                if not state.alerted:
                    state.alerted = True
                    changes.append((channel, 'down'))
            value = state.dump()
            key = STATE_KEY + channel
            # Healthy installations never acquire outage state or external sends.
            if (state.since is not None or key in saved
                    or history_failed[channel] is not None and state.success_at is not None) and saved.get(key) != value:
                writes[key] = value
        if writes:
            await system_settings.put_settings(writes)
        for channel in states:
            self.registry.acknowledge_deliveries(channel, observed[channel])
        self.registry.down_checks = checks

        for channel, change in changes:
            text = (f'告警：渠道 {channel} 已持续不可用超过 {down_alert_settings.minutes()} 分钟。'
                    '请在 Dashboard 顶部查看故障并检查渠道配置或连接。' if change == 'down'
                    else f'恢复：渠道 {channel} 已恢复运行和投递。')
            for row in rows:
                target = row['channel_id']
                operator = (row.get('config') or {}).get('operator_sender_id')
                if target == channel or target in down or not operator:
                    continue
                try:
                    sent, error = await asyncio.wait_for(self.registry.notify(target, operator, text), 20)
                    if not sent:
                        logger.warning('channel %s %s notice via %s failed: %s', channel, change, target, error)
                except Exception:
                    logger.warning('channel %s %s notice via %s failed', channel, change, target, exc_info=True)
