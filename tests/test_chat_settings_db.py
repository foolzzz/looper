"""The chat's stop setting on a real PostgreSQL: off with no row, stored through the API, read back at startup.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from tests.test_schedules_db import DSN, _Env, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_stop_setting_is_stored_and_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        assert await chat_settings.load() is False
        assert await env.call("GET", "/api/chat-settings") == (200, {"stop_enabled": False})
        assert await env.call("PUT", "/api/chat-settings", {"stop_enabled": True}) == (200, {"stop_enabled": True})
        chat_settings.set_stop_enabled(False)  # a restart
        assert await chat_settings.load() is True
        assert await env.call("PUT", "/api/chat-settings", {"stop_enabled": None}) == (200, {"stop_enabled": False})
        assert await chat_settings.load() is False

    _run(test, monkeypatch, tmp_path)
