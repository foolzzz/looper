"""The operator's list of the default Manager's sessions, and resetting one.

The list is read-only. Reset clears that session the way ``/new`` does and posts nothing.
An agent token is refused before either operation runs. Runs against disposable PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, session_routing
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.service import MANAGER_TOOLS
from looper.agents.session_routing import SessionKey, SessionKind
from looper.db import agent_sessions, manager_sessions
from tests.db_support import run_db
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_api_v1_chat_session_controls_db import _api, _manager

CHANNEL = "channel-lark"
NAMED_CHAT = "oc_synthroom01"
BARE_CHAT = "oc_synthbare02"
OPEN_CHAT = OPEN_ID  # ou_abcdefgh12; its stored name is the identifier and must not be shown
OTHER_CHAT = OTHER  # ou_zyxwvut987, shown under a human name
NAMED_KEY = f"group:{CHANNEL}:{NAMED_CHAT}"
BARE_KEY = f"group:{CHANNEL}:{BARE_CHAT}"
OPEN_KEY = f"dm:{CHANNEL}:{OPEN_CHAT}"
OTHER_KEY = f"dm:{CHANNEL}:{OTHER_CHAT}"


class _Resident:
    def __init__(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


def _stored(key: str, chat: str, kind: str, session_id: str, cost: float) -> dict:
    return {"manager_id": "manager", "session_key": key, "kind": kind, "channel_id": CHANNEL, "chat": chat,
            "session_id": session_id, "session_totals": {"cost_usd": cost}, "runtime_type": "claude-code",
            "profile_id": None, "provider_id": None, "clean_start": False, "pending_handoff": None,
            "context_tokens": 9999}


async def _seed(pool) -> None:
    await pool.execute(
        "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
        "VALUES ($1, 'lark', 'Synthetic Lark', true, '{}')", CHANNEL)
    await pool.executemany(
        "INSERT INTO channel_seen_chats (channel_id, chat_id, name) VALUES ($1, $2, $3)",
        [(CHANNEL, NAMED_CHAT, "Synthetic room"),
         (CHANNEL, BARE_CHAT, BARE_CHAT),
         (CHANNEL, OPEN_CHAT, OPEN_CHAT),
         (CHANNEL, OTHER_CHAT, "Other sender")])
    await agent_sessions.save_session("manager", "main-sess", {"cost_usd": 1.5}, runtime_type="claude-code")
    for row in (_stored(NAMED_KEY, NAMED_CHAT, "group", "group-sess", 0.25),
                _stored(BARE_KEY, BARE_CHAT, "group", "bare-sess", 0.1),
                _stored(OTHER_KEY, OTHER_CHAT, "dm", "other-sess", 0.4),
                _stored(OPEN_KEY, OPEN_CHAT, "dm", "open-sess", 0.05)):
        await manager_sessions.save(row)
    stamps = [(NAMED_KEY, datetime(2026, 11, 22, 10, tzinfo=timezone.utc)),
              (OTHER_KEY, datetime(2026, 11, 22, 9, tzinfo=timezone.utc)),
              (BARE_KEY, datetime(2026, 11, 22, 8, tzinfo=timezone.utc)),
              (OPEN_KEY, datetime(2026, 11, 22, 7, tzinfo=timezone.utc))]
    await pool.executemany("UPDATE manager_sessions SET last_active_at = $2 WHERE session_key = $1", stamps)


@pytest.mark.usefixtures("clients")
def test_agent_token_cannot_list_or_reset_sessions(tmp_path, monkeypatch):
    for action in ("list", "reset"):
        operation = api_v1.find("POST", f"/api/v1/manager_sessions/{action}")
        assert operation is not None and operation.operator_only and operation.tool is None
        assert operation.level is ActionLevel.INSIDE and operation.level_of is None
        assert operation.description.startswith(operation.summary)
    assert api_v1.find("POST", "/api/v1/manager_sessions/list").risk == "read"
    reset = api_v1.find("POST", "/api/v1/manager_sessions/reset")
    assert reset.risk == "write" and reset.confirms
    assert all("manager_sessions" not in tool.name for tool in MANAGER_TOOLS)

    async def check():
        from looper.agents.ops.clients import ClientOps
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        run = AsyncMock(side_effect=AssertionError("agent token reached the session list"))
        monkeypatch.setattr(api_v1.domain_for(reset), "run", run)
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for path, body in (("/api/v1/manager_sessions/list", {}),
                               ("/api/v1/manager_sessions/reset", {"key": NAMED_KEY})):
                status, _, answer = await _send(server, f"POST {path}", _bearer(CLIENT_TOKEN), json.dumps(body))
                assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()

    import asyncio
    asyncio.run(check())


def test_generated_cli_lists_and_resets_without_a_hand_command():
    from looper import loopercli

    parser = loopercli.build_parser(api_v1.document())
    listed = parser.parse_args(["manager-sessions", "list"])
    resetting = parser.parse_args(["manager-sessions", "reset", "--key", "main"])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    options = SimpleNamespace(api=lambda: api, as_json=True, session="controls", operator=True)
    for args in (listed, resetting):
        assert args._run(args, options) == 0
    assert calls == [("POST", "/api/v1/manager_sessions/list", {}),
                     ("POST", "/api/v1/manager_sessions/reset", {"key": "main"})]
    assert api_v1.find("POST", "/api/v1/manager_sessions/list").hand is False


def test_list_names_sessions_and_reset_clears_only_that_row(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        manager = _manager(tmp_path, monkeypatch)
        resident = _Resident()
        manager._router.session(SessionKey(SessionKind.GROUP, CHANNEL, NAMED_CHAT)).claude = resident
        before = await pool.fetchval("SELECT count(*) FROM manager_sessions")
        async with _api(tmp_path, pool, manager) as (call, _client):
            status, answer = await call("manager_sessions.list")
            assert status == 200, answer
            result = answer["result"]
            assert result["routing_enabled"] is False  # the switch stays off; the rows are still listed
            rows = {row["key"]: row for row in result["sessions"]}
            assert [row["key"] for row in result["sessions"]] == ["main", NAMED_KEY, OTHER_KEY, BARE_KEY, OPEN_KEY]
            assert rows["main"]["name"] == "主会话" and rows["main"]["kind"] == "main"
            assert rows["main"]["resident"] is False and rows["main"]["cost_usd"] == 1.5
            assert rows["main"]["context_tokens"] is None and rows["main"]["last_active_at"]
            assert rows[NAMED_KEY] == {"key": NAMED_KEY, "kind": "group", "name": "Synthetic room",
                                       "resident": True, "last_active_at": rows[NAMED_KEY]["last_active_at"],
                                       "context_tokens": None, "cost_usd": 0.25, "scope": None, "retired": False}
            assert rows[BARE_KEY]["name"] == BARE_CHAT[-6:]
            assert rows[OPEN_KEY]["name"] == OPEN_CHAT[-6:]
            assert rows[OTHER_KEY]["name"] == "Other sender" and rows[OTHER_KEY]["kind"] == "dm"
            assert all(not str(row["name"]).startswith(("ou_", "on_", "oc_", "cli_")) for row in rows.values())
            assert await pool.fetchval("SELECT count(*) FROM manager_sessions") == before
            assert (await agent_sessions.get_session("manager"))["session_id"] == "main-sess"

            missing, _ = await call("manager_sessions.reset", {"key": "group:missing:oc_none"})
            assert missing == 404
            bad, _ = await call("manager_sessions.reset", {"key": "nope"})
            assert bad == 400
            assert (await manager_sessions.get("manager", NAMED_KEY))["session_id"] == "group-sess"

            status, answer = await call("manager_sessions.reset", {"key": NAMED_KEY})
            assert status == 200, answer
            reset = answer["result"]
            assert reset["key"] == NAMED_KEY and reset["resident"] is False and reset["cost_usd"] is None
            assert reset["context_tokens"] is None and reset["name"] == "Synthetic room"
            stored = await manager_sessions.get("manager", NAMED_KEY)
            assert stored["session_id"] is None and stored["clean_start"] is True
            assert stored["context_tokens"] == 9999  # 1.4 owns this column; reset does not wipe it
            assert (await agent_sessions.get_session("manager"))["session_id"] == "main-sess"
            assert resident.started is False
            assert await manager_sessions.get("manager", "group:missing:oc_none") is None

            status, answer = await call("manager_sessions.reset", {"key": "main"})
            assert status == 200 and answer["result"]["name"] == "主会话"
            assert (await agent_sessions.get_session("manager"))["session_id"] is None
            assert (await agent_sessions.get_session("manager"))["clean_start"] is True
            assert (await manager_sessions.get("manager", OTHER_KEY))["session_id"] == "other-sess"

        assert session_routing.routing_enabled() is False

    run_db(check, monkeypatch, timeout=60)


def test_the_list_is_unavailable_when_the_manager_is_not_running(tmp_path, monkeypatch):
    async def check(pool):
        async with _api(tmp_path, pool, None) as (call, _client):
            status, answer = await call("manager_sessions.list")
            assert status == 503 and answer["error"]["code"] == "unavailable"

    run_db(check, monkeypatch, timeout=30)
