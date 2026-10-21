"""The Manager session's profile over HTTP: GET /api/manager/session, PUT /api/manager/session/profile, and
``new_session_profile_id`` on POST /api/chat/send. The Manager is a stub, so no database is needed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import operator_auth, profile_registry
from looper.agents.http_server import AgentHTTPServer
from looper.agents.message import MANAGER_ADDRESS
from looper.agents.operator_auth import Access
from looper.agents.tiers.task import TaskRouter

VIEW = {"session": {"active": True, "runtime": "claude-code", "profile_id": "agent-a"},
        "selected": {"profile_id": "agent-a", "name": "A"}, "effective": {"profile": {}, "source": "session"},
        "problem": None, "choices": []}
ORIGIN = {"channel": "web", "chat": None, "sender": "dashboard", "group": False}


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _manager(**overrides: Any) -> SimpleNamespace:
    result = SimpleNamespace(effect=SimpleNamespace(value="resumes"), text="done")
    return SimpleNamespace(session_view=AsyncMock(return_value=VIEW),
                           select_session_profile=AsyncMock(return_value=result), **overrides)


def _call(method: str, path: str, body: Any, tmp_path: Path, manager: Any = None,
          adapter: Any = None, planner: bool = False) -> tuple[int, Any]:
    router = TaskRouter(runtime_root=tmp_path)
    if planner:
        router.has_planner = lambda task_id: True  # type: ignore[method-assign]
    server = AgentHTTPServer(web_adapter=adapter or SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=router, manager=manager)
    writer = _Writer()
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    asyncio.run(server._route_api(method, path, {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_get_returns_the_view(tmp_path: Path) -> None:
    assert _call("GET", "/api/manager/session", b"", tmp_path, _manager()) == (200, VIEW)


def test_put_selects_and_returns_the_effect(tmp_path: Path) -> None:
    manager = _manager()
    status, body = _call("PUT", "/api/manager/session/profile", {"profile_id": "agent-a"}, tmp_path, manager)
    assert status == 200
    assert body == {**VIEW, "effect": "resumes", "text": "done"}
    manager.select_session_profile.assert_awaited_once_with("agent-a", origin=ORIGIN, via="api")


def test_put_null_follows_the_settings(tmp_path: Path) -> None:
    manager = _manager()
    status, _ = _call("PUT", "/api/manager/session/profile", {"profile_id": None}, tmp_path, manager)
    assert status == 200
    manager.select_session_profile.assert_awaited_once_with(None, origin=ORIGIN, via="api")


@pytest.mark.parametrize("body", [{}, b"{nope", {"profile_id": 3}])
def test_put_refuses_a_bad_body(tmp_path: Path, body: Any) -> None:
    manager = _manager()
    status, payload = _call("PUT", "/api/manager/session/profile", body, tmp_path, manager)
    assert status == 400 and payload["error"]
    manager.select_session_profile.assert_not_awaited()


def test_put_refused_selection_is_400(tmp_path: Path) -> None:
    manager = _manager()
    manager.select_session_profile.side_effect = profile_registry.RegistryError("no such profile")
    assert _call("PUT", "/api/manager/session/profile", {"profile_id": "x"}, tmp_path, manager) == (
        400, {"error": "no such profile"})


def test_no_manager_is_503(tmp_path: Path) -> None:
    expected = (503, {"error": "the Manager is not running"})
    assert _call("GET", "/api/manager/session", b"", tmp_path) == expected
    assert _call("PUT", "/api/manager/session/profile", {"profile_id": None}, tmp_path) == expected


def _send(tmp_path: Path, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[
        int, Any, AsyncMock]:
    adapter = SimpleNamespace(handle_user_message=AsyncMock(return_value={"correlation_id": "c"}))
    monkeypatch.setattr(profile_registry, "check_selection", AsyncMock(return_value=None))
    from looper.agents import projects
    monkeypatch.setattr(projects, "store_chat_supplement", AsyncMock(return_value=None))
    status, payload = _call("POST", "/api/chat/send", body, tmp_path, adapter=adapter, **kwargs)
    return status, payload, adapter.handle_user_message


def test_send_passes_the_profile_with_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status, _, handle = _send(tmp_path, {"text": "/new", "new_session_profile_id": "agent-a"}, monkeypatch)
    assert status == 200
    assert handle.await_args.kwargs["new_session_profile_id"] == "agent-a"
    assert handle.await_args.kwargs["target"] == MANAGER_ADDRESS


def test_send_without_the_profile_passes_nothing_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, handle = _send(tmp_path, {"text": "/new"}, monkeypatch)
    assert "new_session_profile_id" not in handle.await_args.kwargs


def test_send_profile_with_other_text_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status, payload, handle = _send(tmp_path, {"text": "hello", "new_session_profile_id": "agent-a"}, monkeypatch)
    assert status == 400 and payload == {"error": "new_session_profile_id goes only with /new to the Manager"}
    handle.assert_not_awaited()


def test_send_profile_to_a_planner_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"text": "/new", "new_session_profile_id": "agent-a", "target": {"kind": "planner", "task_id": "task-1"}}
    status, payload, handle = _send(tmp_path, body, monkeypatch, planner=True)
    assert status == 400 and "only with /new to the Manager" in payload["error"]
    handle.assert_not_awaited()


def test_send_refused_profile_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = SimpleNamespace(handle_user_message=AsyncMock())
    monkeypatch.setattr(profile_registry, "check_selection",
                        AsyncMock(side_effect=profile_registry.RegistryError("cannot serve")))
    status, payload = _call("POST", "/api/chat/send", {"text": "/new", "new_session_profile_id": "x"}, tmp_path,
                            adapter=adapter)
    assert (status, payload) == (400, {"error": "cannot serve"})
    adapter.handle_user_message.assert_not_awaited()


def test_the_paths_are_operator_only() -> None:
    assert operator_auth.access("/api/manager/session") is Access.OPERATOR
    assert operator_auth.access("/api/manager/session/profile") is Access.OPERATOR
