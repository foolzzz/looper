"""Global caps affect Manager admission without changing model-visible input."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg, cost_manager, session_budget
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.turn_context import SpeakerKind
from looper.agents.session_switch import SwitchReason

NOW = datetime(2026, 11, 22, 18, tzinfo=timezone.utc)


def setup(monkeypatch, *, mode=None, usd=2, auth="api_key"):
    guard = cg.Guard(Config(mode=mode or Mode.ENFORCE, daily_usd=usd, daily_equivalent_usd=usd),
                     clock=lambda: NOW)
    guard.day = guard.day_start(NOW)
    currency = cg.Currency.EQUIVALENT if auth == "oauth" else cg.Currency.PROVIDER
    guard._spent[cg.BudgetScope.GLOBAL_DAY, "global", currency] = 2
    monkeypatch.setattr(cg, "_guard", guard)
    monkeypatch.setattr(session_budget, "turn_budget_usd", AsyncMock(return_value=None))
    claimed = set()

    async def claim(key, value, **kwargs):
        pair = key, value
        if pair in claimed:
            return False
        claimed.add(pair)
        return True

    monkeypatch.setattr(cost_manager.system_settings, "claim", AsyncMock(side_effect=claim))
    launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth=auth)), max_budget_usd=1)
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/")
    return guard, launch, ops


@pytest.mark.parametrize("mode", [Mode.OFF, Mode.OBSERVE])
def test_inactive_modes_never_read_identity_or_add_blocks(monkeypatch, mode):
    async def check():
        guard, launch, ops = setup(monkeypatch, mode=mode)
        monkeypatch.setattr(cost_manager, "operator_sender_id", AsyncMock(side_effect=AssertionError("identity I/O")))
        turn = Turn(channel="telegram", chat="synthetic", sender="synthetic-operator", message_id="one")
        ops.begin_turn(turn)
        assert await cost_manager.admit(guard, launch) is False
        ops.end_turn(turn)
        assert turn.blocks == []
        cost_manager.system_settings.claim.assert_not_awaited()
        session_budget.turn_budget_usd.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize("auth", ["api_key", "oauth"])
def test_operator_dashboard_passes_with_a_code_line_once_per_scheduler_day(monkeypatch, auth):
    async def check():
        guard, launch, ops = setup(monkeypatch, auth=auth)
        for number in range(2):
            turn = Turn(channel="web", chat="synthetic", message_id=str(number))
            ops.begin_turn(turn)
            assert await cost_manager.admit(guard, launch) is True
            assert turn.blocks == (["今天的花费已经到了上限（$2.00），后台任务已暂停"] if number == 0 else [])
            ops.end_turn(turn)
        guard.day += timedelta(days=1)
        turn = Turn(channel="web", chat="synthetic", message_id="next-day")
        ops.begin_turn(turn)
        assert await cost_manager.admit(guard, launch) is True
        assert len(turn.blocks) == 1
        ops.end_turn(turn)
    asyncio.run(check())


@pytest.mark.parametrize("fields", [
    {"channel": "web", "client_id": "synthetic-client"},
    {"channel": "telegram", "sender": "another"},
    {"channel": "telegram", "sender": "synthetic-operator", "group": True,
     "speaker": SpeakerKind.OPERATOR},
    {"channel": "web", "origin": TurnOrigin.COMPLETION},
])
def test_nonoperator_and_completion_turns_receive_typed_denial(monkeypatch, fields):
    async def check():
        guard, launch, ops = setup(monkeypatch)
        monkeypatch.setattr(cost_manager, "operator_sender_id", AsyncMock(return_value="synthetic-operator"))
        turn = Turn(chat="synthetic", message_id="one", **fields)
        ops.begin_turn(turn)
        with pytest.raises(cg.Denied) as denied:
            await cost_manager.admit(guard, launch)
        assert denied.value.reply == cost_manager.DENIAL_REPLY
        assert turn.blocks == []
        ops.end_turn(turn)
    asyncio.run(check())


def test_only_operator_dm_identity_can_bypass_the_cap(monkeypatch):
    async def check():
        guard, launch, ops = setup(monkeypatch)
        monkeypatch.setattr(cost_manager, "operator_sender_id", AsyncMock(return_value="synthetic-operator"))
        turn = Turn(channel="telegram", channel_id="synthetic-bot", chat="synthetic",
                    sender="synthetic-operator", message_id="one")
        ops.begin_turn(turn)
        assert await cost_manager.admit(guard, launch) is True
        assert len(turn.blocks) == 1
        cost_manager.operator_sender_id.assert_awaited_once_with("synthetic-bot")
        ops.end_turn(turn)
        # Maintenance after a reply cannot inherit the user's exception.
        with pytest.raises(cg.Denied):
            await cost_manager.admit(guard, launch)
    asyncio.run(check())


def test_under_cap_has_no_identity_or_claim_io(monkeypatch):
    async def check():
        guard, launch, ops = setup(monkeypatch, usd=4)
        turn = Turn(channel="web", chat="synthetic", message_id="one")
        ops.begin_turn(turn)
        assert await cost_manager.admit(guard, launch) is False
        assert turn.blocks == []
        cost_manager.system_settings.claim.assert_not_awaited()
        ops.end_turn(turn)
    asyncio.run(check())


def test_maintenance_denial_retains_session_and_restores_the_operator_turn(monkeypatch):
    async def check():
        guard, launch, ops = setup(monkeypatch)
        turn = Turn(channel="web", chat="synthetic", message_id="one")
        ops.begin_turn(turn)
        state = {"session_id": "retained", "runs": 0}

        @cost_manager.maintenance
        async def handoff(manager, switch):
            await cost_manager.admit(guard, launch)
            state.update(session_id="rotated", runs=1)

        await handoff(None, SimpleNamespace(reason=SwitchReason.PROFILE_CHANGED))
        assert state == {"session_id": "retained", "runs": 0}
        assert await cost_manager.admit(guard, launch) is True
        assert len(turn.blocks) == 1
        ops.end_turn(turn)
    asyncio.run(check())


def test_automatic_rotation_defers_the_whole_run_and_keeps_its_pending_switch(monkeypatch):
    from looper.agents.tiers.manager import Prepared

    async def check():
        guard, launch, ops = setup(monkeypatch)
        launch.runtime = SimpleNamespace(type="claude_code")
        launch.profile.id = "synthetic-profile"
        launch.profile.provider.id = "synthetic-provider"
        launch.model = "synthetic-model"
        launch.tool_hook = None
        prompt = SimpleNamespace(write_file=lambda root: "old-prompt")
        process = SimpleNamespace(launch=launch, signature="old-signature", system_prompt=prompt)
        switch = SimpleNamespace(reason=SwitchReason.SESSION_BOUND)
        session = SimpleNamespace(pending_switch=switch, session_id="retained")
        manager = SimpleNamespace(_process=process, _session_root=lambda: "synthetic-root")
        prepared = Prepared(switch=switch, launch=launch, signature="new-signature", session=session,
                            system_prompt=prompt, prompt_file="new-prompt")
        seen = []

        @cost_manager.defer_maintenance
        async def run(instance, token, candidate):
            seen.append(candidate)
            return "answer"

        assert await run(manager, None, prepared) == "answer"
        assert seen[0].switch is None
        assert seen[0].launch is launch and seen[0].signature == "old-signature"
        assert session.session_id == "retained" and session.pending_switch is switch
        guard.config = Config(mode=Mode.ENFORCE, daily_usd=10)
        assert await run(manager, None, prepared) == "answer"
        assert seen[1] is prepared  # the next turn retries automatic maintenance after room is available
    asyncio.run(check())
