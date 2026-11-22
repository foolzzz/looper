"""A session credential cannot borrow another session's turn, including while routing drains."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, session_routing
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops import Audience, Call, RestrictedTool, Risk, Tool, Turn
from looper.agents.ops import service
from looper.agents.ops.service import ManagerOps, Platform


GROUP = "group:synthetic-bot:synthetic-group"


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(service.db_actions, "record", AsyncMock())


def _ops(root: Path) -> tuple[ManagerOps, list[str | None]]:
    ran: list[str | None] = []

    async def run(call: Call) -> dict[str, Any]:
        ran.append(call.turn.message_id)
        return {"message_id": call.turn.message_id}

    tool = Tool("write", "Writes", {"type": "object", "properties": {}}, Risk.WRITE, run)
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="main-token",
                     tools=(tool,), turn_file=root / ".local" / "manager-turn.json")
    return ops, ran


async def _call(ops: ManagerOps, credential: str, binding: str | None) -> dict[str, Any]:
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace(), manager_ops=ops)
    headers = {"authorization": f"Bearer {credential}"}
    if binding is not None:
        headers["x-looper-turn"] = binding
    writer = _Writer()
    await server._handle_agent_api("POST", "/api/agent/v1/ops/write", headers, b"{}", writer)
    return json.loads(writer.data.partition(b"\r\n\r\n")[2])


def test_session_entries_have_distinct_stable_credentials(tmp_path: Path) -> None:
    ops, _ = _ops(tmp_path)
    main = ops.server_config()["env"][service.TOKEN_ENV]
    group = ops.server_config(session_key=GROUP)["env"][service.TOKEN_ENV]
    assert group != main
    assert group == ops.server_config(session_key=GROUP)["env"][service.TOKEN_ENV]
    assert ops.authorized(f"Bearer {group}")
    assert ops.server_config()["env"][service.TOKEN_ENV] == "main-token"


@pytest.mark.parametrize("forged", [False, True])
def test_endpoint_refuses_another_sessions_binding(tmp_path: Path, forged: bool) -> None:
    async def run() -> None:
        ops, ran = _ops(tmp_path)
        main = Turn("web", "main-chat", message_id="main-message")
        group = Turn("lark", "synthetic-group", message_id="group-message", group=True)
        ops.begin_turn(main, binding="main-binding")
        ops.publish_binding("main-binding")
        ops.begin_turn(group, binding="group-binding", session_key=GROUP)
        ops.publish_binding("group-binding", session_key=GROUP)
        # Even with the other binding learned through Read/Bash, the server checks its credential's session.
        key, binding = (GROUP, "main-binding") if forged else ("main", "group-binding")
        credential = ops.server_config(session_key=key)["env"][service.TOKEN_ENV]
        answer = await _call(ops, credential, binding)
        assert answer["status"] == "error" and ran == []
        assert answer["error"]["code"] == "forbidden"
        assert any(row["reason"] == "other_session" for row in ops.binding_report()["by"])
        own = await _call(ops, credential, "group-binding" if forged else "main-binding")
        assert own["status"] == "ok"
        assert ran == ["group-message" if forged else "main-message"]

    asyncio.run(run())


def test_switch_off_keeps_binding_enforced_until_routed_turn_ends(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        ops, ran = _ops(tmp_path)
        group = Turn("lark", "synthetic-group", message_id="group-message", group=True)
        ops.begin_turn(group, binding="group-binding", session_key=GROUP)
        ops.publish_binding("group-binding", session_key=GROUP)
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        main = Turn("web", "main-chat", message_id="main-message")
        ops.begin_turn(main, binding="main-binding")
        ops.publish_binding("main-binding")
        assert ops.binding_report()["enforced"]
        missing = await _call(ops, "main-token", None)
        assert missing["status"] == "error" and ran == []
        credential = ops.server_config(session_key=GROUP)["env"][service.TOKEN_ENV]
        own = await _call(ops, credential, "group-binding")
        assert own["status"] == "ok" and ran == ["group-message"]
        ops.end_turn(group)
        assert not ops.binding_report()["enforced"]
        resumed = await _call(ops, "main-token", None)
        assert resumed["status"] == "ok" and ran == ["group-message", "main-message"]

    asyncio.run(run())


def test_group_credential_lists_and_calls_only_its_own_turns_tools(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async def read(call: Call) -> dict[str, Any]:
            return {"private": "synthetic-operator-note"}

        tool = RestrictedTool("private_read", "Reads operator notes", {"type": "object", "properties": {}},
                              Risk.READ, read, audience=Audience.OPERATOR, refusal="operator only")
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", tools=(tool,),
                         turn_file=tmp_path / ".local" / "manager-turn.json")
        monkeypatch.setattr(ops, "current_scope", AsyncMock(return_value=ops.scope))
        monkeypatch.setattr(service, "operator_sender_id", AsyncMock(return_value="synthetic-operator"))
        group = Turn("lark", "synthetic-group", message_id="group-message", group=True)
        ops.begin_turn(group, binding="group-binding", session_key=GROUP)
        ops.publish_binding("group-binding", session_key=GROUP)
        # Another session is now the latest one. Both MCP listing and invocation still use the credential's turn.
        ops.begin_turn(Turn("web", "main-chat", message_id="main-message"), binding="main-binding")
        credential = ops.server_config(session_key=GROUP)["env"][service.TOKEN_ENV]
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(), manager_ops=ops)
        writer = _Writer()
        await server._handle_agent_api("GET", "/api/agent/v1/tools", {"authorization": f"Bearer {credential}"},
                                       b"", writer)
        assert json.loads(writer.data.partition(b"\r\n\r\n")[2])["tools"] == []
        assert [item["name"] for item in await ops.current_listing(session_key="main")] == ["private_read"]
        writer = _Writer()
        await server._handle_agent_api("POST", "/api/agent/v1/ops/private_read",
                                       {"authorization": f"Bearer {credential}", "x-looper-turn": "group-binding"},
                                       b"{}", writer)
        answer = json.loads(writer.data.partition(b"\r\n\r\n")[2])
        assert answer["status"] == "error" and answer["error"]["code"] == "forbidden"
        assert "synthetic-operator-note" not in writer.data.decode()

    asyncio.run(run())
