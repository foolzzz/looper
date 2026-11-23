"""Switch writes keep the database and the synchronous process-start check in the same order."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import outbound_hook
from looper.db import connection, system_settings


def test_concurrent_switch_writes_wait_for_the_previous_database_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test() -> None:
        off_committed, release_off, on_requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stored = "true"
        writes: list[str] = []

        async def execute(query: str, key: str, value: str) -> None:
            nonlocal stored
            stored = value  # The database commits before its response reaches the caller.
            writes.append(value)
            if value == "false":
                off_committed.set()
                await release_off.wait()

        async def read(keys: Any, **kwargs: Any) -> dict[str, str]:
            return {outbound_hook.ENABLED_KEY: stored}

        async def managers() -> list[Any]:
            return []

        async def turn_on() -> dict[str, Any]:
            on_requested.set()
            return await outbound_hook.set_enabled(True)

        monkeypatch.setattr(connection, "get_pool", lambda: SimpleNamespace(execute=execute))
        monkeypatch.setattr(system_settings, "get_settings", read)
        monkeypatch.setattr(outbound_hook, "managers_without_hook", managers)
        monkeypatch.setattr(outbound_hook, "_known", True)
        monkeypatch.setattr(outbound_hook, "_version", 0)
        off = asyncio.create_task(outbound_hook.set_enabled(False))
        await off_committed.wait()
        on = asyncio.create_task(turn_on())
        await on_requested.wait()  # The newer request has reached its write or is waiting for the older one.
        try:
            assert writes == ["false"], "the newer write overtook the older database response"
            assert not on.done()
        finally:
            release_off.set()
            await asyncio.gather(off, on)
        assert writes == ["false", "true"] and stored == "true"
        assert outbound_hook.known_enabled()

    # Settings tests and repeated server starts use fresh loops; contention must work on each one.
    asyncio.run(test())
    asyncio.run(test())
