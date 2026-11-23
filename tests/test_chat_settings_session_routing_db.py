"""Settings → Chat turns session routing on and sets the resident cap (assistant layer RFC, section 5.3).

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents import session_routing
from tests.db_support import DSN, run_db
from tests.test_api_v1_chat_session_controls_db import _api
from tests.test_chat_settings_ledger_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    monkeypatch.setattr(session_routing, "_max_resident", session_routing.DEFAULT_MAX_RESIDENT)


def test_turning_routing_on_stores_it_applies_it_and_lists_the_bots_without_an_operator_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) VALUES "
            "('channel-lark', 'lark', 'Example Lark', true, '{}'), "
            "('channel-telegram', 'telegram', 'Example Telegram', true, $1::jsonb)",
            json.dumps({"operator_sender_id": "12345"}))
        async with _api(tmp_path, pool, None) as (call, _):
            status, answer = await call("session_routing.get")
            assert status == 200 and answer["result"] == {"session_routing_enabled": False, "max_resident": 2}
            status, answer = await call("session_routing.update", {"session_routing_enabled": True, "max_resident": 3})
            assert status == 200
            result = answer["result"]
            status, answer = await call("session_routing.get")
            assert status == 200 and answer["result"] == {"session_routing_enabled": True, "max_resident": 3}
        assert (result["session_routing_enabled"], result["max_resident"]) == (True, 3)
        assert result["missing_operator_ids"] == [
            {"channel_id": "channel-lark", "channel_type": "lark", "name": "Example Lark"}]
        assert session_routing.routes("manager") and session_routing.max_resident() == 3
        stored = dict(await pool.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)",
                                       [session_routing.ROUTING_KEY, session_routing.MAX_RESIDENT_KEY]))
        assert stored == {session_routing.ROUTING_KEY: "true", session_routing.MAX_RESIDENT_KEY: "3"}
        restored = await _call("session_routing.update", {"session_routing_enabled": None, "max_resident": None})
        assert (restored["session_routing_enabled"], restored["max_resident"]) == (False, 2)
        assert "missing_operator_ids" not in restored and not session_routing.routes("manager")
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = ANY($1)",
                                   [session_routing.ROUTING_KEY, session_routing.MAX_RESIDENT_KEY]) == 0

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("value", [0, 17, 2.5, True, "2"])
def test_a_cap_outside_one_to_sixteen_is_refused_and_nothing_is_stored(value: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        refused = await _call("session_routing.update", {"max_resident": value, "session_routing_enabled": True})
        assert refused.code.value == "invalid"
        assert not await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = ANY($1)",
                                       [session_routing.MAX_RESIDENT_KEY, session_routing.ROUTING_KEY])

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("body", [{"session_routing_enabled": True}, {"max_resident": 4}])
def test_a_client_cannot_set_the_session_routing_settings(body: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            status, answer = await call("session_routing.update", body, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = ANY($1)",
                                       [session_routing.ROUTING_KEY, session_routing.MAX_RESIDENT_KEY,
                                        "chat.stop_enabled"]) == 0

    run_db(test, monkeypatch, timeout=30)
