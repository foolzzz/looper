"""Run admission accounting.

Reservations belong to turns, not resident processes. One lock serializes admission with rebuilds; records are
its durable evidence. Budget decisions are still recorded rather than enforced. In ``enforce``, a usage-window
hold waits out schedule firings and the synchronous background roles. A run the operator starts is only recorded.
Holds never switch accounts. ``off`` reads nothing beyond the cached mode.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from ..db import cost_guard as store
from . import cost_guard_settings as settings

logger = logging.getLogger(__name__)


class Currency(str, Enum):
    PROVIDER = 'provider_priced_usd'
    EQUIVALENT = 'equivalent_usd'


class RunKind(str, Enum):
    MANAGER = 'manager'
    PLANNER = 'planner'
    WORKER = 'worker'
    EXPLORER = 'explorer'
    REVIEWER = 'reviewer'
    PROCESSOR = 'processor'
    LEADS_CHECK = 'leads_check'
    ADVICE_CHECK = 'advice_check'
    OUTBOUND_CHECK = 'outbound_check'
    ADVISOR = 'advisor'
    CONSULT = 'consult'
    SCHEDULE = 'schedule'


class BudgetScope(str, Enum):
    TASK = 'task'
    SCHEDULE_DAY = 'schedule_day'
    ROLE_DAY = 'role_day'
    GLOBAL_DAY = 'global_day'


class LimitAction(str, Enum):
    ASK = 'ask'
    STOP_ROLE = 'stop_role'
    SKIP = 'skip'


class Decision(str, Enum):
    ADMIT = 'admit'
    DENY = 'deny'
    ASK = 'ask'
    HOLD = 'hold'


class Held(Exception):
    """A synchronous background run waits for a usage window. The caller keeps its own failure result."""

    def __init__(self, until: datetime, reason: str):
        self.until = until
        self.reason = reason
        super().__init__(f"usage window on hold until {until.astimezone().strftime('%H:%M')}")


class EstimateSource(str, Enum):
    RUN_CAP = 'run_cap'
    P95 = 'p95'
    FLOOR = 'floor'
    FLOOR_NO_SAMPLES = 'floor_no_samples'


BACKGROUND = frozenset((RunKind.PROCESSOR, RunKind.LEADS_CHECK, RunKind.ADVICE_CHECK,
                        RunKind.OUTBOUND_CHECK, RunKind.ADVISOR))
SMALL = frozenset((RunKind.PROCESSOR, RunKind.LEADS_CHECK, RunKind.ADVICE_CHECK, RunKind.OUTBOUND_CHECK))


@dataclass(frozen=True)
class Scope:
    scope: BudgetScope
    scope_ref: str
    limit_usd: float | None = None

    @property
    def key(self):
        return self.scope, self.scope_ref

    def view(self):
        return {'scope': self.scope.value, 'scope_ref': self.scope_ref, 'limit_usd': self.limit_usd}


@dataclass
class Reservation:
    usd: float
    currency: Currency
    scopes: tuple[Scope, ...]
    observation: dict
    created_at: datetime
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    record_id: int | None = None
    schedule_id: str | None = None

    def metadata(self):
        return {'reservation': {'usd': self.usd, 'currency': self.currency.value,
                                'scopes': [scope.view() for scope in self.scopes], 'token': self.token},
                'cost_guard': self.observation,
                **({'cost_guard_schedule_id': self.schedule_id} if self.schedule_id else {})}


@dataclass(frozen=True)
class Admission:
    decision: Decision = Decision.ADMIT
    would: Decision = Decision.ADMIT
    reservation: Reservation | None = None
    until: datetime | None = None
    reason: str | None = None
    provider_id: str | None = None
    reset_known: bool = True
    window: str | None = None


def currency_of(launch) -> Currency:
    # Same authentication evidence as llm_records.EQUIVALENT; runtime pricing alone does not imply real spend.
    return Currency.EQUIVALENT if launch.profile.provider.auth == 'oauth' else Currency.PROVIDER


def percentile(values: list[float], q=.95) -> float:
    values = sorted(values)
    index = (len(values) - 1) * q
    low = math.floor(index)
    return values[low] + (values[math.ceil(index)] - values[low]) * (index - low)


def _local_day(now: datetime) -> datetime:
    local = now.astimezone()
    return datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo)


class Guard:
    def __init__(self, config=None, *, day_start: Callable = _local_day,
                 clock: Callable = lambda: datetime.now(timezone.utc), pool=None):
        self.config = config or settings.Config()
        self.day_start, self.clock, self.pool = day_start, clock, pool
        self.day = None
        self._lock = asyncio.Lock()
        self._spent = defaultdict(float)
        self._reserved = defaultdict(float)
        self._scopes: dict[tuple, Scope] = {}
        self._pending: dict[str, Reservation] = {}
        self._records: dict[int, Reservation] = {}
        self._samples = defaultdict(list)
        self._limits: dict = {}
        self._recent = deque(maxlen=20)
        self._recommendations = dict.fromkeys(Currency)
        self.problem: str | None = None

    async def rebuild(self):
        if self.config.mode is settings.Mode.OFF:
            return
        async with self._lock:
            await self._rebuild()

    async def _rebuild(self):
        now = self.clock()
        rows = await store.history(now, pool=self.pool)
        limits = await store.limits(pool=self.pool)
        day = self.day_start(now)
        spent, reserved, samples = defaultdict(float), defaultdict(float), defaultdict(list)
        records, scopes, recent, daily = {}, {}, deque(maxlen=20), defaultdict(lambda: defaultdict(float))
        persisted_tokens = set()
        for row in rows:
            metadata = row['metadata']
            # /new and /profile are code receipts, not model runs or estimate samples.
            if 'session_reset' in metadata or 'session_profile' in metadata:
                continue
            kind = row.get('run_kind')
            if kind not in RunKind._value2member_map_:
                continue
            kind, currency = RunKind(kind), Currency(row['currency'])
            chain = self.scopes(kind, row.get('task_id'), row.get('schedule_id'), currency, limits=limits)
            cost = float(row['cost_usd']) if row.get('cost_usd') is not None else 0.
            if row['status'] != 'running' and cost > 0 and row['created_at'] >= now-timedelta(days=30):
                samples[kind, currency].append(cost)
            if row['created_at'] >= now-timedelta(days=30):
                daily[currency][self.day_start(row['created_at'])] += cost
            if observation := metadata.get('cost_guard'):
                recent.append(observation)
            raw = metadata.get('reservation') if row['status'] == 'running' else None
            reservation = None
            if raw:
                # Historical reservation scopes are authoritative for an already admitted run; refreshed limits
                # come from today's settings, never from the old cap stored alongside the reservation.
                chain = tuple(Scope(BudgetScope(s['scope']), s['scope_ref'], self._limit(
                    BudgetScope(s['scope']), s['scope_ref'], Currency(raw['currency']), limits)) for s in raw['scopes'])
                reservation = Reservation(float(raw['usd']), Currency(raw['currency']), chain,
                                          metadata.get('cost_guard', {}), row['created_at'], record_id=row['id'],
                                          schedule_id=row.get('schedule_id'))
                records[row['id']] = reservation
                persisted_tokens.add(raw.get('token'))
            for scope in chain:
                key = (*scope.key, currency)
                scopes[key] = scope
                if reservation:
                    reserved[key] += reservation.usd
                elif scope.scope is BudgetScope.TASK or row['created_at'] >= day:
                    spent[key] += cost
        # Admissions that have not finished opening their record are absent from the snapshot query.
        for reservation in self._pending.values():
            if reservation.token in persisted_tokens:
                continue
            for scope in reservation.scopes:
                key = (*scope.key, reservation.currency)
                scopes[key] = scope
                reserved[key] += reservation.usd
        self.day, self._limits = day, limits
        self._spent, self._reserved, self._samples = spent, reserved, samples
        self._scopes, self._records, self._recent = scopes, records, recent
        # A week of observations precedes advice; recommendations are never applied as configuration.
        observed = [row['created_at'] for row in rows if row['metadata'].get('cost_guard')]
        if observed and min(observed) <= now-timedelta(days=7):
            days = [day-timedelta(days=i) for i in range(1, 31)]
            self._recommendations = {c: percentile([daily[c].get(d, 0.) for d in days])*1.5 for c in Currency}
        self.problem = None

    def _limit(self, scope, ref, currency, limits=None):
        limits = self._limits if limits is None else limits
        if scope is BudgetScope.TASK:
            return limits.get('tasks', {}).get(ref, {}).get('cap')
        if scope is BudgetScope.SCHEDULE_DAY:
            # The existing schedule cap covers both currencies until a separate equivalent cap lands.
            return limits.get('schedules', {}).get(ref)
        if scope is BudgetScope.ROLE_DAY:
            role = self.config.roles.get(ref)
            return getattr(role, 'daily_usd' if currency is Currency.PROVIDER else 'daily_equivalent_usd', None)
        return self.config.daily_usd if currency is Currency.PROVIDER else self.config.daily_equivalent_usd

    def scopes(self, kind, task_id, schedule_id, currency, *, limits=None):
        limits = self._limits if limits is None else limits
        chain = []
        if task_id and kind is not RunKind.MANAGER:
            chain.append((BudgetScope.TASK, task_id))
            schedule_id = schedule_id or limits.get('tasks', {}).get(task_id, {}).get('schedule_id')
        if schedule_id:
            chain.append((BudgetScope.SCHEDULE_DAY, schedule_id))
        if kind in BACKGROUND:
            chain.append((BudgetScope.ROLE_DAY, kind.value))
        chain.append((BudgetScope.GLOBAL_DAY, 'global'))
        return tuple(Scope(scope, ref, self._limit(scope, ref, currency, limits)) for scope, ref in chain)

    def estimate(self, kind, currency, cap):
        if cap is not None:
            return float(cap), EstimateSource.RUN_CAP
        floor = .05 if kind in SMALL else .5
        samples = self._samples[kind, currency]
        if not samples:
            return floor, EstimateSource.FLOOR_NO_SAMPLES
        p95 = percentile(samples)
        return (p95, EstimateSource.P95) if p95 >= floor else (floor, EstimateSource.FLOOR)

    async def admit(self, run_kind, scopes, currency, estimate, *, estimate_source=EstimateSource.RUN_CAP,
                    reserve=True, schedule_id=None):
        if self.config.mode is settings.Mode.OFF:
            return Admission()
        async with self._lock:
            if self.day != self.day_start(self.clock()):
                await self._rebuild()
            return self._admit(run_kind, scopes, currency, estimate, estimate_source, reserve, schedule_id)

    def _admit(self, kind, scopes, currency, estimate, source, reserve, schedule_id):
        if not math.isfinite(estimate) or estimate < 0:
            raise ValueError('reservation estimate must be finite and nonnegative')
        action, hit = None, None
        for scope in scopes:
            self._scopes[*scope.key, currency] = scope
        for scope in scopes:
            key = (*scope.key, currency)
            used = self._spent[key]+self._reserved[key]
            if scope.scope is BudgetScope.SCHEDULE_DAY:
                used = sum(self._spent[*scope.key, c]+self._reserved[*scope.key, c] for c in Currency)
            if scope.limit_usd is not None and used+estimate > scope.limit_usd:
                hit = scope
                action = (LimitAction.SKIP if kind is RunKind.SCHEDULE else
                          LimitAction.STOP_ROLE if kind in BACKGROUND else LimitAction.ASK)
                break
        would = Decision.ADMIT if hit is None else Decision.ASK if action is LimitAction.ASK else Decision.DENY
        observation = {'at': self.clock().isoformat(), 'run_kind': kind.value, 'currency': currency.value,
                       'estimate_usd': estimate, 'estimate_source': source.value, 'decision': would.value,
                       'scope': hit.scope.value if hit else None, 'scope_ref': hit.scope_ref if hit else None,
                       'action': action.value if action else None}
        self._recent.append(observation)
        logger.info('cost_guard.observe %s', observation)
        reservation = None
        if reserve:
            reservation = Reservation(estimate, currency, tuple(scopes), observation, self.clock(), schedule_id=schedule_id)
            self._pending[reservation.token] = reservation
            for scope in scopes:
                self._reserved[*scope.key, currency] += estimate
        return Admission(Decision.ADMIT, would, reservation)

    def _apply_hold(self, kind, admission, hold, provider_id, *, operator=False):
        """Record a hold after the budget chain. Enforce it only for a schedule or a background role.

        ``operator`` records the hold and does not enforce it: a run the operator starts still starts.
        """
        if hold is None:
            return admission
        observation = admission.reservation.observation if admission.reservation is not None else self._recent[-1]
        enforced = (not operator and self.config.mode is settings.Mode.ENFORCE
                    and (kind in BACKGROUND or kind is RunKind.SCHEDULE))
        observation['would_hold_until'] = hold.until.isoformat()
        observation['would_hold_reason'] = hold.reason.value
        observation['note'] = ('hold until ' if enforced else 'would hold until ') + hold.until.isoformat()
        if not enforced:
            return Admission(admission.decision, admission.would, admission.reservation, hold.until,
                             hold.reason.value, provider_id, hold.reset_known, hold.window)
        observation['decision'] = Decision.HOLD.value
        if admission.reservation is not None:
            self.abandon(admission.reservation)
        return Admission(Decision.HOLD, admission.would, None, hold.until, hold.reason.value, provider_id,
                         hold.reset_known, hold.window)

    async def _usage_hold(self, launch, now):
        provider = getattr(getattr(launch, 'profile', None), 'provider', None)
        provider_id = getattr(provider, 'id', None)
        if not isinstance(provider_id, str) or not provider_id:
            return None, None
        from . import usage_windows

        try:
            # Ask as a background caller so a run the operator starts still records that hold.
            hold = await usage_windows.hold_for(provider_id, background=True, now=now, pool=self.pool)
        except Exception:
            logger.exception('usage window lookup failed')
            return None, provider_id
        return hold, provider_id

    def bind(self, reservation, record_id):
        self._pending.pop(reservation.token, None)
        reservation.record_id = record_id
        self._records[record_id] = reservation

    def abandon(self, reservation):
        if self._pending.pop(reservation.token, None) is not None:
            self._release(reservation)

    def _release(self, reservation):
        for scope in reservation.scopes:
            key = (*scope.key, reservation.currency)
            self._reserved[key] = max(0., self._reserved[key]-reservation.usd)

    def finish(self, record_id, cost_usd):
        reservation = self._records.pop(record_id, None)
        if reservation is None:
            return
        self._release(reservation)
        if cost_usd is not None:
            for scope in reservation.scopes:
                if scope.scope is BudgetScope.TASK or reservation.created_at >= self.day:
                    self._spent[*scope.key, reservation.currency] += float(cost_usd)

    def snapshot(self):
        return {'counters': [{**scope.view(), 'currency': currency.value,
                               'spent_usd': self._spent[key], 'reserved_usd': self._reserved[key]}
                              for key, scope in self._scopes.items() for currency in [key[2]]],
                'recent': list(reversed(self._recent)),
                'recommended_daily_usd': self._recommendations[Currency.PROVIDER],
                'recommended_daily_equivalent_usd': self._recommendations[Currency.EQUIVALENT],
                'problem': self.problem}


_guard: Guard | None = None


def current() -> Guard:
    global _guard
    if _guard is None:
        _guard = Guard(settings.cached())
    return _guard


async def initialize(*, day_start, pool=None):
    global _guard
    await settings.load(pool=pool)
    _guard = Guard(settings.cached(), day_start=day_start, pool=pool)
    await refresh()


async def refresh():
    guard = current()
    await settings.load(pool=guard.pool)
    guard.config = settings.cached()
    if guard.config.mode is settings.Mode.OFF:
        # Disabling clears guard state, never any stored record.
        replacement = Guard(guard.config, day_start=guard.day_start, clock=guard.clock, pool=guard.pool)
        global _guard
        _guard = replacement
        return
    try:
        await guard.rebuild()
    except Exception:
        guard.problem = 'Cost accounting could not be refreshed; existing execution and review boundaries remain active.'
        logger.exception('cost guard accounting refresh failed')


async def admit(run_kind: RunKind, scopes: tuple[Scope, ...], currency: Currency, estimate: float) -> Admission:
    """The common admission policy; off uses only cached configuration and performs no I/O."""
    return await current().admit(run_kind, scopes, currency, estimate)


async def before_run(*, launch, run_kind, task_id=None, schedule_id=None):
    guard = current()
    if guard.config.mode is settings.Mode.OFF:
        return None
    kind = RunKind(run_kind) if run_kind in RunKind._value2member_map_ else None
    if kind is None or launch is None:
        return None
    try:
        # Window and failure reads stay outside the admission lock.
        hold, provider_id = await guard._usage_hold(launch, guard.clock())
        async with guard._lock:
            if guard.day != guard.day_start(guard.clock()):
                await guard._rebuild()
            if task_id or schedule_id:
                context = await store.context(task_id, schedule_id, pool=guard.pool)
                for group in ('tasks', 'schedules'):
                    guard._limits.setdefault(group, {}).update(context[group])
            currency = currency_of(launch)
            cap = launch.max_budget_usd
            if kind is RunKind.MANAGER:
                from .session_budget import turn_budget_usd

                turn_cap = await turn_budget_usd(pool=guard.pool)
                cap = min(cap, turn_cap) if cap is not None and turn_cap is not None else turn_cap or cap
            estimate, source = guard.estimate(kind, currency, cap)
            scopes = guard.scopes(kind, task_id, schedule_id, currency)
            admission = guard._admit(kind, scopes, currency, estimate, source, True, schedule_id)
            admission = guard._apply_hold(kind, admission, hold, provider_id)
    except Held:
        raise
    except Exception:
        guard.problem = 'Run accounting failed; inspect the server log.'
        logger.exception('cost guard admission failed for %s', kind.value)
        # Accounting errors leave classification active; only its verdict and persisted audit can allow a send.
        return None
    if admission.decision is Decision.HOLD and kind in BACKGROUND:
        from . import usage_windows

        await usage_windows.notify_hold(admission.provider_id, hold)
        raise Held(admission.until, admission.reason or '')
    return admission.reservation


async def bind(reservation, record_id):
    if reservation is not None:
        guard = current()
        if guard.config.mode is settings.Mode.OFF:
            return
        async with guard._lock:
            guard.bind(reservation, record_id)


async def abandon(reservation):
    if reservation is not None:
        guard = current()
        if guard.config.mode is settings.Mode.OFF:
            return
        async with guard._lock:
            guard.abandon(reservation)


async def finish(record_id, *, cost_usd=None, cost_source=None):
    guard = current()
    if guard.config.mode is settings.Mode.OFF:
        return
    async with guard._lock:
        guard.finish(record_id, cost_usd)


async def _schedule_launch(schedule):
    from .agent_settings import AgentType, load_agent_settings, Launch

    role = AgentType.PROCESSOR if schedule['kind'] == 'subscription' else AgentType.PLANNER
    settings = await load_agent_settings(role, schedule.get('project_id'), schedule_id=schedule['id'], wait=False)
    return Launch(settings, role=role), role


async def schedule_enforced_hold(schedule):
    """The enforced hold for a schedule firing, without recording an observation.

    ``None`` when the guard is not enforcing or the lookup failed. ``(None, provider)`` when nothing holds.
    Window and failure reads stay outside the admission lock.
    """
    guard = current()
    if guard.config.mode is not settings.Mode.ENFORCE:
        return None
    try:
        launch, _role = await _schedule_launch(schedule)
        return await guard._usage_hold(launch, guard.clock())
    except Exception:
        logger.exception('cost guard schedule hold lookup failed')
        return None


async def observe_schedule(schedule, *, operator=False):
    guard = current()
    if guard.config.mode is settings.Mode.OFF:
        return
    try:
        launch, role = await _schedule_launch(schedule)
        # Window and failure reads stay outside the admission lock.
        hold, provider_id = await guard._usage_hold(launch, guard.clock())
        async with guard._lock:
            if guard.day != guard.day_start(guard.clock()):
                await guard._rebuild()
            # This is a trigger observation, not another reservation: the actual model turn reserves once.
            currency = currency_of(launch)
            estimate, source = guard.estimate(RunKind(role.value), currency, schedule.get('run_budget_usd'))
            scopes = guard.scopes(RunKind.SCHEDULE, None, schedule['id'], currency)
            admission = guard._admit(RunKind.SCHEDULE, scopes, currency, estimate, source, False, schedule['id'])
            return guard._apply_hold(RunKind.SCHEDULE, admission, hold, provider_id, operator=operator)
    except Exception:
        guard.problem = 'Schedule accounting failed; inspect the server log.'
        logger.exception('cost guard schedule observation failed')
    return None


async def refresh_day():
    """Use the scheduler's existing tick for the daily sample/counter rebuild, even on an idle day."""
    guard = current()
    if guard.config.mode is settings.Mode.OFF:
        return
    try:
        async with guard._lock:
            if guard.day != guard.day_start(guard.clock()):
                await guard._rebuild()
    except Exception:
        guard.problem = 'Daily accounting refresh failed; inspect the server log.'
        logger.exception('cost guard daily refresh failed')
