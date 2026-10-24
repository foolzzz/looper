"""The chat's stop setting on a real PostgreSQL: off with no row, stored through the API, read back at startup.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from tests.db_support import DSN
from tests.db_support import run_db
from tests.test_api_v1_chat_session_controls_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_stop_setting_is_stored_and_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            assert await chat_settings.load() is False
            assert (await call("chat_settings.get"))[1]["result"] == {"stop_enabled": False}
            assert (await call("chat_settings.update", {"stop_enabled": True}))[1]["result"] == {"stop_enabled": True}
            chat_settings.set_stop_enabled(False)  # a restart
            assert await chat_settings.load() is True
            assert (await call("chat_settings.update", {"stop_enabled": None}))[1]["result"] == {"stop_enabled": False}
            assert await chat_settings.load() is False

    run_db(test, monkeypatch, timeout=30)
