"""Account slots for Looper-started Manager turns and Planner exchanges."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from datetime import datetime, tzinfo
from functools import wraps

from . import admission, lanes, turn_context
from .agent_settings import AgentType
from .lanes import Lane
from .message import MessageType
from .ops import TurnOrigin
from .tiers.planner_protocol import TurnKind

logger = logging.getLogger(__name__)
# Share the grant owner with record binding and nested tool runs.
_current_owner = admission._current_owner


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


def admitted_manager_turn(method):
    @wraps(method)
    async def run(agent, token, prepared):
        if not admission.enabled():
            return await method(agent, token, prepared)
        try:
            lane = manager_turn_lane(prepared.envelope.message_type,
                                     prepared.turn.origin if prepared.turn is not None else None)
            owner = f"{agent.agent_id}:{prepared.session.key}"
        except Exception:
            logger.exception("Manager turn admission lookup failed; admitting turn")
            return await method(agent, token, prepared)
        if lane is None or _current_owner.get() == owner:
            return await method(agent, token, prepared)
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
                await agent._send_status(prepared.envelope, freeze_reply(
                    frozen.account, frozen.windows, frozen.resets_at, turn_context.zone()))

        async with AsyncExitStack() as stack:
            token.admission_waiting = True
            try:
                await token.step("account_admission", stack.enter_async_context(admission.slot(
                    prepared.launch.profile.provider.id, lane, owner=owner,
                    on_wait=on_wait, on_frozen=on_frozen if lane is Lane.INTERACTIVE else None)))
                token.admission_waiting = False
                token.check()  # a simultaneous stop and grant must not start a record
                # token.step enters the slot in a child task; bind this task's records too.
                context = _current_owner.set(owner)
                try:
                    return await method(agent, token, prepared)
                finally:
                    _current_owner.reset(context)
            finally:
                token.admission_waiting = False
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
        async with admission.slot(process.launch.profile.provider.id, lane, owner=agent.agent_id,
                                  on_wait=lambda message: agent._activity("phase", message)):
            agent._admission_recovered = False
            return await method(agent, text, process, limit, maintenance=maintenance, **kwargs)
    return run
