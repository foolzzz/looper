"""The terminal listener and disabled service never hold up task completion."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_terminal_callbacks_are_independent():
    from looper.agents.memory_retro_service import terminal_listener

    async def check():
        scheduler = AsyncMock(side_effect=RuntimeError("synthetic scheduler failure"))
        service = SimpleNamespace(enqueue_finished=AsyncMock())
        await terminal_listener(scheduler, service)("task-synthetic", "completed")
        service.enqueue_finished.assert_awaited_once_with("task-synthetic", "completed")
        scheduler.reset_mock(side_effect=True)
        service.enqueue_finished.side_effect = RuntimeError("synthetic enqueue failure")
        await terminal_listener(scheduler, service)("task-synthetic", "failed")
        scheduler.assert_awaited_once_with("task-synthetic", "failed")
    asyncio.run(check())


def test_disabled_service_does_not_scan_or_claim(monkeypatch, tmp_path):
    from looper.agents import memory_lessons
    from looper.agents.memory_retro_service import MemoryRetroService

    async def check():
        monkeypatch.setattr(memory_lessons, "settings_view", AsyncMock(return_value={
            "enabled": False, "problem": None, "settings": {}}))
        pool = SimpleNamespace(acquire=lambda: (_ for _ in ()).throw(AssertionError("disabled queue access")))
        service = MemoryRetroService(tmp_path, db=pool)
        assert not await service.run_once()
        await service.enqueue_finished("task-synthetic", "completed")
        assert service.drain_waiting() == 0
    asyncio.run(check())


def test_failure_notice_only_uses_the_configured_operator_destination(monkeypatch, tmp_path):
    from looper.agents import operator_identity
    from looper.db import system_settings
    from looper.agents.memory_lessons_model import parse_settings
    from looper.agents.memory_retro_service import MemoryRetroService

    async def check():
        sender = AsyncMock()
        identity = AsyncMock(return_value=None)
        claim = AsyncMock(side_effect=[True, False])
        monkeypatch.setattr(system_settings, "claim", claim)
        monkeypatch.setattr(operator_identity, "operator_sender_id", identity)
        service = MemoryRetroService(tmp_path, sender=sender)
        await service._failure_notice(parse_settings({}), ("task-a", "task-b", "task-c"))
        identity.assert_not_awaited()
        settings = parse_settings({"weekly": {"push_channel": "synthetic-bot"}})
        await service._failure_notice(settings, ("task-a", "task-b", "task-c"))
        sender.assert_not_awaited()
        identity.return_value = "synthetic-operator-dm"
        await service._failure_notice(settings, ("task-a", "task-b", "task-c"))
        assert sender.await_args.args[:2] == ("synthetic-bot", "synthetic-operator-dm")
        await service._failure_notice(settings, ("task-d", "task-e", "task-f"))
        sender.assert_awaited_once()
    asyncio.run(check())
