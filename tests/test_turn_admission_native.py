"""Native-input slot retention preserves probe failures and shutdown cleanup."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, turn_admission
from looper.agents.mid_turn_input import MidTurnInputs
from looper.agents.message import MessageType
from looper.agents.ops import TurnOrigin
from looper.agents.tiers.turn_token import TurnToken
from looper.db import provider_admission, provider_limits


@pytest.fixture(autouse=True)
def account_storage(monkeypatch):
    monkeypatch.setattr(provider_limits, "windows", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider_admission, "save", AsyncMock(return_value=1))
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())


@pytest.mark.parametrize("retained", [False, True])
def test_manager_probe_failure_survives_native_lease_retention(monkeypatch, retained):
    async def run():
        controller = admission.Controller()
        controller.active = True
        controller._state("synthetic").state = admission.AdmissionState.PROBING
        controller._state("synthetic").probe_checked = True
        monkeypatch.setattr(controller, "_account", AsyncMock(return_value={"name": "Synthetic"}))
        transition = AsyncMock(wraps=controller.transition)
        monkeypatch.setattr(controller, "transition", transition)
        monkeypatch.setattr(admission, "_controller", controller)
        monkeypatch.setattr(admission, "slot", controller.slot)
        session = SimpleNamespace(key="main")
        prepared = SimpleNamespace(session=session, turn=SimpleNamespace(origin=TurnOrigin.USER),
            launch=SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(id="synthetic"))),
            envelope=SimpleNamespace(message_type=MessageType.USER_MESSAGE))
        token = TurnToken("synthetic", None, "web", "synthetic", controller.now())

        @turn_admission.admitted_manager_turn
        async def turn(agent, token, prepared):
            if retained:
                token.admission_lease.retain()
            raise ValueError("synthetic runtime failure")

        with pytest.raises(ValueError, match="synthetic runtime failure"):
            await turn(SimpleNamespace(agent_id="manager"), token, prepared)
        if retained:
            assert controller.running and not transition.called
            await token.admission_lease.release()
        assert transition.call_args.args == ("synthetic", admission.ProbeEnded(False, True))
        assert controller._state("synthetic").state is admission.AdmissionState.PROBING
        assert not controller.running

    asyncio.run(run())


@pytest.mark.parametrize("admission_on", [False, True])
def test_native_turn_transfers_slot_until_shutdown(monkeypatch, admission_on):
    async def run():
        controller = admission.Controller()
        controller.active = True
        monkeypatch.setattr(controller, "_account", AsyncMock(return_value={"name": "Synthetic"}))
        monkeypatch.setattr(admission, "_controller", controller)
        monkeypatch.setattr(admission, "slot", controller.slot)
        agent = SimpleNamespace(agent_id="manager")
        agent._mid_turn = MidTurnInputs(agent)
        session = SimpleNamespace(key="main")
        prepared = SimpleNamespace(session=session, turn=SimpleNamespace(origin=TurnOrigin.USER),
            launch=SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(id="synthetic"))),
            envelope=SimpleNamespace(message_type=MessageType.USER_MESSAGE))
        token = TurnToken("synthetic", None, "web", "synthetic", controller.now())

        @turn_admission.admitted_manager_turn
        async def turn(agent, token, prepared):
            token.admission_lease.retain()
            agent._mid_turn._admissions["native-input" if token.message_id == "synthetic" else "next-input"] = (
                token.admission_lease)

        await turn(agent, token, prepared)
        assert list(controller.running) == ["manager:main"]
        lease = token.admission_lease
        assert lease.references == 1
        native = TurnToken("native-input", None, "web", "synthetic", controller.now())
        monkeypatch.setattr(admission, "enabled", lambda: admission_on)
        await turn(agent, native, prepared)
        assert token.admission_lease is None and native.admission_lease is lease
        assert lease.references == 1 and list(controller.running) == ["manager:main"]
        await agent._mid_turn.shutdown()
        assert not controller.running
        assert native.admission_lease is None

    asyncio.run(run())
