"""Routing failures never borrow main's context, and starts reserve resident capacity."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings, completion_events, operator_identity, session_routing
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import manager_events
from tests.lark_cli_stand_in import OPEN_ID, OTHER

LARK = channel_address("lark", "channel-lark")


def _direct() -> MessageEnvelope:
    return MessageEnvelope.create(
        source=LARK, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": "hello", "channel_context": {"chat_id": "oc_synthetic_other_dm", "chat_type": "p2p",
                                                       "sender_id": OTHER}},
        metadata={"origin_channel_id": LARK.agent_id})


def _manager(root: Path, monkeypatch: pytest.MonkeyPatch) -> ManagerAgent:
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(session_routing, "_max_resident", 2)
    monkeypatch.setattr(completion_events, "RETRY_SECONDS", 0.01)
    manager = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root)
    manager._running = True
    return manager


@pytest.mark.parametrize("previously_unconfigured", (False, True))
def test_an_unknown_operator_lookup_pauses_the_message_until_retry(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   previously_unconfigured: bool) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        if previously_unconfigured:
            manager._router._operator_ids[LARK.agent_id] = (time.monotonic() - 60, None)
        failed = asyncio.Event()
        recover = asyncio.Event()

        async def operator(channel_id: str) -> str:
            if not recover.is_set():
                failed.set()
                raise OSError("identity store unavailable")
            return OPEN_ID

        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        message = _direct()
        pending = asyncio.create_task(manager._router.target(message))
        try:
            await failed.wait()
            await asyncio.sleep(0.03)
            assert not pending.done(), "an identity read failure must not route the DM to main"
            recover.set()
            session = await asyncio.wait_for(pending, 1)
            assert session.key == f"dm:{LARK.agent_id}:oc_synthetic_other_dm"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(test())


def test_a_failed_identity_refresh_keeps_the_known_operator(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        manager._router._operator_ids[LARK.agent_id] = (time.monotonic() - 60, OPEN_ID)

        async def operator(channel_id: str) -> str:
            raise OSError("identity store unavailable")

        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        session = await manager._router.target(_direct())
        assert session.key == f"dm:{LARK.agent_id}:oc_synthetic_other_dm"
        assert manager._router._operator_ids[LARK.agent_id][1] == OPEN_ID

    asyncio.run(test())


def _row() -> dict[str, Any]:
    return {"manager_id": "manager", "target_kind": "dm", "channel_id": LARK.agent_id,
            "chat": "oc_synthetic_notice_target", "payload": {"chat_id": "oc_synthetic_claimed_dm"},
            "fallback": [{"notice": {"kind": "user", "chat": OTHER}}]}


def test_a_completion_routes_from_the_claimed_event_without_a_peek(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        manager._bus = object()
        told = asyncio.Event()
        seen: list[str] = []
        monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)

        async def operator(channel_id: str) -> str:
            return OPEN_ID

        async def peek(*args: Any, **kwargs: Any) -> Any:
            raise OSError("the peek failed, but claiming is healthy")

        async def claim(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [_row()]

        async def tell(rows: Any, batch: uuid.UUID) -> None:
            seen.append(manager._session_now().key)
            told.set()

        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        monkeypatch.setattr(manager_events, "pending_one", peek)
        monkeypatch.setattr(manager_events, "claim", claim)
        monkeypatch.setattr(manager, "_tell_completion", tell)
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(_row()))
        target = await manager._router.target(wake)
        if target is not None:
            manager._current.set(target)
        try:
            await manager._handle_completion_due(wake)
            await asyncio.wait_for(told.wait(), 1)
            assert seen == [f"dm:{LARK.agent_id}:oc_synthetic_claimed_dm"]
        finally:
            manager._bus = None
            await manager._router.stop()

    asyncio.run(test())


def test_a_completion_identity_failure_retries_before_claiming(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        claimed: list[uuid.UUID] = []
        retried: list[Any] = []

        class Bus:
            async def publish(self, envelope: Any) -> None:
                retried.append(envelope)

        manager._bus = Bus()
        monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)

        async def operator(channel_id: str) -> str:
            raise OSError("identity store unavailable")

        async def claim(manager_id: str, channel_id: str, chat: str, batch: uuid.UUID) -> list[Any]:
            claimed.append(batch)
            return []

        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        monkeypatch.setattr(manager_events, "claim", claim)
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(_row()))
        await manager._handle_completion_due(wake)
        assert claimed == [] and len(retried) == 1
        assert retried[0].payload == wake.payload

    asyncio.run(test())


def test_concurrent_admissions_count_starts_still_in_progress(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        router = manager._router
        main = manager._sessions["main"]
        main.claude._process = object()  # ResidentClaude.started only checks that a process exists
        first = router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, "oc_synthetic_first"))
        second = router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, "oc_synthetic_second"))
        await first.turn_lock.acquire()
        await router.admit(first, None)
        waiting = asyncio.create_task(router.admit(second, None))
        try:
            await asyncio.sleep(0.03)
            assert not waiting.done(), "a start in progress already occupies its reserved resident slot"
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(test())


@pytest.mark.parametrize("restore", ("instructions", "api"))
def test_preparation_restores_also_reserve_resident_capacity(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch, restore: str) -> None:
    from looper.agents import session_switch
    from looper.agents.tiers import manager as manager_module
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.agents.tiers.manager import Ask

    class StartedForTest(Exception):
        pass

    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        router = manager._router
        main = manager._sessions["main"]
        main.claude._process = object()
        occupied = router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, "oc_synthetic_busy"))
        occupied.claude._process = object()
        await occupied.turn_lock.acquire()
        restoring = router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, "oc_synthetic_restore"))
        restoring.session_id, restoring.session_runtime, restoring.session_loaded = "saved", "api", True
        manager._current.set(restoring)
        started = asyncio.Event()

        class Resident:
            started = False

            async def start(self, *args: Any, **kwargs: Any) -> None:
                started.set()
                raise StartedForTest()

            async def stop(self, **kwargs: Any) -> None:
                pass

        async def nothing(*args: Any, **kwargs: Any) -> None:
            pass

        async def settings(**kwargs: Any) -> AgentSettings:
            return AgentSettings(profile=builtin_resolved_profile())

        async def attach(launch: Any, **kwargs: Any) -> Any:
            return launch

        switch = session_switch.SessionSwitch(session_switch.SwitchReason.INSTRUCTIONS_CHANGED, "test") \
            if restore == "instructions" else None
        monkeypatch.setattr(manager, "_load_db_config", nothing)
        monkeypatch.setattr(manager, "_read_templates", nothing)
        monkeypatch.setattr(manager, "_send_status", nothing)
        monkeypatch.setattr(manager, "_load_agent_settings", settings)
        monkeypatch.setattr(manager_module.llmrec, "attach", attach)
        monkeypatch.setattr(session_switch, "profile_switch", lambda *args, **kwargs: switch)
        monkeypatch.setattr(type(CLAUDE_CODE), "resident_session", lambda self, label: Resident())
        message = _direct()
        preparing = asyncio.create_task(manager._prepare_turn(manager._detached_token(message), message,
                                                              ask=Ask("hello", "", None)))
        try:
            await asyncio.sleep(0.1)
            assert not started.is_set(), "restoring a saved runtime must wait for resident capacity too"
            occupied.claude._process = None
            occupied.turn_lock.release()
            await router.changed()
            with pytest.raises(StartedForTest):
                await preparing
            assert started.is_set() and not router._reservations
        finally:
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)

    asyncio.run(test())


def test_a_handed_off_completion_stays_unsettled_until_its_routed_turn_ends(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        manager = _manager(tmp_path, monkeypatch)
        settled: list[str] = []
        started, release, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Bus:
            def settle_handled(self, envelope: MessageEnvelope) -> None:
                settled.append(envelope.message_id)
                ended.set()

        manager._bus = Bus()
        monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)

        async def operator(channel_id: str) -> str:
            return OPEN_ID

        async def claim(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [_row()]

        async def tell(rows: Any, batch: uuid.UUID) -> None:
            started.set()
            await release.wait()

        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        monkeypatch.setattr(manager_events, "claim", claim)
        monkeypatch.setattr(manager, "_tell_completion", tell)
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(_row()))
        # Main held this wake while routing was off; routing was enabled before its worker took it.
        manager._router.dispatch(manager._sessions["main"], wake)
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert settled == []
            release.set()
            await asyncio.wait_for(ended.wait(), 1)
            assert settled == [wake.message_id]
        finally:
            await manager._router.stop()

    asyncio.run(test())
