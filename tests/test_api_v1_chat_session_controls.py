from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.ops.clients import ClientOps
from tests.test_api_v1_auth import clients as clients
from looper.agents.runtimes import StopMethod
from looper.agents.tiers.manager import StopRequest, StopResult
from looper.agents.tiers.turn_token import TurnPhase, TurnToken


@pytest.fixture(autouse=True)
def _stop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping a reply is off by default (``chat_settings``); the tests of the stop itself turn it on."""
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class _Manager:
    """A stand-in whose ``stop_turn`` marks the token like the real one does."""

    def __init__(self, result: StopResult = StopResult.STOPPING, turn: TurnToken | None = None, *,
                 can_interrupt: bool = True) -> None:
        self.result = result
        self.current_turn = turn
        self.can_interrupt = can_interrupt
        self.stops: list[tuple[str, str]] = []

    async def stop_turn(self, message_id: str, *, via: str) -> StopResult:
        self.stops.append((message_id, via))
        token = self.current_turn
        if token is not None and self.result is StopResult.STOPPING and token.stop is None:
            token.request_stop(via)
        return self.result

    def turn_for(self, message_id: str) -> TurnToken | None:
        return self.current_turn

    def current_turn_for(self, channel: str, chat: str) -> TurnToken | None:
        turn = self.current_turn
        return turn if turn and turn.channel == channel and turn.chat == chat else None


def _call(manager: Any, method: str, path: str, body: bytes = b"",
          query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer

    async def run() -> tuple[int, Any]:
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(), manager=manager)
        writer = _Writer()
        server._client_ops = ClientOps(SimpleNamespace())
        raw = json.dumps(query).encode() if query is not None else body
        await server._handle_v1(method, path, {}, {}, raw, writer, api_v1.OPERATOR)
        head, _, payload = writer.data.partition(b"\r\n\r\n")
        answer = json.loads(payload)
        if answer.get("status") == "ok":
            answer = answer["result"]
            api_v1.validate_arguments(api_v1.find(method, path).result, answer)
        return int(head.split()[1]), answer

    return asyncio.run(run())


def _turn(**kw: Any) -> TurnToken:
    return TurnToken(message_id="m1", client_message_id=kw.pop("client_message_id", None), channel="web", chat="s1",
                     received_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), **kw)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"message_id": ""}', b"[]"])
def test_stop_rejects_bad_body(body: bytes) -> None:
    status, _ = _call(_Manager(), "POST", "/api/v1/operator_chat/stop", body)
    assert status == 400


def test_stop_without_manager_is_503() -> None:
    status, body = _call(None, "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert status == 503 and body["error"]


def test_stop_stopping_passes_via_web_and_reports_method() -> None:
    manager = _Manager(StopResult.STOPPING, _turn())
    manager.current_turn.method = StopMethod.CONTROL
    status, body = _call(manager, "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert (status, body) == (200, {"state": "stopping", "phase": "received", "method": "control"})
    assert manager.stops == [("m1", "web")]


def test_stop_method_unknown_yet_is_null() -> None:
    manager = _Manager(StopResult.ALREADY_STOPPING, _turn())
    status, body = _call(manager, "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert (status, body) == (200, {"state": "already_stopping", "phase": "received", "method": None})


@pytest.mark.parametrize("result", [StopResult.NOT_RUNNING, StopResult.OTHER_MESSAGE, StopResult.CANNOT_INTERRUPT])
def test_stop_conflicts(result: StopResult) -> None:
    status, body = _call(_Manager(result), "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert status == 409 and body["error"]["code"] == "conflict" and body["error"]["details"]["state"] == result.value


def test_turn_validation() -> None:
    assert _call(_Manager(), "POST", "/api/v1/operator_chat/turn")[0] == 400
    assert _call(None, "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})[0] == 503


def test_turn_reports_running_turn_only_for_its_session() -> None:
    manager = _Manager(turn=_turn(stop=StopRequest(via="web", at=datetime.now(timezone.utc))))
    status, body = _call(manager, "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})
    assert status == 200
    assert body == {"turn": {"message_id": "m1", "client_message_id": None, "channel": "web",
                             "started_at": "2026-01-02T03:04:05+00:00", "state": "received", "method": None,
                             "stopping": True, "can_interrupt": True}}
    assert _call(manager, "POST", "/api/v1/operator_chat/turn", query={"session_id": "other"}) == (200, {"turn": None})
    assert _call(_Manager(), "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"}) == (200, {"turn": None})


def test_stop_of_a_runtime_without_an_interrupt_is_refused_plainly() -> None:
    status, body = _call(_Manager(StopResult.CANNOT_INTERRUPT, turn=_turn(), can_interrupt=False), "POST",
                         "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert status == 409 and body["error"]["code"] == "conflict"
    assert body["error"]["details"]["state"] == "cannot_interrupt"
    _, body = _call(_Manager(turn=_turn(), can_interrupt=False), "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})
    assert body["turn"]["can_interrupt"] is False


def test_turn_carries_the_envelope_id_and_the_pages_own_id() -> None:
    manager = _Manager(turn=_turn(client_message_id="c1"))
    _, body = _call(manager, "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})
    assert body["turn"]["message_id"] == "m1" and body["turn"]["client_message_id"] == "c1"


def test_stop_reports_the_phase_it_found_the_turn_in() -> None:
    running = _turn()
    running.phase = TurnPhase.RUNNING
    _, body = _call(_Manager(StopResult.STOPPING, running), "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert body["phase"] == "running"
    _, body = _call(_Manager(StopResult.STOPPING, _turn()), "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert body["phase"] == "received"


def test_turn_reports_a_queued_token_as_received() -> None:
    _, body = _call(_Manager(turn=_turn()), "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})
    assert body["turn"]["state"] == "received" and body["turn"]["stopping"] is False


def test_stop_setting_is_off_by_default() -> None:
    from looper.agents import chat_settings as fresh

    assert fresh.DEFAULT_STOP_ENABLED is False


def test_stop_is_refused_and_changes_nothing_when_the_setting_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    manager = _Manager(StopResult.STOPPING, _turn())
    status, body = _call(manager, "POST", "/api/v1/operator_chat/stop", b'{"message_id": "m1"}')
    assert status == 409 and body["error"]["details"]["state"] == "disabled" and body["error"]
    assert manager.stops == [] and manager.current_turn.stop is None


def test_turn_keeps_working_when_the_setting_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    manager = _Manager(turn=_turn())
    status, body = _call(manager, "POST", "/api/v1/operator_chat/turn", query={"session_id": "s1"})
    assert status == 200 and body["turn"]["message_id"] == "m1"


def test_the_dashboard_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    assert _call(_Manager(), "POST", "/api/v1/chat_settings/get") == (200, {"stop_enabled": False})
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)
    assert _call(_Manager(), "POST", "/api/v1/chat_settings/get") == (200, {"stop_enabled": True})


@pytest.mark.parametrize("body", [b"{}", b'{"stop_enabled": "yes"}', b'{"stop_enabled": 1}', b'{"other": true}'])
def test_the_setting_refuses_a_bad_update(body: bytes) -> None:
    status, answer = _call(_Manager(), "POST", "/api/v1/chat_settings/update", body)
    assert status == 400 and answer["error"]
    assert chat_settings.stop_enabled() is True  # unchanged


@pytest.mark.parametrize("operation,options,body", [
    ("manager_session.get", ["--manager", "manager-review"], {"manager": "manager-review"}),
    ("manager_session.select_profile", ["--profile-id", "null"], {"profile_id": None}),
    ("operator_chat.turn", ["--session-id", "s1"], {"session_id": "s1"}),
    ("operator_chat.stop", ["--message-id", "m1"], {"message_id": "m1"}),
    ("chat_settings.get", [], {}),
    ("chat_settings.update", ["--stop-enabled", "null"], {"stop_enabled": None}),
])
def test_generated_cli_posts_ordinary_control_operations(operation, options, body):
    from looper import loopercli

    family, action = operation.split(".")
    args = loopercli.build_parser(api_v1.document()).parse_args([
        family.replace("_", "-"), action.replace("_", "-"), *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="controls")) == 0
    assert calls == [("POST", "/api/v1/" + operation.replace(".", "/"), body)]
    assert not api_v1.find("POST", calls[0][1]).hand


@pytest.mark.parametrize("operation,body", [
    ("manager_session.get", {"manager": None}),
    ("manager_session.select_profile", {}),
    ("manager_session.select_profile", {"profile_id": 1}),
    ("manager_session.select_profile", {"profile_id": None, "origin": {"sender": "dashboard"}}),
    ("manager_session.select_profile", {"profile_id": None, "via": "api"}),
    ("manager_session.select_profile", {"profile_id": None, "manager_id": "manager-other"}),
    ("manager_session.select_profile", {"profile_id": None, "source": "web"}),
    ("operator_chat.turn", {"session_id": None}),
    ("operator_chat.stop", {"message_id": 1}),
    ("chat_settings.get", {"unknown": True}),
    ("chat_settings.update", {"stop_enabled": 1}),
])
def test_control_requests_are_closed_and_keep_nullable_distinct_from_optional(operation, body):
    from looper.agents.ops import OpError

    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/" + operation.replace(".", "/")).request, body)


@pytest.mark.parametrize("action,body", [("turn", {"session_id": "s1"}), ("stop", {"message_id": "m1"})])
def test_browser_controls_refuse_client_identity_before_touching_the_manager(tmp_path, clients, action, body):
    from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving
    from tests.test_operator_auth import TOKEN

    async def run():
        async with _serving(tmp_path) as server:
            server._manager = _Manager(turn=_turn())
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/operator_chat/" + action,
                                            _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert not server._manager.stops and server._manager.current_turn.stop is None
            status, _, answer = await _send(server, "POST /api/v1/operator_chat/" + action,
                                            _bearer(TOKEN), json.dumps(body))
            assert status == 200 and answer["result"]
    asyncio.run(run())
