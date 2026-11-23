"""Optional per-account admission, fair queues and durable quota freezes.

The cached disabled path neither takes a lock nor reads storage. A controller
error admits the run; persistence errors preserve the effective in-memory state.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from ..db import llm_records, profile_registry, provider_limits, system_settings
from . import attempts
from .agent_settings import AgentType
from .lanes import Lane

logger = logging.getLogger(__name__)
KEY = "engine.admission.enabled"
# Share with usage_windows when its independently switched cost guard lands.
BACKGROUND_PAUSE_UTILIZATION = 0.95
AGING_SECONDS = 300
INTERACTIVE_SECONDS = 120
RECHECK_SECONDS = 30
BACKOFF_MINUTES = (5, 10, 20, 40, 60)
_current_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "admission_owner", default=None
)


class AdmissionState(StrEnum):
    OPEN = "open"
    FROZEN = "frozen"
    PROBING = "probing"


@dataclass(frozen=True)
class QuotaFailure:
    resets_at: datetime | None
    record_id: int | None


@dataclass(frozen=True)
class UntilReached:
    pass


@dataclass(frozen=True)
class ProbeEnded:
    quota: bool
    other_failure: bool


@dataclass(frozen=True)
class AllowedSignal:
    observed_at: datetime


@dataclass(frozen=True)
class OperatorClear:
    pass


Event = QuotaFailure | UntilReached | ProbeEnded | AllowedSignal | OperatorClear


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class StillLimited:
    resets_at: datetime | None = None


@dataclass(frozen=True)
class Unavailable:
    pass


ProbeResult = Allowed | StillLimited | Unavailable
Probe = Callable[[str], Awaitable[ProbeResult]]


async def _unavailable(provider_id: str) -> ProbeResult:
    # No safe app-server session is available at the admission boundary today.
    # Keep the typed hook so an authenticated read can replace the waiter probe.
    return Unavailable()


@dataclass
class Account:
    state: AdmissionState = AdmissionState.OPEN
    reason: str | None = None
    until: datetime | None = None
    until_known: bool = True
    backoff_step: int = 0
    record_id: int | None = None
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    probe_owner: str | None = None
    probe_checked: bool = False
    resumed_at: datetime | None = None


@dataclass
class Grant:
    provider_id: str
    owner: str
    lane: Lane
    borrowed: bool = False
    probe: bool = False
    reported: bool = False


@dataclass
class Waiter:
    provider_id: str
    owner: str
    lane: Lane
    arrived: datetime
    sequence: int
    ready: asyncio.Future[Grant]
    draining: Callable[[], bool] | None = None
    on_wait: Callable[[str], Awaitable[None]] | None = None
    last_message: str | None = None


def effective_lane(lane: Lane, waited_seconds: float) -> Lane:
    if lane is Lane.INTERACTIVE:
        return lane
    return Lane(
        max(Lane.USER_TASK, lane - int(max(0, waited_seconds) // AGING_SECONDS))
    )


def lane_for(
    *,
    role: AgentType,
    schedule_id: str | None = None,
    trigger: attempts.Trigger | None = None,
    same_attempt: bool = False,
    recovered: bool = False,
    run_now: bool = False,
    parent_lane: Lane | None = None,
) -> Lane:
    if parent_lane is not None:
        return parent_lane
    if recovered or same_attempt or trigger is attempts.Trigger.RESTART:
        return Lane.RECOVERY
    if role in (
        AgentType.PROCESSOR,
        AgentType.ADVISOR,
        AgentType.ADVICE_CHECK,
        AgentType.LEADS_CHECK,
        AgentType.OUTBOUND_CHECK,
    ):
        return Lane.BACKGROUND
    return Lane.BACKGROUND if schedule_id and not run_now else Lane.USER_TASK


async def _freeze_alert(provider_id: str, event_id: int, **kwargs) -> None:
    from .admission_notices import freeze_alert

    await freeze_alert(provider_id, event_id, **kwargs)


class Controller:
    def __init__(
        self, *, now: Callable[[], datetime] | None = None, probe: Probe | None = None
    ):
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.probe = probe or _unavailable
        self.draining: Callable[[], bool] = lambda: False
        self.active = False
        self.lock = asyncio.Condition()
        self.states: dict[str, Account] = {}
        self.running: dict[str, Grant] = {}
        self.waiters: list[Waiter] = []
        self.sequence = 0
        self.warning_day: date | None = None
        self.overadmissions = 0
        self.notices: list[tuple[str, int, dict]] = []

    def _state(self, provider_id: str) -> Account:
        return self.states.setdefault(provider_id, Account(since=self.now()))

    async def transition(self, provider_id: str, event: Event) -> AdmissionState:
        async with self.lock:
            state = await self._transition(provider_id, event)
            self.lock.notify_all()
        await self._send_notices()
        return state

    async def _send_notices(self) -> None:
        pending, self.notices = self.notices, []
        for provider_id, event_id, fields in pending:
            try:
                await _freeze_alert(provider_id, event_id, **fields)
            except Exception:
                logger.exception("Could not send account freeze alert")

    async def _transition(self, provider_id: str, event: Event) -> AdmissionState:
        account = self._state(provider_id)
        previous = account.state
        now = self.now()
        freeze = isinstance(event, QuotaFailure) and previous in (
            AdmissionState.OPEN,
            AdmissionState.PROBING,
        )
        freeze = (
            freeze
            or isinstance(event, ProbeEnded)
            and event.quota
            and previous is AdmissionState.PROBING
        )
        if freeze:
            reset = event.resets_at if isinstance(event, QuotaFailure) else None
            valid = reset is not None and now < reset <= now + timedelta(days=8)
            account.state, account.reason = AdmissionState.FROZEN, "quota"
            account.until_known = valid
            account.until = (
                reset + timedelta(seconds=60)
                if valid
                else now
                + timedelta(
                    minutes=BACKOFF_MINUTES[
                        min(account.backoff_step, len(BACKOFF_MINUTES) - 1)
                    ]
                )
            )
            account.backoff_step = 0 if valid else account.backoff_step + 1
            account.record_id = (
                event.record_id
                if isinstance(event, QuotaFailure)
                else account.record_id
            )
        elif (
            isinstance(event, UntilReached)
            and previous is AdmissionState.FROZEN
            and account.until is not None
            and now >= account.until
        ):
            account.state = AdmissionState.PROBING
        elif (
            isinstance(event, OperatorClear)
            or (
                isinstance(event, AllowedSignal)
                and (
                    previous is AdmissionState.PROBING
                    or previous is AdmissionState.FROZEN
                    and event.observed_at > account.since
                )
            )
            or (
                isinstance(event, ProbeEnded)
                and previous is AdmissionState.PROBING
                and not event.quota
                and not event.other_failure
            )
        ):
            account.state, account.reason = AdmissionState.OPEN, None
            account.until, account.until_known, account.backoff_step = None, True, 0
        else:
            if isinstance(event, ProbeEnded) and previous is AdmissionState.PROBING:
                account.probe_owner = None
            return account.state
        account.since = now
        if account.state is AdmissionState.OPEN:
            account.resumed_at = now
        account.probe_owner, account.probe_checked = None, False
        try:
            from ..db import provider_admission

            event_id = await provider_admission.save(
                provider_id,
                previous.value,
                account.state.value,
                reason=account.reason,
                until=account.until,
                until_known=account.until_known,
                backoff_step=account.backoff_step,
                record_id=account.record_id,
                since=account.since,
                actor="operator" if isinstance(event, OperatorClear) else "code",
            )
            if freeze:
                self.notices.append(
                    (
                        provider_id,
                        event_id,
                        {
                            "until": account.until,
                            "until_known": account.until_known,
                            "waiting": sum(
                                w.provider_id == provider_id for w in self.waiters
                            ),
                        },
                    )
                )
        except Exception:
            logger.exception(
                "Could not persist account admission transition; in-memory state retained"
            )
        return account.state

    async def load_states(self) -> None:
        from ..db import provider_admission

        rows = await provider_admission.read_all()
        async with self.lock:
            for row in rows:
                # PR5 owns degraded; it does not block admission in this version.
                state = (
                    AdmissionState.OPEN
                    if row["state"] == "degraded"
                    else AdmissionState(row["state"])
                )
                self.states[row["provider_id"]] = Account(
                    **{
                        k: row[k]
                        for k in (
                            "reason",
                            "until",
                            "until_known",
                            "backoff_step",
                            "record_id",
                            "since",
                        )
                    },
                    state=state,
                    resumed_at=row["since"] if state is AdmissionState.OPEN else None,
                )

    async def _account(self, provider_id: str) -> dict:
        provider = await profile_registry.get_provider(provider_id)
        if provider is None:
            raise LookupError("admission provider not found")
        return provider

    async def set_enabled(self, value: bool) -> None:
        async with self.lock:
            self.active = value
            self.lock.notify_all()

    async def _background_paused(self, provider_id: str) -> bool:
        now = self.now()
        return any(
            w.get("utilization") is not None
            and w["utilization"] >= BACKGROUND_PAUSE_UTILIZATION
            and (w.get("resets_at") is None or w["resets_at"] > now)
            and (
                self._state(provider_id).resumed_at is None
                or w.get("observed_at", now) > self._state(provider_id).resumed_at
            )
            for w in await provider_limits.windows(provider_id)
        )

    def daily_overadmissions(self) -> int:
        return (
            self.overadmissions
            if self.warning_day == self.now().astimezone().date()
            else 0
        )

    def _overadmitted(self) -> None:
        today = self.now().astimezone().date()
        if today != self.warning_day:
            self.warning_day, self.overadmissions = today, 0
        self.overadmissions += 1
        logger.warning(
            "Interactive account slot over-admitted after two minutes (today=%d)",
            self.overadmissions,
        )

    def _grant(self, waiter: Waiter, *, probe=False) -> None:
        grant = Grant(waiter.provider_id, waiter.owner, waiter.lane, probe=probe)
        if self.active:
            self.running[waiter.owner] = grant
        if probe:
            self._state(waiter.provider_id).probe_owner = waiter.owner
        self.waiters.remove(waiter)
        waiter.ready.set_result(grant)

    async def _pump(self, provider_id: str) -> None:
        waiting = [
            w
            for w in self.waiters
            if w.provider_id == provider_id and not w.ready.done()
        ]
        if not waiting:
            return
        if not self.active:
            for waiter in waiting:
                self._grant(waiter)
            return
        provider = await self._account(provider_id)
        account = self._state(provider_id)
        now = self.now()
        if (
            account.state is AdmissionState.FROZEN
            and account.until is not None
            and now >= account.until
        ):
            await self._transition(provider_id, UntilReached())
        eligible = [
            w
            for w in waiting
            if not self.draining() and (not w.draining or not w.draining())
        ]
        if account.state is AdmissionState.PROBING:
            if account.probe_owner is not None or not eligible:
                await self._notify_waiters(provider_id, provider)
                return
            if not account.probe_checked:
                account.probe_checked = True
                if provider.get("oauth_runtime_type") == "codex":
                    result = await self.probe(provider_id)
                    if isinstance(result, Allowed):
                        await self._transition(provider_id, AllowedSignal(now))
                    elif isinstance(result, StillLimited):
                        await self._transition(
                            provider_id, QuotaFailure(result.resets_at, None)
                        )
                        return
            if account.state is AdmissionState.PROBING:
                self._grant(min(eligible, key=lambda w: w.sequence), probe=True)
                await self._notify_waiters(provider_id, provider)
                return
        if account.state is not AdmissionState.OPEN:
            await self._notify_waiters(provider_id, provider)
            return
        paused = await self._background_paused(provider_id)
        eligible.sort(
            key=lambda w: (
                effective_lane(w.lane, (now - w.arrived).total_seconds()),
                w.sequence,
            )
        )
        limit = provider.get("max_concurrency")
        for waiter in eligible:
            if waiter.lane is Lane.BACKGROUND and paused:
                continue
            count = sum(g.provider_id == provider_id for g in self.running.values())
            over = (
                waiter.lane is Lane.INTERACTIVE
                and (now - waiter.arrived).total_seconds() >= INTERACTIVE_SECONDS
            )
            if limit is None or count < limit or over:
                if limit is not None and count >= limit:
                    self._overadmitted()
                self._grant(waiter)
        await self._notify_waiters(provider_id, provider)

    async def _notify_waiters(self, provider_id: str, provider: dict) -> None:
        account = self._state(provider_id)
        limit = provider.get("max_concurrency")
        waiting = [w for w in self.waiters if w.provider_id == provider_id]
        for waiter in waiting:
            if waiter not in self.waiters or waiter.on_wait is None:
                continue
            if account.state is AdmissionState.FROZEN:
                reset = (
                    (account.until - timedelta(seconds=60))
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M %Z")
                    if account.until_known
                    else "unknown"
                )
                message = f"{provider['name']} is out of quota; resets at {reset}, continues then"
            else:
                count = sum(g.provider_id == provider_id for g in self.running.values())
                message = f"Waits for {provider['name']}'s slot ({count} running, limit {limit if limit is not None else 'unlimited'})"
            if message != waiter.last_message:
                waiter.last_message = message
                await waiter.on_wait(message)

    async def acquire(
        self,
        provider_id: str,
        lane: Lane,
        owner: str,
        *,
        parent: str | None = None,
        on_wait=None,
        draining=None,
    ) -> Grant:
        if not self.active:
            return Grant(provider_id, owner, lane)
        waiter = None
        try:
            async with self.lock:
                if (
                    parent is not None
                    and parent in self.running
                    and not self.draining()
                    and (not draining or not draining())
                ):
                    grant = Grant(
                        provider_id, owner, self.running[parent].lane, borrowed=True
                    )
                    self.running[owner] = grant
                    return grant
                self.sequence += 1
                waiter = Waiter(
                    provider_id,
                    owner,
                    lane,
                    self.now(),
                    self.sequence,
                    asyncio.get_running_loop().create_future(),
                    draining,
                    on_wait,
                )
                self.waiters.append(waiter)
                while not waiter.ready.done():
                    await self._pump(provider_id)
                    # A channel notification must never hold the all-account lock.
                    if self.notices:
                        self.lock.release()
                        try:
                            await self._send_notices()
                        finally:
                            await self.lock.acquire()
                    if waiter.ready.done():
                        break
                    try:
                        await asyncio.wait_for(self.lock.wait(), RECHECK_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                return waiter.ready.result()
        except asyncio.CancelledError:
            # A cancellation after a grant must not leak its account slot.
            await self.release(owner)
            raise
        except Exception:
            logger.exception("Account admission failed; admitting run")
            return Grant(provider_id, owner, lane)
        finally:
            if waiter is not None and waiter in self.waiters:
                self.waiters.remove(waiter)

    @asynccontextmanager
    async def slot(
        self,
        provider_id: str,
        lane: Lane,
        *,
        owner: str,
        parent: str | None = None,
        on_wait=None,
        draining=None,
    ):
        if not self.active:
            yield Grant(provider_id, owner, lane)
            return
        grant = await self.acquire(
            provider_id, lane, owner, parent=parent, on_wait=on_wait, draining=draining
        )
        token = _current_owner.set(owner)
        failed = False
        try:
            yield grant
        except BaseException:
            failed = True
            raise
        finally:
            _current_owner.reset(token)
            try:
                if self.active and grant.probe and not grant.reported:
                    await self.transition(provider_id, ProbeEnded(False, failed))
                await self.release(owner)
            except Exception:
                logger.exception("Could not release account admission slot")

    async def register(self, provider_id: str, lane: Lane, owner: str) -> None:
        if self.active:
            async with self.lock:
                self.running[owner] = Grant(provider_id, owner, lane)
                self.lock.notify_all()

    async def release(self, owner: str) -> None:
        if not self.active:
            self.running.pop(owner, None)
            return
        async with self.lock:
            self.running.pop(owner, None)
            self.lock.notify_all()

    async def wake(self) -> None:
        async with self.lock:
            self.lock.notify_all()

    async def snapshot(self, provider_id: str) -> dict[str, Any]:
        async with self.lock:
            provider = await self._account(provider_id)
            account = self._state(provider_id)
            waiting = Counter(
                w.lane.name.lower()
                for w in self.waiters
                if w.provider_id == provider_id
            )
            return {
                "state": account.state.value,
                "until": account.until.isoformat() if account.until else None,
                "until_known": account.until_known,
                "waiting": {
                    lane.name.lower(): waiting[lane.name.lower()] for lane in Lane
                },
                "running": sum(
                    g.provider_id == provider_id for g in self.running.values()
                ),
                "limit": provider.get("max_concurrency"),
            }


_controller = Controller()


def enabled() -> bool:
    return _controller.active


async def initialize() -> None:
    try:
        active = (await system_settings.get_settings([KEY])).get(KEY) == "true"
    except Exception:
        logger.exception("Could not load account admission setting; admitting runs")
        active = False
    if active:
        try:
            await _controller.load_states()
        except Exception:
            logger.exception("Could not restore account admission state")
    await _controller.set_enabled(active)


async def set_enabled(value: bool) -> None:
    if value and not enabled():
        try:
            await _controller.load_states()
        except Exception:
            logger.exception("Could not restore account admission state")
    await _controller.set_enabled(value)


def configure_drain(draining: Callable[[], bool]) -> None:
    _controller.draining = draining


def configure_probe(probe: Probe) -> None:
    _controller.probe = probe


slot = _controller.slot
transition = _controller.transition
register = _controller.register
release = _controller.release
wake = _controller.wake
snapshot = _controller.snapshot
daily_overadmissions = _controller.daily_overadmissions


async def recorded(
    provider_id: str,
    owner: str | None,
    record_id: int | None,
    failure,
    *,
    interrupted: bool = False,
) -> None:
    if not enabled():
        return
    from .failures import FailureClass

    try:
        grant = _controller.running.get(owner) or _controller.running.get(
            _current_owner.get()
        )
        quota = failure is not None and failure.cls is FailureClass.QUOTA
        if quota:
            await transition(provider_id, QuotaFailure(failure.resets_at, record_id))
        elif grant is not None and grant.probe:
            await transition(
                provider_id, ProbeEnded(False, interrupted or failure is not None)
            )
        if grant is not None:
            grant.reported = True
    except Exception:
        logger.exception("Could not apply run result to account admission")


async def signal(
    provider_id: str, record_id: int | None, windows: list[tuple[str, dict]]
) -> None:
    if not enabled():
        return
    try:
        if any(w["status"] == "allowed" for _, w in windows):
            record = (
                await llm_records.get_record(record_id)
                if record_id is not None
                else None
            )
            if record is not None:
                started_at = record["created_at"]
                if isinstance(started_at, str):
                    started_at = datetime.fromisoformat(started_at)
                await transition(provider_id, AllowedSignal(started_at))
    except Exception:
        logger.exception("Could not apply observed account limit signal")
    finally:
        await wake()


def record_metadata() -> dict[str, int]:
    """Persist only the enabled grant's typed lane for children launched later."""
    if not enabled():
        return {}
    grant = _controller.running.get(_current_owner.get())
    return {"admission_lane": grant.lane.value} if grant is not None else {}


async def parent_for(record_id: int | None) -> tuple[str | None, Lane]:
    if not enabled():
        return None, Lane.BACKGROUND
    current = _current_owner.get()
    if current in _controller.running:
        return current, _controller.running[current].lane
    try:
        record = (
            await llm_records.get_record(record_id) if record_id is not None else None
        )
        if record is None:
            return None, Lane.BACKGROUND
        owner = record.get("agent_id")
        if owner in _controller.running:
            return owner, _controller.running[owner].lane
        metadata = record.get("metadata")
        saved_lane = (
            metadata.get("admission_lane") if isinstance(metadata, Mapping) else None
        )
        if type(saved_lane) is int and saved_lane in {lane.value for lane in Lane}:
            return None, Lane(saved_lane)
        from ..db.connection import get_pool

        kind = await get_pool().fetchval(
            "SELECT run_kind FROM llm_records WHERE id=$1", record_id
        )
        role = (
            AgentType(kind)
            if kind in {r.value for r in AgentType}
            else AgentType.WORKER
        )
        return None, await worker_lane(record.get("task_id"), {}, role=role)
    except Exception:
        logger.exception("Could not resolve parent account admission slot")
        return None, Lane.BACKGROUND


async def worker_lane(
    task_id: str | None,
    payload: Mapping[str, Any],
    *,
    role: AgentType = AgentType.WORKER,
) -> Lane:
    if not enabled():
        return Lane.USER_TASK
    try:
        from ..db import tasks
        from ..db.connection import get_pool

        task = await tasks.get_task(task_id) if task_id else None
        raw_trigger = payload.get("attempt_trigger", payload.get("trigger"))
        same_attempt = (
            payload.get("same_attempt") is True or raw_trigger == "same_attempt"
        )
        attempt_id = payload.get("attempt_id")
        if type(attempt_id) is int:
            attempt = await get_pool().fetchrow(
                "SELECT trigger,worker_runs FROM step_attempts WHERE id=$1", attempt_id
            )
            if attempt is not None:
                raw_trigger = raw_trigger or attempt["trigger"]
                if role is AgentType.WORKER:
                    same_attempt = same_attempt or attempt["worker_runs"] > 0
                elif role is AgentType.REVIEWER:
                    # A panel's independent seats share the attempt. Only a prior
                    # record of this seat makes its dispatch recovery.
                    same_attempt = same_attempt or await get_pool().fetchval(
                        "SELECT EXISTS (SELECT 1 FROM llm_records WHERE attempt_id=$1 "
                        "AND task_id=$2 AND run_kind='reviewer' AND metadata->>'step_id'=$3)",
                        attempt_id,
                        task_id,
                        payload.get("task_id"),
                    )
        trigger = (
            attempts.Trigger(raw_trigger)
            if raw_trigger in {x.value for x in attempts.Trigger}
            else None
        )
        # schedule_runs.task_id has a dedicated index; no scan of run records.
        schedule_id = task.get("schedule_id") if task else None
        firing = (
            await get_pool().fetchval(
                "SELECT trigger FROM schedule_runs WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1",
                task_id,
            )
            if schedule_id
            else None
        )
        return lane_for(
            role=role,
            schedule_id=schedule_id,
            trigger=trigger,
            same_attempt=same_attempt,
            recovered=payload.get("recovered") is True
            or payload.get("restart") is True,
            run_now=firing == "manual",
        )
    except Exception:
        logger.exception(
            "Could not determine account admission lane; using user-task priority"
        )
        return Lane.USER_TASK


async def observed(
    provider_id: str, windows: list[tuple[str, dict]], record_id: int | None
) -> None:
    await signal(provider_id, record_id, windows)
