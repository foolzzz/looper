"""Account slots for Looper-started Manager turns and Planner exchanges."""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone, tzinfo
from functools import wraps
from types import TracebackType

from . import admission, lanes, turn_context
from .agent_settings import AgentType
from .lanes import Lane
from .message import MessageType
from .ops import TurnOrigin
from .tiers.planner_protocol import TurnKind
from .tiers.turn_token import TurnPhase, TurnToken

logger = logging.getLogger(__name__)
# Share the grant owner with record binding and nested tool runs.
_current_owner = admission._current_owner


class RetryQuotaProbe(Exception):
    """A retained message's probe failed on quota before any model output."""


@dataclass
class ManagerLease:
    """One session slot, retained while an injected input may run natively."""

    token: TurnToken
    provider_id: str
    stack: AsyncExitStack
    references: int = 1
    failure: tuple[type[BaseException] | None, BaseException | None, TracebackType | None] = (None, None, None)

    def retain(self) -> None:
        self.references += 1

    def bind(self, token: TurnToken) -> None:
        """The queued native turn takes ownership when it claims a retained slot."""
        if self.token.admission_lease is self:
            self.token.admission_lease = None
        self.token = token
        token.admission_lease = self

    async def release(self, failure=(None, None, None)) -> None:
        if self.failure[1] is None and failure[1] is not None:
            self.failure = failure
        self.references -= 1
        if self.references == 0:
            if self.token.admission_lease is self:
                self.token.admission_lease = None
            await self.stack.__aexit__(*self.failure)


def manager_turn_lane(message_type: MessageType, origin: TurnOrigin | None) -> Lane | None:
    if origin is not None:
        return lanes.manager_lane(origin)
    return Lane.INTERACTIVE if message_type is MessageType.USER_MESSAGE else Lane.BACKGROUND


def planner_turn_lane(kind: TurnKind | None, *, task_lane: Lane, recovered: bool) -> Lane:
    if kind in (TurnKind.CHAT, TurnKind.GATE):
        return Lane.INTERACTIVE
    if kind is TurnKind.RESTART or recovered:
        return Lane.RECOVERY
    return task_lane


def freeze_reply(account: str, windows: Sequence[str], resets_at: datetime | None, zone: tzinfo) -> str:
    if resets_at is None:
        return (f"{account} 的额度已用完，重置时间未知；Looper 会定时重新检查，"
                "恢复后自动回答你的消息。发 /stop 可取消。")
    names = f"（{'、'.join(windows)}）" if windows else ""
    return (f"{account} 的额度已用完{names}，{resets_at.astimezone(zone):%m-%d %H:%M} 重置。"
            "你的消息已保留，额度恢复后自动回答；发 /stop 可取消。")


async def scheduled_quota_deadline(task_id: str, schedule_id: str | None, service, *, started_at=None, pool=None) -> datetime:
    """Persist the first quota wait, capped at six hours or one schedule period, whichever is shorter."""
    from ..db import schedules
    from . import cron

    maximum = 6 * 3600.0
    if schedule_id:
        row = await schedules.get_schedule(schedule_id, **({"pool": pool} if pool is not None else {}))
        if row is not None:
            now = started_at or (service.clock() if callable(getattr(service, "clock", None)) else datetime.now(timezone.utc))
            rule, zone = cron.parse(row["cron"]), cron.zone(row["timezone"])
            first = cron.next_fire(rule, zone, now)
            following = cron.next_fire(rule, zone, first) if first else None
            if first is not None and following is not None:
                maximum = min(maximum, (following - first).total_seconds())
    return await service.quota_deadline(task_id, maximum, **({"started_at": started_at} if started_at is not None else {}))


def admitted_manager_turn(method):
    @wraps(method)
    async def run(agent, token, prepared):
        if not admission.enabled():
            mid_turn = getattr(agent, "_mid_turn", None)
            lease = mid_turn.claim_admission(token, prepared.launch.profile.provider.id) if mid_turn else None
            if lease is not None:
                lease.bind(token)
            try:
                return await method(agent, token, prepared)
            finally:
                if lease is not None:
                    await lease.release(sys.exc_info())
        try:
            lane = manager_turn_lane(prepared.envelope.message_type,
                                     prepared.turn.origin if prepared.turn is not None else None)
            owner = f"{agent.agent_id}:{prepared.session.key}"
        except Exception:
            logger.exception("Manager turn admission lookup failed; admitting turn")
            return await method(agent, token, prepared)
        if lane is None or _current_owner.get() == owner:
            return await method(agent, token, prepared)
        mid_turn = getattr(agent, "_mid_turn", None)
        if mid_turn is not None and mid_turn.native_completed(token, prepared):
            return await method(agent, token, prepared)  # already ran under the previous turn's slot
        told = logged = False

        async def on_wait(text):
            nonlocal logged
            if not logged:
                logged = True
                logger.info("Manager message %s: %s", token.message_id, text)

        async def on_frozen(frozen):
            nonlocal told
            if lane is Lane.INTERACTIVE and not told:
                told = True
                await agent._freeze_notice_once(prepared.envelope, freeze_reply(
                    frozen.account, frozen.windows, frozen.resets_at, turn_context.zone()))

        while True:
            lease = mid_turn.claim_admission(token, prepared.launch.profile.provider.id) if mid_turn else None
            stack = AsyncExitStack()
            try:
                token.admission_waiting = True
                try:
                    if lease is None:
                        await token.step("account_admission", stack.enter_async_context(admission.slot(
                            prepared.launch.profile.provider.id, lane, owner=owner,
                            user_message=lane is Lane.INTERACTIVE,
                            on_wait=on_wait, on_frozen=on_frozen if lane is Lane.INTERACTIVE else None)))
                        lease = ManagerLease(token, prepared.launch.profile.provider.id, stack)
                    lease.bind(token)
                    token.admission_waiting = False
                    token.check()  # a simultaneous stop and grant must not start a record
                    # token.step enters the slot in a child task; bind this task's records too.
                    context = _current_owner.set(owner)
                    try:
                        return await method(agent, token, prepared)
                    except RetryQuotaProbe:
                        token.check()
                        token.phase = TurnPhase.RUNNING if prepared.retried else TurnPhase.PREPARING
                        token.cancel.prompt_sent = False
                        token.failed_step = None
                        prepared = replace(prepared,
                            run=type(prepared.run)(persona_scope=prepared.run.persona_scope))
                    finally:
                        _current_owner.reset(context)
                finally:
                    token.admission_waiting = False
            finally:
                if lease is not None:
                    await lease.release(sys.exc_info())
                else:
                    await stack.__aexit__(*sys.exc_info())
    return run


def admitted_planner_exchange(method):
    @wraps(method)
    async def run(agent, text, process, limit, *, maintenance=False, **kwargs):
        if not admission.enabled() or _current_owner.get() == agent.agent_id:
            return await method(agent, text, process, limit, maintenance=maintenance, **kwargs)
        try:
            task_lane = getattr(agent, "_admission_task_lane", None)
            if task_lane is None:
                task_lane = await admission.worker_lane(agent._task_id, {}, role=AgentType.PLANNER)
                agent._admission_task_lane = task_lane
            lane = planner_turn_lane(None if maintenance else agent._turn_kind,
                                     task_lane=task_lane, recovered=agent._admission_recovered)
        except Exception:
            logger.exception("Planner exchange admission lookup failed; admitting turn")
            return await method(agent, text, process, limit, maintenance=maintenance, **kwargs)
        from . import failure_handling, failures

        service = failure_handling.get_service()
        if service is None or service.known_mode() is not failures.FailureTyping.ACT:
            async with admission.slot(process.launch.profile.provider.id, lane, owner=agent.agent_id,
                                      on_wait=lambda message: agent._activity("phase", message)):
                agent._admission_recovered = False
                return await method(agent, text, process, limit, maintenance=maintenance, **kwargs)
        frozen = asyncio.Event()
        deadline = None

        async def on_frozen(found):
            nonlocal deadline
            # This callback runs on the first admission refusal, before any model call can exist.
            deadline = await agent._quota_failure_deadline()
            frozen.set()

        async def waiting():
            await frozen.wait()
            delay = (max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds()) if deadline is not None
                     else 365 * 24 * 3600.0)
            elapsed = await service.wait_turn(delay, task_id=agent._task_id)
            return elapsed and deadline is not None

        async with AsyncExitStack() as stack:
            acquire = asyncio.create_task(stack.enter_async_context(admission.slot(
                process.launch.profile.provider.id, lane, owner=agent.agent_id,
                on_wait=lambda message: agent._activity("phase", message), on_frozen=on_frozen)))
            wake = asyncio.create_task(waiting())
            try:
                done, _ = await asyncio.wait((acquire, wake), return_when=asyncio.FIRST_COMPLETED)
                if wake in done:
                    from .tiers.planner import PlannerTurnError
                    from ..db.tasks import FailKind

                    expired = await wake
                    raise PlannerTurnError(
                        "the scheduled task reached its quota waiting deadline" if expired else
                        "failure handling ended while the Planner waited for quota",
                        failure=failures.Failure(failures.FailureCause.USAGE_LIMIT),
                        fail_kind=FailKind.QUOTA_EXHAUSTED if expired else None)
                await acquire
            finally:
                for job in (acquire, wake):
                    if not job.done():
                        job.cancel()
                await asyncio.gather(acquire, wake, return_exceptions=True)
            # acquire ran in a child task. This task owns its recorder and any nested tool runs.
            owner = _current_owner.set(agent.agent_id)
            try:
                agent._admission_recovered = False
                return await method(agent, text, process, limit, maintenance=maintenance, **kwargs)
            finally:
                _current_owner.reset(owner)
    return run
