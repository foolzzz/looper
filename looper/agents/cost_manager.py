"""Manager admission identity and reply-only notices; never model-visible context."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from dataclasses import replace
import logging

from ..db import system_settings
from . import cost_guard, cost_guard_settings, session_budget
from .message import BOT_CHANNEL_TYPES, builtin_channel_id
from .operator_identity import is_operator, operator_sender_id
from .ops import Turn, TurnState
from .turn_context import SpeakerKind
from .session_switch import SwitchReason

DENIAL_REPLY = "今天的花费已经到了上限，这次没有启动管家回合。请操作者在设置的花费页调整上限。"
_turn: ContextVar[Turn | None] = ContextVar("cost_guard_manager_turn", default=None)
_defer_rotation: ContextVar[bool] = ContextVar("cost_guard_defer_rotation", default=False)
_AUTOMATIC = (SwitchReason.SESSION_BOUND, SwitchReason.CONTEXT_FULL)
logger = logging.getLogger(__name__)


class _MaintenanceDenied(cost_guard.Denied):
    """A handoff was refused before it could replace the retained session."""


def begin_turn(turn: Turn) -> None:
    # begin_turn is synchronous; identity reads and the durable daily claim happen at admission.
    _turn.set(turn)


def end_turn(turn: Turn) -> None:
    if _turn.get() is turn:
        _turn.set(None)


async def _operator(turn: Turn | None) -> bool:
    if (turn is None or turn.state is not TurnState.ANSWERING or turn.autonomous
            or turn.group or turn.client_id is not None):
        return False
    if turn.speaker is not None:
        return turn.speaker is SpeakerKind.OPERATOR
    operator_id = None
    if turn.channel in BOT_CHANNEL_TYPES:
        operator_id = await operator_sender_id(turn.channel_id or builtin_channel_id(turn.channel))
    return is_operator(channel=turn.channel, group=turn.group, sender=turn.sender, client_id=turn.client_id,
                       operator_id=operator_id)


async def admit(guard, launch) -> bool:
    """Return the operator's global-cap exception; other over-cap turns raise a fixed typed denial.

    Called under the guard's admission lock, after its day counters are refreshed. No lock is reacquired.
    """
    if guard.config.mode is not cost_guard_settings.Mode.ENFORCE:
        return False
    scope = await _over_cap(guard, launch)
    if scope is None:
        return False
    turn = _turn.get()
    if not await _operator(turn):
        raise cost_guard.Denied(reply=DENIAL_REPLY)
    day = guard.day or guard.day_start(guard.clock())
    if await system_settings.claim("cost_guard_manager_line", day.isoformat(), pool=guard.pool):
        turn.blocks.append(f"今天的花费已经到了上限（${scope.limit_usd:.2f}），后台任务已暂停")
    return True


async def _over_cap(guard, launch):
    currency = cost_guard.currency_of(launch)
    cap = launch.max_budget_usd
    turn_cap = await session_budget.turn_budget_usd(pool=guard.pool)
    cap = min(cap, turn_cap) if cap is not None and turn_cap is not None else turn_cap or cap
    estimate, _ = guard.estimate(cost_guard.RunKind.MANAGER, currency, cap)
    scope = guard.scopes(cost_guard.RunKind.MANAGER, None, None, currency)[-1]
    key = (*scope.key, currency)
    if scope.limit_usd is not None and guard._spent[key] + guard._reserved[key] + estimate > scope.limit_usd:
        return scope
    return None


def maintenance(method):
    """Handoffs have no operator exception; explicit profile changes may rotate without a summary."""
    @wraps(method)
    async def wrapped(manager, switch, *args, **kwargs):
        if cost_guard.current().config.mode is not cost_guard_settings.Mode.ENFORCE:
            return await method(manager, switch, *args, **kwargs)
        token = _turn.set(None)
        try:
            return await method(manager, switch, *args, **kwargs)
        except cost_guard.Denied as exc:
            if switch.reason in _AUTOMATIC and _defer_rotation.get():
                raise _MaintenanceDenied(exc.reply) from exc
            return None
        finally:
            _turn.reset(token)
    return wrapped


def defer_maintenance(method):
    """Defer automatic rotations for the whole run, including its conversation and recording settings."""
    @wraps(method)
    async def wrapped(manager, token, prepared):
        guard, process = cost_guard.current(), manager._process
        if (guard.config.mode is not cost_guard_settings.Mode.ENFORCE or prepared.switch is None
                or prepared.switch.reason not in _AUTOMATIC or process is None):
            return await method(manager, token, prepared)
        old, new = process.launch, prepared.launch
        # Never undo a selected profile or weaken a newly required outbound review boundary.
        if (old.profile.id, old.profile.provider.id, old.runtime.type, old.model, old.tool_hook is None) != (
                new.profile.id, new.profile.provider.id, new.runtime.type, new.model, new.tool_hook is None):
            return await method(manager, token, prepared)

        def retained():
            return replace(prepared, switch=None, launch=old, signature=process.signature,
                           system_prompt=process.system_prompt,
                           prompt_file=process.system_prompt.write_file(manager._session_root()))

        try:
            async with guard._lock:
                if guard.day != guard.day_start(guard.clock()):
                    await guard._rebuild()
                over = await _over_cap(guard, old)
        except Exception:
            logger.warning("cannot precheck Manager maintenance; keeping normal admission", exc_info=True)
            over = None
        if over is not None:
            return await method(manager, token, retained())
        context = _defer_rotation.set(True)
        try:
            return await method(manager, token, prepared)
        except _MaintenanceDenied:
            from .tiers.turn_token import TurnPhase

            token.check()
            token.phase = TurnPhase.RUNNING if prepared.retried else TurnPhase.PREPARING
            token.failed_step = None
            return await method(manager, token, retained())
        finally:
            _defer_rotation.reset(context)
    return wrapped
