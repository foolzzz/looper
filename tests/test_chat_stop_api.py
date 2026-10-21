from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.runtimes import StopMethod
from looper.agents.tiers.manager import CurrentTurn, StopRequest, StopResult


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class _Manager:
    def __init__(self, result: StopResult = StopResult.STOPPING, turn: CurrentTurn | None = None) -> None:
        self.result = result
        self.current_turn = turn
        self.stops: list[tuple[str, str]] = []

    async def stop_turn(self, message_id: str, *, via: str) -> StopResult:
        self.stops.append((message_id, via))
        return self.result

    def current_turn_for(self, channel: str, chat: str) -> CurrentTurn | None:
        turn = self.current_turn
        return turn if turn and turn.channel == channel and turn.chat == chat else None


def _call(manager: Any, method: str, path: str, body: bytes = b"",
          query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer

    async def run() -> tuple[int, Any]:
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(), manager=manager)
        writer = _Writer()
        await server._route_api(method, path, query or {}, body, writer)
        head, _, payload = writer.data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    return asyncio.run(run())


def _turn(**kw: Any) -> CurrentTurn:
    return CurrentTurn(message_id="m1", channel="web", chat="s1",
                       started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), **kw)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"message_id": ""}', b"[]"])
def test_stop_rejects_bad_body(body: bytes) -> None:
    status, _ = _call(_Manager(), "POST", "/api/chat/stop", body)
    assert status == 400


def test_stop_without_manager_is_503() -> None:
    status, body = _call(None, "POST", "/api/chat/stop", b'{"message_id": "m1"}')
    assert status == 503 and body["error"]


def test_stop_stopping_passes_via_web_and_reports_method() -> None:
    manager = _Manager(StopResult.STOPPING, _turn())
    manager.current_turn.method = StopMethod.CONTROL
    status, body = _call(manager, "POST", "/api/chat/stop", b'{"message_id": "m1"}')
    assert (status, body) == (200, {"state": "stopping", "method": "control"})
    assert manager.stops == [("m1", "web")]


def test_stop_method_unknown_yet_is_null() -> None:
    manager = _Manager(StopResult.ALREADY_STOPPING, _turn())
    status, body = _call(manager, "POST", "/api/chat/stop", b'{"message_id": "m1"}')
    assert (status, body) == (200, {"state": "already_stopping", "method": None})


@pytest.mark.parametrize("result", [StopResult.NOT_RUNNING, StopResult.OTHER_MESSAGE])
def test_stop_conflicts(result: StopResult) -> None:
    status, body = _call(_Manager(result), "POST", "/api/chat/stop", b'{"message_id": "m1"}')
    assert status == 409 and body["state"] == result.value and body["error"].endswith(".")


def test_turn_validation() -> None:
    assert _call(_Manager(), "GET", "/api/chat/turn")[0] == 400
    assert _call(None, "GET", "/api/chat/turn", query={"session_id": "s1"})[0] == 503


def test_turn_reports_running_turn_only_for_its_session() -> None:
    manager = _Manager(turn=_turn(stop=StopRequest(via="web", at=datetime.now(timezone.utc))))
    status, body = _call(manager, "GET", "/api/chat/turn", query={"session_id": "s1"})
    assert status == 200
    assert body == {"turn": {"message_id": "m1", "channel": "web", "started_at": "2026-01-02T03:04:05+00:00",
                             "stopping": True}}
    assert _call(manager, "GET", "/api/chat/turn", query={"session_id": "other"}) == (200, {"turn": None})
    assert _call(_Manager(), "GET", "/api/chat/turn", query={"session_id": "s1"}) == (200, {"turn": None})
