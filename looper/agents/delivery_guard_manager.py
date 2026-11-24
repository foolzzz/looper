"""Manager I/O for delivery recovery; the policy stays in delivery_guard."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import chat_settings
from .claude_cli import ResidentClaude, answer_text
from .delivery_guard import (
    BlockKind, DeliveryDebt, FAILURE_NOTICE, FOLLOWUP_MESSAGE, FollowupAction, GuardState,
    TurnEnding, ending_for, followup_action, initial_debt, observe_block,
)
from .runtimes import Capability

if TYPE_CHECKING:
    from .ops import Turn
    from .tiers.manager import ManagerAgent, Prepared
    from .tiers.turn_token import TurnToken

logger = logging.getLogger(__name__)
_reminders: dict[TurnToken, set[asyncio.Task[None]]] = {}


async def finish(token: TurnToken) -> None:
    """Withdraw unfinished advisory inputs when their owning model turn ends."""
    tasks = _reminders.pop(token, set())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def initialize(turn: Turn) -> None:
    turn.debt = initial_debt(enabled=chat_settings.delivery_guard_enabled(), origin=turn.origin)


def awaiting_followup(prepared: Prepared, result: dict[str, Any] | None, text_parts: list[str]) -> bool:
    """Keep the user's stop token live across successful empty model turns only."""
    turn = prepared.turn
    return bool(turn is not None and turn.debt is DeliveryDebt.OWED and not turn.refused
                and result is not None and result.get("subtype") == "success" and not result.get("is_error")
                and not prepared.launch.runtime.budget_error(result) and not answer_text(result, text_parts))


def metadata(prepared: Prepared, *, reminders: int = 0) -> dict[str, Any]:
    guard: dict[str, Any] = {}
    if prepared.delivery_followup:
        guard.update({"followup": prepared.delivery_followup, "for": prepared.turn.message_id})
    if reminders:
        guard["reminders"] = reminders
    return {"delivery_guard": guard} if guard else {}


async def run(manager: ManagerAgent, token: TurnToken, prepared: Prepared) -> str:
    """One user reply, with separately recorded hidden turns and the original cancellation token."""
    from .tiers.manager import NoReply, OverCap, RunState, TurnFailed
    from .tiers.turn_token import TurnStopped

    try:
        return await manager._run_prepared(token, prepared)
    except NoReply:
        turn = prepared.turn
        if (turn is None or turn.refused or not token.live or followup_action(
                turn.debt, ending=TurnEnding.EMPTY, followups=0) is not FollowupAction.FOLLOWUP):
            raise
    finally:
        await finish(token)
    followups = 0
    ending = TurnEnding.EMPTY
    while followup_action(turn.debt, ending=ending, followups=followups) is FollowupAction.FOLLOWUP:
        token.check()
        followups += 1
        prepared = replace(prepared, message=FOLLOWUP_MESSAGE, turn_text=FOLLOWUP_MESSAGE, note="",
                           quoted=None, state=None, switch=None, retried=True,
                           run=RunState(persona_scope=prepared.run.persona_scope),
                           delivery_followup=followups)
        try:
            return await manager._run_prepared(token, prepared)
        except TurnStopped:
            raise
        except Exception as exc:
            if isinstance(exc, OverCap) and prepared.session.budget is not None:
                raise
            ending = ending_for(exc)
        finally:
            await finish(token)
    token.check()
    token.settle()
    raise TurnFailed(FAILURE_NOTICE, "no reply after follow-ups")


async def observe(state: GuardState, block: dict[str, Any], *, prepared: Prepared, manager: ManagerAgent,
                  token: TurnToken) -> GuardState:
    """Complete top-level blocks only. A reminder is written only while that resident turn is live."""
    kind = {"text": BlockKind.TEXT, "tool_use": BlockKind.TOOL_USE}.get(block.get("type"), BlockKind.OTHER)
    resident = manager._claude
    # Only the verified stream-json session kind; keeper and runner sessions are not eligible.
    capabilities = (prepared.launch.profile.runtime.capabilities
                    if type(resident) is ResidentClaude and resident.can_write_mid_turn
                    and not prepared.launch.runtime.hosts_runs else frozenset())
    state, reminder = observe_block(state, kind, text=str(block.get("text") or ""), capabilities=capabilities)
    if prepared.turn is not None:
        prepared.turn.debt = state.debt
    if reminder is not None and Capability.MID_TURN_INPUT in capabilities:
        async def remind() -> None:
            from .tiers.turn_token import TurnStopped

            try:
                if token.live:
                    written = await token.step("delivery_reminder", resident.write_mid_turn(reminder))
                    if written:
                        await prepared.run.recorder.annotate(metadata(prepared, reminders=state.reminders))
            except TurnStopped:
                pass
            except Exception:
                logger.warning("manager: delivery reminder failed", exc_info=True)

        # The stream observer must keep draining tool/text/result events while
        # Claude decides whether to fold this advisory input into its reply.
        task = asyncio.create_task(remind())
        tasks = _reminders.setdefault(token, set())
        tasks.add(task)

        def done(task: asyncio.Task[None]) -> None:
            tasks.discard(task)
            if not tasks and _reminders.get(token) is tasks:
                _reminders.pop(token, None)

        task.add_done_callback(done)
    return state
